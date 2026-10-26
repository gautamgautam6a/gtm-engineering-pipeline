"""Create fake Stripe and product usage data in DuckDB."""

import json
import random
from datetime import datetime, timedelta, timezone

import duckdb

random.seed(7)
now = datetime.now(timezone.utc)
con = duckdb.connect("gtm.duckdb")

con.execute("create or replace table raw_stripe_customers (id varchar, payload json)")
con.execute("create or replace table raw_stripe_subscriptions (id varchar, payload json)")
con.execute("create or replace table raw_usage_events (user_email varchar, event varchar, occurred_at timestamp)")

domains = ["acme.io", "globex.com", "initech.co", "umbrella.dev", "gmail.com", "stark.ai"]
statuses = ["active", "active", "active", "past_due", "trialing", "canceled"]
plans = [("month", 4900), ("month", 19900), ("year", 190000)]

for i, (domain, status) in enumerate(zip(domains, statuses), 1):
    cid = f"cus_mock{i:03d}"
    customer = {
        "id": cid,
        "email": f"billing@{domain}",
        "name": domain.split(".")[0].title()
    }

    interval, amount = random.choice(plans)
    item = {
        "quantity": random.choice([1, 3]),
        "price": {
            "unit_amount": amount,
            "recurring": {"interval": interval}
        }
    }

    sub = {
        "id": f"sub_mock{i:03d}",
        "customer": cid,
        "status": status,
        "items": {"data": [item]}
    }

    con.execute(
        "insert into raw_stripe_customers values (?, ?)",
        [cid, json.dumps(customer)]
    )
    con.execute(
        "insert into raw_stripe_subscriptions values (?, ?)",
        [sub["id"], json.dumps(sub)]
    )

    for user in range(random.randint(1, 5)):
        for _ in range(random.randint(1, 8)):
            when = now - timedelta(
                days=random.randint(0, 60),
                hours=random.randint(0, 23)
            )
            con.execute(
                "insert into raw_usage_events values (?, ?, ?)",
                [
                    f"user{user}@{domain}",
                    random.choice(["login", "export", "invite"]),
                    when.replace(tzinfo=None)
                ]
            )

print("seeded", len(domains), "customers")