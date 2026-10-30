"""Method-neutral JSON tool bridge; production registration remains runner-owned."""

import json

from .environment import PublicEnvironment


class PublicToolAdapter:
    def __init__(self, public_spec):
        self.environment = PublicEnvironment(public_spec)

    def environment_query(self, scope="current"):
        if scope != "current":
            return {"status": "REJECTED", "reason": "only current public observations are available"}
        return self.environment.query()

    def simulate_transaction(self, action):
        return self.environment.simulate(action)

    def advance_business_stage(self, stage):
        return self.environment.advance(stage)

    def finish(self, action=None, receipt_id=None, behavior="EXECUTE"):
        return self.environment.finish(action, receipt_id, behavior)

    def call(self, name, arguments):
        tools = {"environment_query": self.environment_query,
                 "simulate_transaction": self.simulate_transaction,
                 "advance_business_stage": self.advance_business_stage,
                 "FINISH": self.finish}
        if name not in tools:
            return {"status": "REJECTED", "reason": "unknown public tool"}
        try:
            if isinstance(arguments, str):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            return tools[name](**arguments)
        except (ValueError, TypeError, KeyError) as error:
            return {"status": "REJECTED", "reason": str(error)}

    def reset(self):
        return self.environment.reset()

    @staticmethod
    def tool_definitions():
        return [
            {"name": "environment_query", "description": "Read current public world and released messages; no clock advance.",
             "parameters": {"type": "object", "properties": {"scope": {"enum": ["current"]}}, "additionalProperties": False}},
            {"name": "simulate_transaction", "description": "Simulate a complete action without money, stock or seat side effects.",
             "parameters": {"type": "object", "properties": {"action": {"type": "object"}}, "required": ["action"], "additionalProperties": False}},
            {"name": "advance_business_stage", "description": "Advance to the immediately next stage declared in public policy.",
             "parameters": {"type": "object", "properties": {"stage": {"type": "string"}}, "required": ["stage"], "additionalProperties": False}},
            {"name": "FINISH", "description": "Apply all remaining public stages then validate final declaration and current receipt.",
             "parameters": {"type": "object", "properties": {"action": {"type": "object"}, "receipt_id": {"type": "string"},
                 "behavior": {"enum": ["EXECUTE", "BLOCK", "REQUEST_CLARIFICATION"]}}, "required": ["behavior"], "additionalProperties": False}},
        ]
