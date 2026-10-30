"""Gold-free adapter from the historical ledger interface to public commerce."""

from copy import deepcopy
import json

from payment.environment import EnvironmentLedger
from three_domain.contracts import digest
from three_domain.environment import PublicEnvironment


class CommerceLedger(EnvironmentLedger):
    is_commerce = True

    def __init__(self, case, release_policy="on_first_engagement", risk_model=None):
        if release_policy not in ("on_first_engagement", "disabled"):
            raise ValueError("unsupported release policy")
        if risk_model is not None:
            raise ValueError("commerce does not support legacy risk-model injection")
        self.public_environment = PublicEnvironment(case.environment["public_spec"])
        self.release_policy = release_policy
        self.risk_model = None
        self.network = "offline-commerce"
        self.schema_version = "three-domain-v1"
        self.conversation_start_at = self.public_environment.now
        self.timezone = "UTC"
        self.is_shop = self.public_environment.world["domain"] in ("Shopping", "ecommerce-payment")
        self.is_v4 = False
        self.simulation_permitted = True
        self.sim_max_calls = self.public_environment.world["policy"].get("max_simulations", 20)
        self.sim_per_proposal_max = None
        self.sim_per_proposal = {}
        self.sim_calls = 0
        self.receipts = []
        self.tool_log = []
        self.released_events = []
        self.pending_events = []
        self.state_transitions = []
        self.observed_products = []
        self.observed_action_catalogs = []
        self.intent_version = None
        self.engaged = False
        self.finish_response = None
        self.boundary_candidate = None
        self._chain = []
        self._chain_index = 0
        self._initial_user_turns = [turn["content"] for turn in case.visible_turns()
                                    if turn.get("role", "user") == "user"]
        self._sync()
        self.initial_state = deepcopy(self.current_state)

    @property
    def chain_exhausted(self):
        return self.public_environment.stage_index == len(self.public_environment.stages) - 1

    @property
    def current_version(self):
        return self.public_environment.state_version

    def _catalog(self, observation):
        accounts = [row["account_id"] for row in observation["accounts"]]
        space = observation["policy"].get("action_space", {})
        kinds = space.get("action_types", ["purchase"] if self.is_shop else ["book_flight", "change_flight"])
        catalog = []
        for kind in kinds:
            parameters = {"account_id": {"type": "string", "required": True, "enum": accounts}}
            if kind == "purchase":
                parameters.update({
                    "item_id": {"type": "string", "required": True,
                                "enum": [row["item_id"] for row in observation["items"]]},
                    "quantity": {"type": "integer", "required": True, "minimum": 1,
                                 "enum": space.get("quantities", [1])}})
            else:
                parameters.update({
                    "flight_ids": {"type": "array", "required": True, "enum": space.get("flight_routes", [])},
                    "passenger_ids": {"type": "array", "required": True, "enum": space.get("passenger_groups", [])}})
                if kind == "change_flight":
                    parameters["order_id"] = {"type": "string", "required": True,
                                               "enum": [row["order_id"] for row in observation["orders"]]}
            catalog.append({"id": kind, "operation": kind, "parameters": parameters,
                            "amount_unit": "minor", "currency": observation["currency"]})
        return catalog

    def _sync(self):
        observation = self.public_environment.query()
        self.current_state = {
            "protocol": "three-domain-v1",
            "initial_state_hash": self.public_environment.initial_state_hash,
            "network": self.network, "state_version": observation["state_version"],
            "action_catalog": self._catalog(observation), "asset_catalog": [],
            "facts": {"domain": observation["domain"], "amount_unit": "minor",
                      "currency": observation["currency"], "stage": observation["stage"],
                      "now": observation["now"],
                      "commerce_public_observation": deepcopy(observation),
                      "commerce_candidate": deepcopy(self.boundary_candidate)},
            "commerce_observation": observation,
            "public_observation": deepcopy(observation),
        }
        self.sim_calls = self.public_environment.simulation_calls

    def execution_context(self):
        observation = self.public_environment.query()
        return {"protocol": "three-domain-v1", "state_version": self.current_version,
                "conversation_sha256": digest(observation["messages"]),
                "authorization_versions": {row["account_id"]: row["authorization_version"]
                                           for row in observation["accounts"]},
                "stage": observation["stage"], "now": observation["now"],
                "commerce_stage": observation["stage"], "public_observation": observation,
                "finish_response": deepcopy(self.finish_response),
                "chain_exhausted": self.chain_exhausted, "amount_unit": "minor"}

    def _peek(self, state):
        return deepcopy(state)

    def peek_catalog(self):
        return {key: deepcopy(self.current_state[key])
                for key in ("protocol", "initial_state_hash", "network", "state_version", "action_catalog", "asset_catalog", "public_observation")}

    def environment_query(self, scope="current"):
        if scope not in ("current", "initial"):
            return "ERROR: scope must be current or initial"
        if scope == "initial" and self.public_environment.stage_index:
            return "ERROR: initial snapshot is stale"
        self.engaged = True
        self._sync()
        payload = self.peek_current_state()
        payload["field_semantics"] = {
            "amount_unit": "All monetary fields are integer USD cents, never dollar floats.",
            "amount_due_minor": "Current payment after fees, discounts and original-order credit.",
            "new_total_minor": "New itinerary fare and taxes before fees and original-order credit.",
            "cumulative_minor": "Original amount already paid plus current payment.",
            "business_stages": "Use advance_business_stage to visit each next stage; query never advances. FINISH requires a current receipt after the final stage.",
        }
        self.observed_action_catalogs.append({"state_version": self.current_version,
                                              "action_catalog": deepcopy(payload["action_catalog"])})
        self.tool_log.append({"tool": "environment_query", "scope": scope,
                              "state_version": self.current_version, "result": deepcopy(payload)})
        return json.dumps(payload, ensure_ascii=False)

    def advance_stage(self, stage=None):
        previous = deepcopy(self.current_state)
        message_count = len(self.public_environment.messages)
        try:
            observation = self.public_environment.advance(stage)
        except (ValueError, TypeError) as error:
            return {"status": "REJECTED", "reason": str(error)}
        self._sync()
        events = deepcopy(observation["messages"][message_count:])
        self.released_events.extend(events)
        if previous["commerce_observation"]["stage"] != observation["stage"]:
            self.state_transitions.append({"previous_facts": previous["facts"],
                                           "current_facts": deepcopy(self.current_state["facts"]),
                                           "state_version": self.current_version, "events": events,
                                           "chain_exhausted": self.chain_exhausted})
        result = {"status": "OBSERVED", "observation": observation, "released_events": events}
        self.tool_log.append({"tool": "advance_business_stage", "stage": stage, "result": deepcopy(result)})
        return result

    def _maybe_release_after_response(self):
        if self.chain_exhausted:
            return None
        return json.dumps(self.advance_stage(), ensure_ascii=False)

    def simulate(self, action_id, parameters):
        synchronize = getattr(self, "before_simulate", None)
        if synchronize:
            synchronize()
        if not isinstance(parameters, dict) or "type" in parameters:
            return "ERROR: parameters must be an object without type; type is supplied by action_id"
        if action_id not in [action["id"] for action in self.current_state["action_catalog"]]:
            return "ERROR: unknown action_id"
        action = {"type": action_id, **deepcopy(parameters)}
        self.boundary_candidate = deepcopy(action)
        self._sync()
        context = self.execution_context()
        for existing in self.receipts:
            if (existing.get("status") == "SIMULATED_EXECUTED"
                    and existing.get("action") == action
                    and existing.get("state_version") == self.current_version
                    and existing.get("stage") == self.public_environment.stages[self.public_environment.stage_index]
                    and existing.get("conversation_sha256") == context["conversation_sha256"]
                    and existing.get("intent_version") == self.intent_version):
                return json.dumps({"receipt": deepcopy(existing), "cached": True}, ensure_ascii=False)
        result = self.public_environment.simulate(action)
        self._sync()
        receipt = {**result, "action_id": action_id, "operation": action_id,
                   "parameters": deepcopy(parameters), "state_version": self.current_version,
                   "conversation_sha256": context["conversation_sha256"],
                   "stage": self.public_environment.stages[self.public_environment.stage_index],
                   "intent_version": self.intent_version}
        if result.get("economics"):
            receipt.update(result["economics"])
        self.receipts.append(deepcopy(receipt))
        self.tool_log.append({"tool": "simulate_transaction", "action_id": action_id,
                              "parameters": deepcopy(parameters), "result": deepcopy(receipt)})
        return json.dumps({"receipt": receipt}, ensure_ascii=False)

    def validate_prediction(self, prediction):
        values = prediction if isinstance(prediction, dict) else vars(prediction)
        behavior = values.get("final_behavior", values.get("behavior"))
        parameters = values.get("parameters")
        if behavior == "EXECUTE" and (not isinstance(parameters, dict) or "type" in parameters):
            return {"status": "REJECTED", "reason": "invalid action parameters"}
        action = {"type": values.get("action_id"), **parameters} if behavior == "EXECUTE" else None
        if not self.chain_exhausted:
            return {"status": "REJECTED", "reason": "business stages incomplete; advance and inspect updates before deciding",
                    "observation": self.public_environment.query()}
        receipt = next((row for row in self.receipts if row.get("receipt_id") == values.get("receipt_id")
                        and row.get("stage") == self.public_environment.stages[self.public_environment.stage_index]
                        and row.get("intent_version") == self.intent_version), None)
        if behavior == "EXECUTE" and receipt is None:
            return {"status": "REJECTED", "reason": "missing current-intent receipt"}
        if behavior not in ("EXECUTE", "BLOCK", "REQUEST_CLARIFICATION"):
            return {"status": "REJECTED", "reason": "unknown behavior"}
        terminal = self.public_environment.terminal
        if terminal and (terminal.get("behavior") != behavior or
                         (behavior == "EXECUTE" and (terminal.get("action") != action or
                          terminal.get("receipt_id") != values.get("receipt_id")))):
            return {"status": "REJECTED", "reason": "terminal decision is frozen"}
        if behavior == "EXECUTE":
            try:
                binding = self.public_environment._binding(action, self.public_environment._calculate(action))
                if values.get("receipt_id") not in self.public_environment.receipts or digest(binding) != values.get("receipt_id"):
                    raise ValueError("missing or stale receipt; obtain a current simulation")
            except (ValueError, KeyError, TypeError) as error:
                return {"status": "REJECTED", "reason": str(error)}
        return {"status": "VALIDATED", "behavior": behavior}

    def finish_prediction(self, prediction):
        validated = self.validate_prediction(prediction)
        if validated["status"] != "VALIDATED":
            self.finish_response = deepcopy(validated)
            return validated
        values = prediction if isinstance(prediction, dict) else vars(prediction)
        behavior = values.get("final_behavior", values.get("behavior"))
        action = {"type": values.get("action_id"), **values["parameters"]} if behavior == "EXECUTE" else None
        result = self.public_environment.finish(action, values.get("receipt_id"), behavior)
        self.finish_response = deepcopy(result)
        self._sync()
        self.tool_log.append({"tool": "finish", "result": deepcopy(result)})
        return result

    def shop_search_products(self, query="", category=None, state_version=None):
        if not self.is_shop:
            return "ERROR: shopping search is unavailable for ticketing"
        if state_version is not None and state_version != self.current_version:
            return "ERROR: stale state_version"
        rows = [row for row in self.public_environment.query()["items"]
                if query.lower() in json.dumps(row, ensure_ascii=False).lower()
                and (category is None or row.get("attributes", {}).get("category") == category)]
        self.observed_products.extend(deepcopy(rows))
        result = {"state_version": self.current_version, "items": sorted(rows, key=lambda row: row["unit_price_minor"])}
        self.tool_log.append({"tool": "shop_search_products", "result": deepcopy(result)})
        return json.dumps(result, ensure_ascii=False)

    def shop_get_product_details(self, sku, state_version=None):
        if state_version is not None and state_version != self.current_version:
            return "ERROR: stale state_version"
        rows = [row for row in self.public_environment.query()["items"] if row["item_id"] == sku]
        if not rows:
            return "ERROR: unknown item_id"
        self.observed_products.extend(deepcopy(rows))
        result = {"state_version": self.current_version, "item": rows[0]}
        self.tool_log.append({"tool": "shop_get_product_details", "result": deepcopy(result)})
        return json.dumps(result, ensure_ascii=False)
