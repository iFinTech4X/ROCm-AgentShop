"""Offline, method-neutral replay with physically separate public and grading inputs."""

import argparse
import json
from pathlib import Path

from .adapter import PublicToolAdapter


def read_rows(path):
    rows = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list):
        raise ValueError("input must be a JSON array")
    return rows


def load_public(path):
    cases = {}
    for row in read_rows(path):
        if set(row) != {"case_id", "public_spec"}:
            raise ValueError("public records accept case_id and public_spec only")
        case_id = row["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in cases:
            raise ValueError("invalid or duplicate case_id")
        cases[case_id] = PublicToolAdapter(row["public_spec"])
    return cases


def load_references(path):
    references = {}
    for row in read_rows(path):
        if set(row) != {"case_id", "reference"} or row["case_id"] in references:
            raise ValueError("hidden records require unique case_id and reference")
        references[row["case_id"]] = row["reference"]
    return references


def replay(adapter, calls):
    if not isinstance(calls, list):
        raise ValueError("calls must be an array")
    outputs = []
    last_receipt = None
    for call in calls:
        if set(call) != {"name", "arguments"} or not isinstance(call["arguments"], dict):
            raise ValueError("calls require name and arguments object")
        arguments = dict(call["arguments"])
        if arguments.get("receipt_id") == "${last_receipt}":
            arguments["receipt_id"] = last_receipt
        result = adapter.call(call["name"], arguments)
        outputs.append(result)
        if result.get("status") == "SIMULATED_EXECUTED":
            last_receipt = result["receipt_id"]
    return {"outputs": outputs, "terminal": adapter.environment.terminal}


def grade(adapter, reference):
    from .oracle import derive_reference, evaluate

    terminal = adapter.environment.terminal
    if terminal is None:
        return {"correct": False, "violations": ["missing_terminal"]}
    spec = adapter.environment._initial
    if terminal["behavior"] == "EXECUTE":
        receipt = adapter.environment.receipts.get(terminal["receipt_id"])
        return evaluate(spec, reference, terminal["action"], receipt)
    expected = derive_reference(spec, reference)["expected_behavior"]
    return {"correct": terminal["behavior"] == expected, "expected_behavior": expected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", required=True)
    parser.add_argument("--calls", required=True)
    parser.add_argument("--references")
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    adapters = load_public(arguments.public)
    results = []
    seen = set()
    for row in read_rows(arguments.calls):
        if set(row) != {"case_id", "calls"} or row["case_id"] in seen:
            raise ValueError("replay records require unique case_id and calls")
        seen.add(row["case_id"])
        results.append({"case_id": row["case_id"], **replay(adapters[row["case_id"]], row["calls"])})
    if arguments.references:
        references = load_references(arguments.references)
        for result in results:
            result["evaluation"] = grade(adapters[result["case_id"]], references[result["case_id"]])
    Path(arguments.output).write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
