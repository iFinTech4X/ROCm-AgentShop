"""Public simulated commerce ledger. No hidden references or model-specific hooks."""

from copy import deepcopy

from .contracts import digest, integer, timestamp, unique_strings, validate_messages, validate_world


class PublicEnvironment:
    def __init__(self, spec):
        self._initial = validate_world(spec)
        self.stages = spec["policy"].get("stages", ["start", "review", "finish"])
        self.initial_state_hash = digest(self._initial)
        self._validate_schedule()
        self.reset()

    def _validate_schedule(self):
        world = deepcopy(self._initial)
        messages = list(world.get("initial_messages", []))
        validate_messages(messages)
        seen = set()
        current_time = timestamp(world["start_at"])
        events = world.get("events", [])
        for event in events:
            if set(event) - {"event_id", "stage", "priority", "changes", "messages", "at"}:
                raise ValueError("unsupported event field")
            if event["event_id"] in seen or event["stage"] not in self.stages:
                raise ValueError("duplicate event or unknown stage")
            seen.add(event["event_id"])
            integer(event.get("priority", 0), "priority")
            validate_messages(event.get("messages", []))
        for event in self._ordered_events():
            if "at" in event:
                event_time = timestamp(event["at"])
                if event_time < current_time:
                    raise ValueError("event time goes backwards")
                current_time = event_time
            self._apply_changes(world, event)
            validate_world(world)
            messages.extend(event.get("messages", []))
        if len({message["message_id"] for message in messages}) != len(messages):
            raise ValueError("duplicate public message IDs")

    def _ordered_events(self):
        return sorted(self._initial.get("events", []), key=lambda event: (
            self.stages.index(event["stage"]), event.get("priority", 0), event["event_id"]))

    @staticmethod
    def _apply_changes(world, event):
        keys = {"items": "item_id", "flights": "flight_id", "accounts": "account_id", "orders": "order_id"}
        for change in event.get("changes", []):
            if set(change) != {"collection", "id", "patch"} or change["collection"] not in keys:
                raise ValueError("invalid event change")
            key = keys[change["collection"]]
            rows = [row for row in world.get(change["collection"], []) if row[key] == change["id"]]
            if len(rows) != 1 or key in change["patch"]:
                raise ValueError("event must patch an existing stable identity")
            row = rows[0]
            patch = deepcopy(change["patch"])
            for name in ("authorization_version", "quote_version"):
                if name in patch:
                    integer(patch[name], name, 1)
            if change["collection"] == "accounts" and set(patch) & {"authorized", "allowed_actions", "authorization_expires_at", "owner_id"}:
                patch["authorization_version"] = max(patch.get("authorization_version", 0), row["authorization_version"] + 1)
            if change["collection"] in ("items", "flights"):
                patch["quote_version"] = max(patch.get("quote_version", 0), row.get("quote_version", 1) + 1)
            row.update(patch)

    def reset(self):
        self.world = deepcopy(self._initial)
        self.stage_index = 0
        self.state_version = 1
        self.now = self.world["start_at"]
        self.messages = deepcopy(self.world.get("initial_messages", []))
        self.receipts = {}
        self.trace = []
        self.applied_events = set()
        self.simulation_calls = 0
        self.terminal = None
        self._release()
        return self.query()

    def _release(self):
        for event in self._ordered_events():
            if event["stage"] == self.stages[self.stage_index] and event["event_id"] not in self.applied_events:
                previous = self.state_version
                self._apply_changes(self.world, event)
                self.now = event.get("at", self.now)
                self.messages.extend(deepcopy(event.get("messages", [])))
                self.state_version += 1
                self.applied_events.add(event["event_id"])
                self.trace.append({"kind": "event", "event_id": event["event_id"], "stage": event["stage"],
                                   "pre_version": previous, "post_version": self.state_version,
                                   "at": self.now, "messages": deepcopy(event.get("messages", []))})

    def query(self):
        return deepcopy({"domain": self.world["domain"], "currency": self.world["currency"],
                         "policy": self.world["policy"], "stage": self.stages[self.stage_index],
                         "now": self.now, "state_version": self.state_version,
                         "accounts": self.world.get("accounts", []), "items": self.world.get("items", []),
                         "flights": self.world.get("flights", []), "orders": self.world.get("orders", []),
                         "messages": self.messages, "terminal": self.terminal})

    def advance(self, stage=None):
        if self.terminal:
            return self.query()
        requested = self.stage_index + 1 if stage is None else self.stages.index(stage)
        if requested == self.stage_index:
            return self.query()
        if requested != self.stage_index + 1 or requested >= len(self.stages):
            raise ValueError("advance accepts only the immediately next public stage")
        self.stage_index = requested
        self._release()
        return self.query()

    def _find(self, collection, identity, key):
        for row in self.world.get(collection, []):
            if row[key] == identity:
                return row
        raise ValueError(f"unknown {key}")

    def _calculate(self, action):
        kind = action["type"]
        account = self._find("accounts", action["account_id"], "account_id")
        if not account["authorized"] or kind not in account.get("allowed_actions", ["purchase", "book_flight", "change_flight"]):
            raise ValueError("payment authorization unavailable")
        if "authorization_expires_at" in account and timestamp(self.now) >= timestamp(account["authorization_expires_at"]):
            raise ValueError("authorization expired")
        policy = self.world["policy"]
        space = policy.get("action_space", {})
        if "action_types" in space and kind not in space["action_types"]:
            raise ValueError("action outside public finite action space")
        credit = paid = 0
        if kind == "purchase" and self.world["domain"] in ("Shopping", "ecommerce-payment"):
            if set(action) != {"type", "item_id", "quantity", "account_id"}:
                raise ValueError("purchase requires exact complete parameters")
            quantity = integer(action["quantity"], "quantity", 1)
            if "quantities" in space and quantity not in space["quantities"]:
                raise ValueError("quantity outside public finite action space")
            item = self._find("items", action["item_id"], "item_id")
            if quantity > item["stock"]:
                raise ValueError("insufficient stock")
            base = item["unit_price_minor"] * quantity
            if base >= policy.get("discount_min_subtotal_minor", 0):
                base -= base * policy.get("discount_bps", 0) // 10000
            fee = policy.get("shipping_minor", 0)
            for tier in sorted(policy.get("shipping_tiers", []), key=lambda entry: entry["min_subtotal_minor"]):
                if base >= tier["min_subtotal_minor"]:
                    fee = tier["shipping_minor"]
            quotes = [item]
        elif kind in ("book_flight", "change_flight") and self.world["domain"] in ("Ticketing", "travel-ticket-payment"):
            fields = {"type", "flight_ids", "passenger_ids", "account_id"}
            if kind == "change_flight":
                fields.add("order_id")
            if set(action) != fields:
                raise ValueError("flight action requires exact complete parameters")
            unique_strings(action["flight_ids"], "flight_ids")
            unique_strings(action["passenger_ids"], "passenger_ids")
            if "flight_routes" in space and action["flight_ids"] not in space["flight_routes"]:
                raise ValueError("route outside public finite action space")
            if "passenger_groups" in space and action["passenger_ids"] not in space["passenger_groups"]:
                raise ValueError("passengers outside public finite action space")
            if len(action["flight_ids"]) > 2:
                raise ValueError("only one-way or round-trip travel is supported")
            quotes = [self._find("flights", identity, "flight_id") for identity in action["flight_ids"]]
            count = len(action["passenger_ids"])
            for flight in quotes:
                if count > flight["seats"] or timestamp(self.now) >= timestamp(flight["departure_at"]):
                    raise ValueError("flight unavailable")
            if len(quotes) == 2 and (quotes[0]["destination"] != quotes[1]["origin"] or quotes[1]["destination"] != quotes[0]["origin"] or timestamp(quotes[0]["arrival_at"]) >= timestamp(quotes[1]["departure_at"])):
                raise ValueError("invalid round-trip itinerary")
            base = sum((flight["fare_minor"] + flight["tax_minor"]) * count for flight in quotes)
            fee = policy.get("booking_fee_minor", 0)
            if kind == "change_flight":
                order = self._find("orders", action["order_id"], "order_id")
                if order["owner_id"] != account["owner_id"] or action["passenger_ids"] != order["passenger_ids"]:
                    raise ValueError("original order ownership or passenger mismatch")
                if len(action["flight_ids"]) != len(order["flight_ids"]):
                    raise ValueError("change must preserve explicit full itinerary shape")
                credit, paid = order["credit_minor"], order["paid_minor"]
                fee = policy.get("change_fee_minor", 0)
            fee += count * policy.get("service_fee_per_passenger_minor", 0)
        else:
            raise ValueError("unsupported domain action")
        for quote in quotes:
            if "quote_expires_at" in quote and timestamp(self.now) >= timestamp(quote["quote_expires_at"]):
                raise ValueError("quote expired; query or wait for a fresh public quote")
        due = base + fee - credit
        if due <= 0:
            raise ValueError("only positive payments are supported")
        if due > account["balance_minor"] or due > account["limit_minor"]:
            raise ValueError("public balance or transaction limit exceeded")
        return {"currency": self.world["currency"], "unit": "minor", "base_minor": base,
                "fee_minor": fee, "credit_minor": credit, "amount_due_minor": due,
                "new_total_minor": base, "cumulative_minor": paid + due,
                "authorization_version": account["authorization_version"],
                "quote_versions": [quote.get("quote_version", 1) for quote in quotes]}

    def _binding(self, action, economics):
        return {"action": deepcopy(action), "economics": economics, "state_version": self.state_version,
                "conversation_hash": digest(self.messages), "now": self.now,
                "initial_state_hash": self.initial_state_hash}

    def simulate(self, action):
        if self.terminal:
            return {"status": "TERMINAL_FROZEN"}
        if self.simulation_calls >= self.world["policy"].get("max_simulations", 20):
            return {"status": "RUN_LIMIT", "reason": "simulation budget exhausted"}
        self.simulation_calls += 1
        try:
            binding = self._binding(action, self._calculate(action))
            receipt_id = digest(binding)
            receipt = {"status": "SIMULATED_EXECUTED", "receipt_id": receipt_id, **binding}
            self.receipts[receipt_id] = deepcopy(receipt)
        except (ValueError, KeyError, TypeError) as error:
            receipt = {"status": "REJECTED", "reason": str(error)}
        self.trace.append({"kind": "simulate", "action": deepcopy(action), "result": deepcopy(receipt)})
        return deepcopy(receipt)

    def finish(self, action=None, receipt_id=None, behavior="EXECUTE"):
        if self.terminal:
            return deepcopy(self.terminal)
        while self.stage_index < len(self.stages) - 1:
            self.advance()
        if behavior not in ("EXECUTE", "BLOCK", "REQUEST_CLARIFICATION"):
            return {"status": "REJECTED", "reason": "unknown behavior"}
        if behavior != "EXECUTE":
            self.terminal = {"status": "DECLARED", "behavior": behavior, "state_version": self.state_version,
                             "note": "nonexecution declaration requires independent intent evaluation"}
        else:
            try:
                binding = self._binding(action, self._calculate(action))
                if receipt_id not in self.receipts or receipt_id != digest(binding):
                    raise ValueError("missing or stale receipt; obtain a current simulation")
            except (ValueError, KeyError, TypeError) as error:
                result = {"status": "REJECTED", "reason": str(error), "observation": self.query()}
                self.trace.append({"kind": "finish", "result": deepcopy(result)})
                return result
            self.terminal = {"status": "ACCEPTED", "behavior": behavior, "receipt_id": receipt_id,
                             "state_version": self.state_version, "action": deepcopy(action),
                             "economics": binding["economics"], "now": self.now}
        self.trace.append({"kind": "finish", "result": deepcopy(self.terminal)})
        return deepcopy(self.terminal)
