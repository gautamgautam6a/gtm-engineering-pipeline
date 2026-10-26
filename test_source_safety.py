"""Row-count guard tests use disposable databases and state, never project data."""

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import duckdb

import reverse_etl
from hubspot_payloads import plan_upsert_batches
from source_safety import check_source_row_count


def core_record(index):
    return {"stripe_customer_id": f"cus_{index}", "customer_name": "Example Company",
            "subscription_status": "active", "mrr_cents": 4900, "company_domain": "example.com",
            "active_users_30d": 1, "last_event_at": None}


class SourceSafetyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix=".guard_test_", dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        self.database_path = Path(folder.name) / "test.duckdb"
        self.state_path = Path(folder.name) / "test_state.json"
        self.state = {core_record(index)["stripe_customer_id"]: reverse_etl.comparable_values(core_record(index))
                      for index in range(6)}
        reverse_etl.save_sync_state(self.state_path, self.state)
        self.original_state_bytes = self.state_path.read_bytes()

    def create_source(self, count):
        connection = duckdb.connect(str(self.database_path))
        try:
            connection.execute("""CREATE TABLE account_billing (
                stripe_customer_id VARCHAR, customer_name VARCHAR, company_domain VARCHAR,
                mrr_cents BIGINT, subscription_status VARCHAR, active_users_30d BIGINT,
                last_event_at VARCHAR)""")
            for index in range(count):
                connection.execute("INSERT INTO account_billing VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [f"cus_{index}", "Example Company", "example.com", 4900, "active", 1, None])
        finally:
            connection.close()

    def run_preview(self, dry_run):
        with patch.dict(os.environ, {"DRY_RUN": "1" if dry_run else "0"}), \
                patch("sys.argv", ["reverse_etl.py", "--database", str(self.database_path),
                                   "--state", str(self.state_path)]), \
                patch("socket.socket.connect", side_effect=AssertionError("No network")), \
                patch.object(reverse_etl, "save_sync_state", side_effect=AssertionError("No saves")), \
                redirect_stdout(StringIO()) as output:
            result = reverse_etl.main()
        return result, output.getvalue()

    def assert_no_actions(self, output):
        self.assertNotIn('"inputs"', output)
        self.assertNotIn("Planned sync actions", output)
        self.assertNotIn("Planned upsert batches", output)

    def test_exact_boundaries_and_first_run(self):
        with self.assertRaisesRegex(ValueError, "2 current rows.*6 saved"):
            check_source_row_count(2, 6)
        for current, saved in ((3, 6), (4, 6), (0, 0), (1, 0), (3, 5)):
            with self.subTest(current=current, saved=saved):
                check_source_row_count(current, saved)
        with self.assertRaises(ValueError):
            check_source_row_count(2, 5)

    def test_guard_failure_preserves_state_and_never_calls_sender(self):
        self.create_source(2)
        records = reverse_etl.read_billing_records(self.database_path)
        sender = Mock(return_value=True)
        with self.assertRaisesRegex(ValueError, "row-count guard"):
            reverse_etl.sync_with_simulated_sender(records, self.state_path, sender)
        sender.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), self.original_state_bytes)

    def test_both_preview_modes_stop_before_actionable_output(self):
        self.create_source(2)
        original_database = self.database_path.read_bytes()
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                result, output = self.run_preview(dry_run)
                self.assertEqual(result, 1)
                self.assertIn("row-count guard", output)
                self.assert_no_actions(output)
        self.assertEqual(self.state_path.read_bytes(), self.original_state_bytes)
        self.assertEqual(self.database_path.read_bytes(), original_database)

    def test_half_the_saved_count_is_allowed(self):
        self.create_source(3)
        for dry_run in (False, True):
            result, output = self.run_preview(dry_run)
            self.assertEqual(result, 0)
            self.assertNotIn("row-count guard", output)
        sender = Mock(return_value=True)
        # Three of six rows are unchanged: zero pending rows is not a source collapse.
        outcomes = reverse_etl.sync_with_simulated_sender(
            reverse_etl.read_billing_records(self.database_path), self.state_path, sender)
        self.assertEqual(set(outcomes.values()), {"UNCHANGED"})
        sender.assert_not_called()

    def test_missing_state_allows_first_sync_without_creating_state(self):
        self.create_source(2)
        self.state_path.unlink()
        for dry_run in (False, True):
            result, output = self.run_preview(dry_run)
            self.assertEqual(result, 0)
            self.assertIn("NEW: 2", output)
            self.assertFalse(self.state_path.exists())

    def test_malformed_state_stops_without_writing_or_sending(self):
        self.create_source(2)
        self.state_path.write_text("{broken JSON", encoding="utf-8")
        before = self.state_path.read_bytes()
        for dry_run in (False, True):
            result, output = self.run_preview(dry_run)
            self.assertEqual(result, 1)
            self.assertIn("Malformed state file", output)
            self.assert_no_actions(output)
        sender = Mock(return_value=True)
        with self.assertRaisesRegex(ValueError, "Malformed state file"):
            reverse_etl.sync_with_simulated_sender([core_record(0)], self.state_path, sender)
        sender.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_missing_source_file_is_not_created(self):
        for dry_run in (False, True):
            result, output = self.run_preview(dry_run)
            self.assertEqual(result, 1)
            self.assertIn("database file not found", output)
            self.assert_no_actions(output)
        self.assertFalse(self.database_path.exists())
        self.assertEqual(self.state_path.read_bytes(), self.original_state_bytes)

    def test_missing_table_is_an_error_not_zero_source_rows(self):
        duckdb.connect(str(self.database_path)).close()
        for dry_run in (False, True):
            result, output = self.run_preview(dry_run)
            self.assertEqual(result, 1)
            self.assertIn("could not read account_billing", output)
            self.assertNotIn("row-count guard", output)
            self.assert_no_actions(output)
        self.assertEqual(self.state_path.read_bytes(), self.original_state_bytes)

    def test_unreadable_source_stops_both_modes(self):
        self.create_source(2)
        # Simulate OS permission failure deterministically, without changing ACLs.
        with patch.object(reverse_etl.duckdb, "connect", side_effect=PermissionError("Test access denied")):
            for dry_run in (False, True):
                result, output = self.run_preview(dry_run)
                self.assertEqual(result, 1)
                self.assertIn("access denied", output)
                self.assert_no_actions(output)
        self.assertEqual(self.state_path.read_bytes(), self.original_state_bytes)

    def test_unreadable_state_stops_both_modes(self):
        self.create_source(2)
        with patch.object(Path, "open", side_effect=PermissionError("Test state access denied")):
            for dry_run in (False, True):
                result, output = self.run_preview(dry_run)
                self.assertEqual(result, 1)
                self.assertIn("state access denied", output)
                self.assert_no_actions(output)
        self.assertEqual(self.state_path.read_bytes(), self.original_state_bytes)

    def test_batch_planner_itself_cannot_bypass_guard(self):
        with self.assertRaisesRegex(ValueError, "row-count guard"):
            plan_upsert_batches([], self.state)


if __name__ == "__main__":
    unittest.main(verbosity=2)
