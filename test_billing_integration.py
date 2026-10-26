"""Exercise the runner and reverse ETL without touching the project database."""

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import duckdb

import reverse_etl
import run_model
import test_account_billing as billing_fixtures

subscription_item = billing_fixtures.subscription_item


class BillingIntegrationTests(unittest.TestCase):
    # Reuse the raw-schema fixture helpers, without inheriting their test cases.
    setUp = billing_fixtures.AccountBillingTests.setUp
    add_customer = billing_fixtures.AccountBillingTests.add_customer
    add_subscription = billing_fixtures.AccountBillingTests.add_subscription

    def test_runner_requires_explicit_apply(self):
        with patch("sys.argv", ["run_model.py"]), patch.object(duckdb, "connect") as connect:
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(run_model.main(), 1)
            connect.assert_not_called()
            self.assertIn("No changes made", output.getvalue())

    def test_runner_materializes_and_checks_new_schema(self):
        self.add_customer()
        self.add_subscription([subscription_item(4900, 3)])
        with redirect_stdout(StringIO()) as output:
            run_model.materialize_model(self.connection)
        self.assertIn("PASS", output.getvalue())
        self.assertEqual(self.connection.execute("""SELECT stripe_customer_id,
            customer_name, mrr_cents FROM account_billing""").fetchone(),
            ("cus_test", "Example Company", 14700))

    def test_runner_rolls_back_failed_quality_check(self):
        self.add_customer()
        self.add_subscription([subscription_item(4900)])
        run_model.materialize_model(self.connection)
        before = self.connection.execute("SELECT * FROM account_billing").fetchall()
        self.connection.execute("""UPDATE raw_stripe_subscriptions
            SET payload = json_merge_patch(payload,
            '{"items":{"data":[{"quantity":1,"price":{"unit_amount":0,"recurring":{"interval":"month"}}}]}}')""")
        with self.assertRaisesRegex(ValueError, "positive active MRR"):
            run_model.materialize_model(self.connection)
        self.assertEqual(self.connection.execute("SELECT * FROM account_billing").fetchall(), before)

    def test_old_dollar_state_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "legacy"):
            reverse_etl.validate_state({"cus_test": {
                "account_name": "Example", "subscription_status": "active", "mrr": "49.00"}})

    def test_cents_are_strict_integers(self):
        for value in (49.0, "4900", True, -1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                reverse_etl.comparable_values(reverse_etl.simulated_record("cus_test", "Example", "active", value))
        self.assertEqual(reverse_etl.comparable_values(
            reverse_etl.simulated_record("cus_test", "Example", "active", 4900))["mrr_cents"], 4900)

    def test_temporary_database_preview_and_retry_workflow(self):
        # Real DuckDB file, but only inside this disposable test directory.
        with tempfile.TemporaryDirectory(prefix=".billing_test_", dir=Path(__file__).parent) as folder:
            database_path = Path(folder) / "test.duckdb"
            state_path = Path(folder) / "test_state.json"
            self.add_customer()
            self.add_subscription([subscription_item(4900)])
            # Copy the isolated raw fixtures into a temporary disk database.
            disk = duckdb.connect(str(database_path))
            try:
                for table in ("raw_stripe_customers", "raw_stripe_subscriptions", "raw_usage_events"):
                    if table == "raw_usage_events":
                        disk.execute("CREATE TABLE raw_usage_events (user_email VARCHAR, event VARCHAR, occurred_at TIMESTAMP)")
                    else:
                        disk.execute(f"CREATE TABLE {table} (id VARCHAR, payload JSON)")
                        rows = self.connection.execute(f"SELECT * FROM {table}").fetchall()
                        if rows:
                            disk.executemany(f"INSERT INTO {table} VALUES (?, ?)", rows)
                run_model.materialize_model(disk)
            finally:
                disk.close()

            original_database = database_path.read_bytes()
            records = reverse_etl.read_billing_records(database_path)
            self.assertEqual(records, [{"stripe_customer_id": "cus_test", "customer_name": "Example Company",
                "subscription_status": "active", "mrr_cents": 4900, "company_domain": "acme.io",
                "active_users_30d": 0, "last_event_at": None}])
            self.assertEqual(reverse_etl.classify_customer(records[0], {}), "NEW")
            success = Mock(return_value=True)
            reverse_etl.sync_with_simulated_sender(records, state_path, success)
            saved_bytes = state_path.read_bytes()
            self.assertEqual(reverse_etl.load_sync_state(state_path)["cus_test"]["mrr_cents"], 4900)
            self.assertEqual(reverse_etl.classify_customer(
                records[0], reverse_etl.load_sync_state(state_path)), "UNCHANGED")

            # Exercise the real preview entry point against only temporary files.
            with patch("sys.argv", ["reverse_etl.py", "--database", str(database_path),
                                    "--state", str(state_path)]), redirect_stdout(StringIO()) as output:
                self.assertEqual(reverse_etl.main(), 0)
            self.assertIn("UNCHANGED: 1", output.getvalue())
            self.assertEqual(database_path.read_bytes(), original_database)
            self.assertEqual(state_path.read_bytes(), saved_bytes)

            changed = [{**records[0], "mrr_cents": 0}]
            self.assertEqual(reverse_etl.classify_customer(changed[0], reverse_etl.load_sync_state(state_path)), "CHANGED")
            failure = Mock(side_effect=TimeoutError("Simulated timeout"))
            outcomes = reverse_etl.sync_with_simulated_sender(changed, state_path, failure)
            self.assertEqual(outcomes["cus_test"], "FAILED")
            self.assertEqual(state_path.read_bytes(), saved_bytes)
            reverse_etl.sync_with_simulated_sender(changed, state_path, success)
            self.assertEqual(reverse_etl.load_sync_state(state_path)["cus_test"]["mrr_cents"], 0)
            skipped = Mock(return_value=True)
            outcomes = reverse_etl.sync_with_simulated_sender(changed, state_path, skipped)
            self.assertEqual(outcomes["cus_test"], "UNCHANGED")
            skipped.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
