"""Lesson schema and request bodies only. This module makes no HTTP requests."""

from datetime import datetime
import re

from source_safety import check_source_row_count

def text_property(name, label, **extra):
    return {"name": name, "label": label, "type": "string", "fieldType": "text", **extra}


def number_property(name, label):
    return {"name": name, "label": label, "type": "number", "fieldType": "number"}


# Exact schema body from the lesson, for review only; nothing creates it remotely.
BILLING_ACCOUNT_SCHEMA = {
    "name": "billing_account",
    "labels": {"singular": "Billing account", "plural": "Billing accounts"},
    "primaryDisplayProperty": "customer_name",
    "requiredProperties": ["stripe_customer_id"],
    "searchableProperties": ["stripe_customer_id", "customer_name", "company_domain"],
    "associatedObjects": ["0-2"],
    "properties": [
        text_property("stripe_customer_id", "Stripe customer ID", hasUniqueValue=True),
        text_property("customer_name", "Customer name"),
        text_property("company_domain", "Company domain"),
        number_property("mrr_cents", "MRR (cents)"),
        text_property("subscription_status", "Subscription status"),
        number_property("active_users_30d", "Active users (30d)"),
        {"name": "last_event_at", "label": "Last product event", "type": "datetime", "fieldType": "date"},
    ],
}

DESTINATION_COLUMNS = (
    "stripe_customer_id", "customer_name", "company_domain", "mrr_cents",
    "subscription_status", "active_users_30d", "last_event_at",
)


def destination_values(row):
    """Keep typed values for comparison; serialization happens only at the edge."""
    missing = set(DESTINATION_COLUMNS) - row.keys()
    if missing:
        raise ValueError(f"Missing destination columns: {', '.join(sorted(missing))}")
    identifier = row["stripe_customer_id"]
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("Missing or blank stripe_customer_id; cannot upsert safely.")
    customer_name = row["customer_name"]
    if not isinstance(customer_name, str) or not customer_name.strip():
        raise ValueError("Missing or blank customer_name; cannot upsert safely.")
    for field in ("mrr_cents", "active_users_30d"):
        if type(row[field]) is not int or row[field] < 0:
            raise ValueError(f"{field} must be a nonnegative integer.")
    for field in ("customer_name", "company_domain", "subscription_status", "last_event_at"):
        if row[field] is not None and not isinstance(row[field], str):
            raise ValueError(f"{field} must be text or null.")
    timestamp = row["last_event_at"]
    if timestamp is not None:
        # Require a full date/time and explicit UTC, not a date or local time.
        # Parse for calendar validity, but return the original string unchanged.
        utc_pattern = (r"[0-9]{4}-[0-9]{2}-[0-9]{2}T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
                       r"(?:\.[0-9]+)?(?:Z|\+00:00)")
        if not re.fullmatch(utc_pattern, timestamp):
            raise ValueError("last_event_at must be a valid UTC ISO 8601 datetime or null.")
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            raise ValueError("last_event_at must be a valid UTC ISO 8601 datetime or null.") from None
    return {field: row[field] for field in DESTINATION_COLUMNS if field != "stripe_customer_id"}


def to_upsert_input(row):
    """Lesson mapping: properties are strings, null properties are omitted."""
    values = destination_values(row)
    properties = {"stripe_customer_id": row["stripe_customer_id"]}
    properties.update({name: str(value) for name, value in values.items() if value is not None})
    return {"idProperty": "stripe_customer_id", "id": row["stripe_customer_id"],
            "properties": properties}


def plan_upsert_batches(rows, previous_state):
    """Validate all rows first; select NEW/CHANGED rows in batches of at most 100.

    Local NEW/CHANGED describes our memory, not proof of remote existence.
    HubSpot will decide create versus update using the same unique external key.
    """
    check_source_row_count(len(rows), len(previous_state))
    counts = {"NEW": 0, "CHANGED": 0, "UNCHANGED": 0}
    pending = []
    seen = set()
    for row in rows:
        values = destination_values(row)
        identifier = row["stripe_customer_id"]
        if identifier in seen:
            raise ValueError(f"Duplicate stripe_customer_id: {identifier!r}")
        seen.add(identifier)
        if identifier not in previous_state:
            classification = "NEW"
        elif previous_state[identifier] != values:
            classification = "CHANGED"
        else:
            classification = "UNCHANGED"
        counts[classification] += 1
        if classification != "UNCHANGED":
            pending.append(to_upsert_input(row))
    batches = [{"inputs": pending[index:index + 100]} for index in range(0, len(pending), 100)]
    return counts, batches
