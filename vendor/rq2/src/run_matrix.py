"""Global-pool matrix runner: all conditions x all cases in one worker pool.

Unlike run_experiment.py (one condition per invocation, workers idle at each
condition's tail), this driver queues every (condition, case) pair into a
single ThreadPoolExecutor so a freed worker immediately picks up the next
task regardless of which condition it belongs to.  Total concurrency never
exceeds --workers, keeping the relay's rate limit respected.

Output layout is identical to run_experiment.py: one directory per condition
under --output-root (cases/<case_id>/{prediction.json,trajectory.jsonl},
metrics_per_case.jsonl, metrics_summary.json, manifest.json), so --resume
skips completed cases and results are interchangeable with single-condition
runs.

Usage:
    python src/run_matrix.py --input data/cases.jsonl --output-root results/m50 \
        --conditions all --workers 15 --resume

Concurrency tuning: --workers is the GLOBAL cap at any moment.  Changing it requires a new output directory under audit-v1; resume requires
the same frozen concurrency and execution settings.
Isolation: every task deep-copies its PaymentCase before running, so
conditions can never contaminate each other through shared case data.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from multiagent.llm import load_llm
from payment.cases import load_cases
from payment.hooks import build_hooks, condition_name
from payment.mock_replay import build_replay_model
from payment.runner import CaseResult, run_case
from run_experiment import (
    _case_complete,
    _code_hashes,
    _prediction_from_record,
    _sha256_of_file,
    _write_json,
    load_config,
)
from scoring.audit_contract import load_constraint_annotations
from scoring.report import aggregate
from scoring.rq2_metrics import per_case_metrics

from experiment_integrity import (AttemptStore, failure_record, freeze_run,
                                  run_identity, select_cases)
from execution_schedule import balanced_tasks, provider_lease

ALL_CONDITIONS = ["b000", "b100", "b010", "b001", "b110", "b101", "b011", "b111"]


def get_args():
    parser = argparse.ArgumentParser(
        description="Global-pool matrix runner (all conditions, one worker pool)."
    )
    parser.add_argument("--input", required=True, help="merged cases.jsonl path")
    parser.add_argument("--output-root", required=True,
                        help="parent directory; one subdir per condition")
    parser.add_argument("--config", default="config/config.ini")
    parser.add_argument("--config-llm", default="config/config_llm.ini")
    parser.add_argument("--conditions", default="all",
                        help="'all' or comma list, e.g. b000,b111")
    parser.add_argument("--case-index", type=int, nargs="*", default=None)
    parser.add_argument("--sample", type=int, default=-1,
                        help="randomly sample N cases from the input (seeded)")
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--schedule-seed", type=int, default=20260918,
                        help="frozen seed for randomized case blocks and condition order")
    parser.add_argument("--limit", type=int, default=-1)
    parser.add_argument("--mock", action="store_true",
                        help="offline replay validation with scripted models")
    parser.add_argument("--allow-legacy-runtime-budget", action="store_true",
                        help="explicit historical-protocol comparison only; old quotas may depend on gold")
    parser.add_argument("--release-policy", default=None,
                        choices=["on_first_engagement", "disabled"])
    parser.add_argument("--workers", type=int, default=15,
                        help="GLOBAL concurrency cap across all conditions")
    parser.add_argument("--case-timeout", type=int, default=0,
                        help="per-case wall cap in seconds from case start; "
                             "0 disables. Timed-out cases are recorded as "
                             "INCOMPLETE (case_timeout) and stay retryable "
                             "in a fresh output dir.")
    parser.add_argument("--split", choices=["test", "dev", "all"], default="test")
    parser.add_argument("--require-reviewed", action="store_true")
    parser.add_argument("--constraint-annotations", default=None, help="optional scorer-only sidecar; never visible to agents")
    parser.add_argument("--retry-infrastructure", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def _flags_of(condition: str):
    return condition[1] == "1", condition[2] == "1", condition[3] == "1"


def main() -> int:
    args = get_args()
    config = load_config(args.config, args.config_llm)
    with provider_lease(config, mock=args.mock):
        return _main(args, config)


def _main(args, config) -> int:
    out_root = Path(args.output_root)
    if out_root.exists() and not args.resume:
        print(f"error: output root {out_root} already exists; "
              "pass --resume to continue it", file=sys.stderr)
        return 2
    out_root.mkdir(parents=True, exist_ok=True)

    conditions = (ALL_CONDITIONS if args.conditions == "all"
                  else [c.strip().lower() for c in args.conditions.split(",") if c.strip()])
    if not conditions or len(conditions) != len(set(conditions)):
        raise ValueError("Conditions must be non-empty and unique")
    if args.workers < 1 or args.case_timeout < 0:
        raise ValueError("workers must be positive and case-timeout nonnegative")
    for c in conditions:
        if c not in ALL_CONDITIONS:
            print(f"error: unknown condition {c!r}", file=sys.stderr)
            return 2

    run_cfg = config["RUN"] if config.has_section("RUN") else {}
    recursion_limit = int(run_cfg.get("recursion_limit", 30))
    release_policy = args.release_policy or run_cfg.get(
        "release_policy", "on_first_engagement"
    )
    scoring_cfg = config["SCORING"] if config.has_section("SCORING") else {}
    rel_tol = float(scoring_cfg.get("amount_rel_tol", 1e-6))
    abs_tol = float(scoring_cfg.get("amount_abs_tol", 1e-9))

    all_cases = select_cases(load_cases(args.input), args.split)
    if (not args.mock and not getattr(args, "allow_legacy_runtime_budget", False)
            and any(not c.runtime_policy for c in all_cases)):
        raise ValueError("Real matrix runs require round6 runtime policy. Prepare fresh inputs with "
                         "scripts/prepare_round6.py, or explicitly use --allow-legacy-runtime-budget "
                         "for a historical protocol comparison.")
    sampled_indices = None
    if args.case_index is not None:
        sampled_indices = list(args.case_index)
    elif args.sample > 0:
        import random

        rng = random.Random(args.seed)
        sampled_indices = sorted(rng.sample(range(len(all_cases)),
                                            min(args.sample, len(all_cases))))
    cases = ([all_cases[i] for i in sampled_indices]
             if sampled_indices is not None else list(all_cases))
    if args.limit > 0:
        cases = cases[: args.limit]
        if sampled_indices is not None:
            sampled_indices = sampled_indices[: args.limit]

    cases = select_cases(cases, "all", args.require_reviewed)
    constraint_annotations = load_constraint_annotations(args.constraint_annotations, args.input)
    identity = run_identity(cases, config, _code_hashes(Path(__file__).resolve().parent.parent),
                            constraint_annotations=constraint_annotations,
                            input_sha256=_sha256_of_file(args.input), conditions=conditions,
                            mock=args.mock, split=args.split, require_reviewed=args.require_reviewed, release_policy=release_policy,
                            recursion_limit=recursion_limit, workers=args.workers,
                            case_timeout=args.case_timeout, max_attempts=args.max_attempts,
                            schedule="case-block-randomized-v1", schedule_seed=args.schedule_seed,
                            retry_infrastructure=args.retry_infrastructure)
    identity_hash = freeze_run(out_root, identity)
    store = AttemptStore(out_root, identity_hash, args.retry_infrastructure, args.max_attempts)
    llm = None if args.mock else load_llm(config)
    started = time.time()

    # Attempt allocation precedes thread execution. The same terminal-write
    # lock protects successful returns, driver errors and wall timeouts.
    tasks = []
    for condition in conditions:
        for case in cases:
            attempt = store.begin(condition, case.case_id)
            if attempt is not None:
                tasks.append((condition, case, attempt))
    tasks = balanced_tasks(tasks, args.schedule_seed)
    starts = {}

    def process(condition, case, attempt):
        store.started(attempt)
        starts[str(attempt)] = time.monotonic()
        try:
            h1, h2, h3 = _flags_of(condition)
            model = build_replay_model(case, h1_intent="auto" if h1 else None) if args.mock else llm
            result = run_case(copy.deepcopy(case), model,
                              build_hooks(h1=h1, h2=h2, h3=h3, llm=model),
                              release_policy=release_policy, recursion_limit=recursion_limit,
                              verbose=args.verbose)
            store.finish(attempt, result.prediction_record(), result.raw_trajectory)
        except Exception as exc:
            store.finish(attempt, failure_record(case.case_id,
                         f"driver_error: {type(exc).__name__}: {exc}",
                         time.monotonic() - starts[str(attempt)]))

    pool = ThreadPoolExecutor(max_workers=args.workers)
    pending = {pool.submit(process, *t): t for t in tasks}
    while pending:
        for future, (condition, case, attempt) in list(pending.items()):
            if future.done():
                future.result()
                del pending[future]
                continue
            st = starts.get(str(attempt))
            if args.case_timeout > 0 and st is not None and time.monotonic() - st > args.case_timeout:
                store.finish(attempt, failure_record(case.case_id,
                             f"case_timeout: exceeded {args.case_timeout}s", args.case_timeout))
                del pending[future]
        if pending:
            time.sleep(0.05)
    # Python threads cannot be killed. Late completions remain in the attempt;
    # they cannot replace timeouts, and their cost is not claimed to be zero.
    # Keep the provider lease until all running requests drain. Primary timeout
    # records remain immutable; late completions are diagnostic artifacts only.
    pool.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------ per-condition scoring
    for condition in conditions:
        cond_dir = out_root / condition
        per_case = []
        for case in cases:
            record = store.record(condition, case.case_id)
            result = CaseResult(
                case_id=case.case_id,
                prediction=_prediction_from_record(record),
                raw_trajectory=[],
                agent_trajectory=record["agent_trajectory"],
                handoff_sequence=record["handoff_sequence"],
                receipts=record.get("receipts", []),
                tool_log=record.get("tool_log", []),
                released_events=record.get("released_events", []),
                final_state_version=record.get("final_state_version", 1),
                sim_calls=record.get("sim_calls", 0),
                wall_seconds=record.get("wall_seconds", 0.0),
                execution_context=record.get("execution_context"),
                model_call_audit=record.get("model_call_audit", {}),
            )
            per_case.append(per_case_metrics(case, result,
                                             rel_tol=rel_tol, abs_tol=abs_tol,
                                             constraint_annotations=constraint_annotations.get(case.case_id)))
        if not per_case:
            continue
        with open(cond_dir / "metrics_per_case.jsonl", "w", encoding="utf-8") as f:
            for row in per_case:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        summary = aggregate(per_case)
        summary["attempt_accounting"] = store.stats(condition, cases)
        print(f"{condition}: contract_tsr={summary['contract_tsr']} verified={summary['contract_tsr_verified_n']}/{len(cases)}; unknown={summary['contract_tsr_unverifiable_n']}")
        _write_json(cond_dir / "metrics_summary.json", summary)

        h1, h2, h3 = _flags_of(condition)
        config_snapshot = {s: dict(config.items(s)) for s in config.sections()}
        for _sec in config_snapshot.values():
            for _k in list(_sec):
                if "key" in _k.lower() and _sec[_k]:
                    _sec[_k] = "***"
        manifest = {
            "run_identity_sha256": identity_hash,
            "run_name": f"{out_root.name}/{condition}",
            "condition": condition_name(h1, h2, h3),
            "h1": h1, "h2": h2, "h3": h3,
            "mock": bool(args.mock),
            "simulated_user": False,
            "runner": "run_matrix",
            "config_llm_path": args.config_llm,
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "wall_seconds": round(time.time() - started, 3),
            "input_path": str(Path(args.input).resolve()),
            "input_sha256": identity["input_sha256"],
            "n_cases": len(cases),
            "sample_seed": args.seed if args.sample > 0 else None,
            "sampled_indices": sampled_indices,
            "config": config_snapshot,
            "code_sha256": identity["code_sha256"],
            "schedule": identity["schedule"], "schedule_seed": args.schedule_seed,
            "release_policy": release_policy,
            "tolerances": {"rel_tol": rel_tol, "abs_tol": abs_tol},
            "scope_note": (
                "Offline payment simulation only; this run makes no statement "
                "about real on-chain safety, production payment security, fraud "
                "detection, or regulatory compliance."
            ),
        }
        if not (cond_dir / "manifest.json").exists():
            _write_json(cond_dir / "manifest.json", manifest)
        _write_json(cond_dir / "last_invocation.json", manifest)
        print(f"== {condition}: accuracy={summary['accuracy']:.4f} "
              f"macro_f1={summary['macro']['f1']:.4f} legacy_tsr={summary['tsr']:.4f} "
              f"hf1={summary['hf1']:.4f} asr={summary['asr']:.4f} "
              f"valid_run_rate={summary['valid_run_rate']:.4f} "
              f"({summary['n_cases']} cases)")

    print(f"\nmatrix wall: {round(time.time() - started, 1)}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
