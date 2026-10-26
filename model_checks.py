"""Shared SQL checks. A healthy billing model returns no rows for each query."""

MODEL_CHECKS = {
    "unique keys": """SELECT stripe_customer_id, count(*) FROM account_billing
        GROUP BY stripe_customer_id HAVING count(*) > 1""",
    "nonnegative MRR": """SELECT * FROM account_billing
        WHERE mrr_cents IS NULL OR mrr_cents < 0""",
    "positive active MRR": """SELECT * FROM account_billing
        WHERE subscription_status = 'active' AND mrr_cents = 0""",
    "present keys": """SELECT * FROM account_billing
        WHERE stripe_customer_id IS NULL OR trim(stripe_customer_id) = ''""",
}
