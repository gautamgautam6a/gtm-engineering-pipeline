"""Test the billing SQL in memory only: no disk database or network access."""

import json
from pathlib import Path
import unittest

import duckdb


from model_checks import MODEL_CHECKS


def subscription_item(price_cents, quantity=1, interval="month", interval_count=None):
    recurring = {"interval": interval}
    if interval_count is not None:
        recurring["interval_count"] = interval_count
    item = {"price": {"unit_amount": price_cents, "recurring": recurring}}
    if quantity is not None:
        item["quantity"] = quantity
    return item


class AccountBillingTests(unittest.TestCase):
    def setUp(self):
        self.connection = duckdb.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("SET TimeZone = 'UTC'")
        # These definitions match the actual schema in seed_mock.py.
        self.connection.execute("CREATE TABLE raw_stripe_customers (id VARCHAR, payload JSON)")
        self.connection.execute("CREATE TABLE raw_stripe_subscriptions (id VARCHAR, payload JSON)")
        self.connection.execute("""CREATE TABLE raw_usage_events
            (user_email VARCHAR, event VARCHAR, occurred_at TIMESTAMP)""")
        self.model_sql = (Path(__file__).parent / "Models" / "account_billing.sql").read_text()

    def add_customer(self, customer_id="cus_test", email="billing@acme.io"):
        payload = {"id": customer_id, "name": "Example Company", "email": email}
        self.connection.execute("INSERT INTO raw_stripe_customers VALUES (?, ?)",
                                [customer_id, json.dumps(payload)])

    def add_subscription(self, items, status="active", customer_id="cus_test", subscription_id="sub_test"):
        payload = {"id": subscription_id, "customer": customer_id,
                   "status": status, "items": {"data": items}}
        self.connection.execute("INSERT INTO raw_stripe_subscriptions VALUES (?, ?)",
                                [subscription_id, json.dumps(payload)])

    def build_model(self):
        self.connection.execute("CREATE OR REPLACE TABLE account_billing AS " + self.model_sql)
        result = self.connection.execute("SELECT * FROM account_billing ORDER BY stripe_customer_id")
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]

    def assert_checks_pass(self):
        for name, sql in MODEL_CHECKS.items():
            with self.subTest(check=name):
                self.assertEqual(self.connection.execute(sql).fetchall(), [])

    def test_quantity_statuses_and_intervals(self):
        cases = [
            ("monthly", "active", subscription_item(4900, 3), 14700),
            ("past_due", "past_due", subscription_item(190000, 3, "year"), 47500),
            ("annual", "active", subscription_item(190000, 1, "year"), 15833),
            ("quarter", "active", subscription_item(30000, 2, "month", 3), 20000),
            ("two_year", "active", subscription_item(240000, 1, "year", 2), 10000),
            ("default_qty", "active", subscription_item(4900, None), 4900),
        ]
        for status in ("canceled", "trialing", "unpaid", "incomplete", "incomplete_expired", "paused"):
            cases.append((status, status, subscription_item(4900, 3), 0))
        for customer_id, status, item, expected in cases:
            self.add_customer(customer_id)
            self.add_subscription([item], status, customer_id, "sub_" + customer_id)
        rows = {row["stripe_customer_id"]: row for row in self.build_model()}
        for customer_id, status, item, expected in cases:
            with self.subTest(customer_id=customer_id):
                self.assertEqual(rows[customer_id]["mrr_cents"], expected)
                self.assertIsInstance(rows[customer_id]["mrr_cents"], int)
                self.assertEqual(rows[customer_id]["subscription_status"], status)
        self.assertEqual(self.connection.execute(
            "SELECT typeof(mrr_cents) FROM account_billing LIMIT 1").fetchone()[0], "BIGINT")
        self.assert_checks_pass()

    def test_multiple_items_and_subscriptions(self):
        self.add_customer()
        self.add_subscription([subscription_item(4900, 3), subscription_item(12000, 1, "year")])
        self.add_subscription([subscription_item(6000)], "past_due", subscription_id="sub_due")
        self.add_subscription([subscription_item(999999)], "canceled", subscription_id="sub_old")
        rows = self.build_model()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mrr_cents"], 21700)
        self.assertEqual(rows[0]["subscription_status"], "active")
        self.assert_checks_pass()

    def test_round_once_after_summing_items(self):
        self.add_customer()
        self.add_subscription([subscription_item(100, 1, "month", 3)] * 3)
        self.assertEqual(self.build_model()[0]["mrr_cents"], 100)
        self.assert_checks_pass()

    def test_customers_without_subscriptions_and_empty_items(self):
        self.add_customer("cus_none")
        self.add_customer("cus_empty")
        self.add_subscription([], "canceled", "cus_empty")
        rows = {row["stripe_customer_id"]: row for row in self.build_model()}
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows["cus_none"]["subscription_status"], "none")
        self.assertEqual(rows["cus_empty"]["subscription_status"], "canceled")
        for row in rows.values():
            self.assertEqual(row["mrr_cents"], 0)
            self.assertEqual(row["active_users_30d"], 0)
            self.assertIsNone(row["last_event_at"])
        self.assert_checks_pass()

    def test_usage_and_company_domains(self):
        self.add_customer(email="Billing@ACME.IO")
        self.add_customer("cus_free", "billing@GMAIL.COM")
        for email, days_ago in [("User@acme.io", 1), ("user@ACME.IO", 2),
                                ("second@acme.io", 29), ("old@acme.io", 31),
                                ("other@globex.com", 1), ("person@gmail.com", 1)]:
            self.connection.execute("""INSERT INTO raw_usage_events
                VALUES (?, 'login', current_timestamp - ? * INTERVAL 1 DAY)""",
                [email, days_ago])
        rows = {row["stripe_customer_id"]: row for row in self.build_model()}
        self.assertEqual(rows["cus_test"]["company_domain"], "acme.io")
        self.assertEqual(rows["cus_test"]["active_users_30d"], 2)
        self.assertIsNone(rows["cus_free"]["company_domain"])
        expected_last_event = self.connection.execute("""SELECT
            strftime(max(occurred_at), '%Y-%m-%dT%H:%M:%SZ') FROM raw_usage_events
            WHERE lower(split_part(user_email, '@', 2)) = 'acme.io'""").fetchone()[0]
        self.assertEqual(rows["cus_test"]["last_event_at"], expected_last_event)
        self.assert_checks_pass()

    def test_cancellation_changes_exactly_one_customer(self):
        for customer_id in ("cus_one", "cus_two"):
            self.add_customer(customer_id)
            self.add_subscription([subscription_item(4900)], customer_id=customer_id,
                                  subscription_id="sub_" + customer_id)
        before = {row["stripe_customer_id"]: row for row in self.build_model()}
        self.connection.execute("""UPDATE raw_stripe_subscriptions
            SET payload = json_merge_patch(payload, '{"status": "canceled"}')
            WHERE id = 'sub_cus_one'""")
        after = {row["stripe_customer_id"]: row for row in self.build_model()}
        self.assertEqual([key for key in before if before[key] != after[key]], ["cus_one"])
        self.assertEqual(before["cus_one"]["mrr_cents"], 4900)
        self.assertEqual(after["cus_one"]["mrr_cents"], 0)
        self.assert_checks_pass()

    def test_checks_detect_bad_results(self):
        self.add_customer()
        self.add_subscription([subscription_item(4900)])
        self.build_model()
        self.assert_checks_pass()
        self.connection.execute("INSERT INTO account_billing SELECT * FROM account_billing")
        self.assertTrue(self.connection.execute(MODEL_CHECKS["unique keys"]).fetchall())
        for invalid_mrr in (-1, None):
            self.connection.execute("UPDATE account_billing SET mrr_cents = ?", [invalid_mrr])
            self.assertTrue(self.connection.execute(MODEL_CHECKS["nonnegative MRR"]).fetchall())
        self.connection.execute("UPDATE account_billing SET mrr_cents = 0")
        self.assertTrue(self.connection.execute(MODEL_CHECKS["positive active MRR"]).fetchall())

    def test_unsupported_interval_is_zero_and_flagged_when_active(self):
        self.add_customer()
        self.add_subscription([subscription_item(4900, interval="week")])
        self.assertEqual(self.build_model()[0]["mrr_cents"], 0)
        self.assertTrue(self.connection.execute(MODEL_CHECKS["positive active MRR"]).fetchall())


if __name__ == "__main__":
    unittest.main(verbosity=2)
