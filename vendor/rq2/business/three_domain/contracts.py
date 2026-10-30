"""Public, gold-free schema and typed quantities for the development adapter."""

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json


def integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def validate_world(spec):
    allowed = {"domain", "start_at", "currency", "policy", "accounts", "items", "flights",
               "orders", "initial_messages", "events"}
    if set(spec) - allowed:
        raise ValueError(f"unknown public fields: {sorted(set(spec) - allowed)}")
    digest(spec)
    if spec["domain"] not in ("Shopping", "Ticketing", "ecommerce-payment", "travel-ticket-payment"):
        raise ValueError("unsupported domain; legacy DeFi uses its existing adapter")
    if spec["currency"] != "USD":
        raise ValueError("this pilot supports USD integer cents only")
    timestamp(spec["start_at"])
    policy = spec["policy"]
    allowed_policy = {"stages", "max_simulations", "shipping_minor", "booking_fee_minor", "change_fee_minor", "action_space",
                      "discount_bps", "discount_min_subtotal_minor", "shipping_tiers", "service_fee_per_passenger_minor"}
    if set(policy) - allowed_policy:
        raise ValueError("unsupported public policy")
    stages = policy.get("stages", ["start", "review", "finish"])
    if not isinstance(stages, list) or len(stages) < 2 or stages[0] != "start" or stages[-1] != "finish" or len(set(stages)) != len(stages):
        raise ValueError("stages must be unique and run start to finish")
    for stage in stages:
        identifier(stage, "stage")
    integer(policy.get("max_simulations", 20), "max_simulations", 1)
    for field in ("shipping_minor", "booking_fee_minor", "change_fee_minor", "discount_min_subtotal_minor", "service_fee_per_passenger_minor"):
        integer(policy.get(field, 0), field)
    if integer(policy.get("discount_bps", 0), "discount_bps") > 10000:
        raise ValueError("discount_bps exceeds 10000")
    tiers = policy.get("shipping_tiers", [])
    if not isinstance(tiers, list):
        raise ValueError("shipping_tiers must be an array")
    thresholds = set()
    for tier in tiers:
        if not isinstance(tier, dict) or set(tier) != {"min_subtotal_minor", "shipping_minor"}:
            raise ValueError("invalid shipping tier")
        threshold = integer(tier["min_subtotal_minor"], "min_subtotal_minor")
        integer(tier["shipping_minor"], "shipping_minor")
        if threshold in thresholds:
            raise ValueError("duplicate shipping threshold")
        thresholds.add(threshold)
    space = policy.get("action_space", {})
    if not isinstance(space, dict) or set(space) - {"quantities", "passenger_groups", "flight_routes", "action_types"}:
        raise ValueError("invalid finite action space")
    for name, values in space.items():
        if not isinstance(values, list) or not values or len({digest(value) for value in values}) != len(values):
            raise ValueError("action space axes must be nonempty unique arrays")
    if "action_types" in space:
        supported = {"purchase"} if spec["domain"] in ("Shopping", "ecommerce-payment") else {"book_flight", "change_flight"}
        if any(not isinstance(value, str) or value not in supported for value in space["action_types"]):
            raise ValueError("unsupported action type for domain")
    for quantity in space.get("quantities", []):
        integer(quantity, "quantity", 1)
    for name in ("passenger_groups", "flight_routes"):
        for values in space.get(name, []):
            unique_strings(values, name)
    collection_fields = {
        "accounts": ({"account_id", "balance_minor", "limit_minor", "authorized", "authorization_version", "owner_id", "authorization_expires_at", "allowed_actions"}, "account_id"),
        "items": ({"item_id", "unit_price_minor", "stock", "attributes", "quote_version", "quote_expires_at"}, "item_id"),
        "flights": ({"flight_id", "origin", "destination", "departure_at", "arrival_at", "fare_minor", "tax_minor", "seats", "attributes", "quote_version", "quote_expires_at"}, "flight_id"),
        "orders": ({"order_id", "owner_id", "passenger_ids", "flight_ids", "paid_minor", "credit_minor"}, "order_id"),
    }
    indexed = {}
    for collection, (fields, key) in collection_fields.items():
        rows = spec.get(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"{collection} must be an array")
        indexed[collection] = {}
        for row in rows:
            if set(row) - fields:
                raise ValueError(f"unsupported {collection} field")
            identity = identifier(row[key], key)
            if identity in indexed[collection]:
                raise ValueError(f"duplicate {key}")
            indexed[collection][identity] = row
            for name, value in row.items():
                if name.endswith("_minor") or name in ("stock", "seats"):
                    integer(value, name)
                if name.endswith("_at"):
                    timestamp(value)
            if collection == "accounts":
                for name in ("balance_minor", "limit_minor", "authorization_version"):
                    integer(row[name], name, 1 if name == "authorization_version" else 0)
                identifier(row["owner_id"], "owner_id")
                if type(row["authorized"]) is not bool:
                    raise ValueError("authorized must be boolean")
                if "allowed_actions" in row and (not isinstance(row["allowed_actions"], list) or any(not isinstance(value, str) or value not in ("purchase", "book_flight", "change_flight") for value in row["allowed_actions"])):
                    raise ValueError("invalid authorization action scope")
            if collection in ("items", "flights"):
                integer(row.get("quote_version", 1), "quote_version", 1)
                if not isinstance(row.get("attributes", {}), dict):
                    raise ValueError("attributes must be an object")
            if collection == "items":
                integer(row["unit_price_minor"], "unit_price_minor")
                integer(row["stock"], "stock")
            if collection == "flights":
                for name in ("fare_minor", "tax_minor", "seats"):
                    integer(row[name], name)
                for name in ("origin", "destination"):
                    identifier(row[name], name)
                if row["origin"] == row["destination"] or timestamp(row["departure_at"]) >= timestamp(row["arrival_at"]):
                    raise ValueError("invalid flight route or chronology")
    for order in indexed["orders"].values():
        identifier(order["owner_id"], "owner_id")
        unique_strings(order["passenger_ids"], "passenger_ids")
        unique_strings(order["flight_ids"], "flight_ids")
        if not set(order["flight_ids"]) <= indexed["flights"].keys():
            raise ValueError("order references missing flights")
        integer(order["paid_minor"], "paid_minor")
        integer(order["credit_minor"], "credit_minor")
        if order["credit_minor"] > order["paid_minor"]:
            raise ValueError("credit exceeds original paid amount")
    for route in space.get("flight_routes", []):
        if len(route) > 2 or not set(route) <= indexed["flights"].keys():
            raise ValueError("invalid finite flight route")
    return deepcopy(spec)


def unique_strings(values, name):
    if not isinstance(values, list) or not values:
        raise ValueError(f"{name} must be a nonempty array")
    for value in values:
        identifier(value, name)
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {name}")


def validate_messages(messages):
    for message in messages:
        if set(message) != {"message_id", "role", "content"}:
            raise ValueError("messages require exactly message_id, role, content")
        identifier(message["message_id"], "message_id")
        identifier(message["content"], "content")
        if message["role"] not in ("user", "environment"):
            raise ValueError("invalid public message role")
