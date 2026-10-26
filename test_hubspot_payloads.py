"""Offline payload checks: temporary databases, no destination connections."""

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import duckdb

from hubspot_payloads import (
    BILLING_ACCOUNT_SCHEMA, destination_values, plan_upsert_batches, to_upsert_input,
)
import reverse_etl


def example_row(**changes):
    row = {"stripe_customer_id": "cus_test", "customer_name": "Example Company",
           "company_domain": "example.com", "mrr_cents": 4900,
           "subscription_status": "active", "active_users_30d": 3,
           "last_event_at": "2026-10-24T10:00:00Z"}
    row.update(changes)
    return row


class HubSpotPayloadTests(unittest.TestCase):
    def test_schema_matches_lesson(self):
        self.assertEqual(BILLING_ACCOUNT_SCHEMA["name"], "billing_account")
        self.assertEqual(BILLING_ACCOUNT_SCHEMA["requiredProperties"], ["stripe_customer_id"])
        properties = {prop["name"]: prop for prop in BILLING_ACCOUNT_SCHEMA["properties"]}
        self.assertEqual(set(properties), set(example_row()))
        self.assertTrue(properties["stripe_customer_id"]["hasUniqueValue"])
        self.assertEqual(properties["last_event_at"]["type"], "datetime")

    def test_exact_payload_mapping(self):
        self.assertEqual(to_upsert_input(example_row()), {
            "idProperty": "stripe_customer_id", "id": "cus_test",
            "properties": {"stripe_customer_id": "cus_test", "customer_name": "Example Company",
                "company_domain": "example.com", "mrr_cents": "4900",
                "subscription_status": "active", "active_users_30d": "3",
                "last_event_at": "2026-10-24T10:00:00Z"}})

    def test_valid_utc_timestamps_are_preserved_exactly(self):
        for timestamp in ("2026-10-24T10:00:00Z", "2024-02-29T23:59:59Z",
                          "2026-10-24T10:00:00.123456Z", "2026-10-24T10:00:00+00:00",
                          "2026-10-24T10:00:00.000+00:00"):
            with self.subTest(timestamp=timestamp):
                row = example_row(last_event_at=timestamp)
                self.assertEqual(destination_values(row)["last_event_at"], timestamp)
                self.assertEqual(to_upsert_input(row)["properties"]["last_event_at"], timestamp)

    def test_malformed_or_non_utc_timestamps_are_rejected(self):
        for timestamp in ("", " ", "not-a-date", "2026-10-24", "2026-10-24T10:00:00",
                          "2026-02-29T10:00:00Z", "2026-13-24T10:00:00Z",
                          "2026-10-24T24:00:00Z", "2026-10-24T10:60:00Z",
                          "2026-10-24T10:00:60Z", "2026-10-24T10:00:00+05:30",
                          "2026-10-24T10:00Z", "2026-10-24T10:00:00z", 123, False):
            with self.subTest(timestamp=timestamp):
                with self.assertRaisesRegex(ValueError, "last_event_at"):
                    to_upsert_input(example_row(last_event_at=timestamp))

    def test_invalid_timestamp_preflight_prevents_sends_and_dry_run_batches(self):
        with tempfile.TemporaryDirectory(prefix=".timestamp_test_", dir=Path(__file__).parent) as folder:
            state_path = Path(folder) / "test_state.json"
            reverse_etl.save_sync_state(state_path, {"cus_test": destination_values(example_row())})
            before = state_path.read_bytes()
            rows = [example_row(mrr_cents=0),
                    example_row(stripe_customer_id="cus_invalid", last_event_at="2026-02-30T10:00:00Z")]
            sender = Mock(return_value=True)
            with self.assertRaisesRegex(ValueError, "last_event_at"):
                reverse_etl.sync_with_simulated_sender(rows, state_path, sender)
            sender.assert_not_called()
            with patch.object(reverse_etl, "read_destination_records", return_value=rows), \
                    redirect_stdout(StringIO()) as output:
                with self.assertRaisesRegex(ValueError, "last_event_at"):
                    reverse_etl.print_destination_payloads(Path(folder) / "unused.duckdb", state_path)
            self.assertNotIn('"inputs"', output.getvalue())
            self.assertNotIn("Planned upsert batches", output.getvalue())
            self.assertEqual(state_path.read_bytes(), before)

    def test_valid_customer_names_are_preserved_exactly(self):
        for name in ("Acme", "  Acme  ", "\tAcme\n"):
            with self.subTest(name=name):
                row = example_row(customer_name=name)
                self.assertEqual(destination_values(row)["customer_name"], name)
                self.assertEqual(to_upsert_input(row)["properties"]["customer_name"], name)

    def test_missing_customer_name_column_is_rejected(self):
        row = example_row()
        del row["customer_name"]
        with self.assertRaisesRegex(ValueError, "Missing destination columns: customer_name"):
            to_upsert_input(row)

    def test_null_blank_and_nonstring_customer_names_are_rejected(self):
        for name in (None, "", "   ", "\t\n", 123, 0, False, [], {}):
            with self.subTest(name=name):
                row = example_row(customer_name=name)
                with self.assertRaisesRegex(ValueError, "Missing or blank customer_name"):
                    destination_values(row)
                with self.assertRaisesRegex(ValueError, "Missing or blank customer_name"):
                    to_upsert_input(row)

    def test_invalid_name_prevents_all_sends_and_preserves_saved_state(self):
        with tempfile.TemporaryDirectory(prefix=".name_validation_", dir=Path(__file__).parent) as folder:
            state_path = Path(folder) / "test_state.json"
            saved_row = example_row()
            reverse_etl.save_sync_state(state_path, {"cus_test": destination_values(saved_row)})
            before = state_path.read_bytes()
            # Put a valid pending update first: it must not be sent before the
            # later invalid NEW record is discovered during preflight validation.
            valid_update = example_row(mrr_cents=0)
            for name in (None, "", "   ", "\t\n", 123):
                with self.subTest(name=name):
                    sender = Mock(return_value=True)
                    invalid_row = example_row(stripe_customer_id="cus_new", customer_name=name)
                    with patch("socket.socket.connect", side_effect=AssertionError("No network")), \
                            patch.object(reverse_etl, "save_sync_state") as save:
                        with self.assertRaisesRegex(ValueError, "customer_name"):
                            reverse_etl.sync_with_simulated_sender(
                                [valid_update, invalid_row], state_path, sender)
                    sender.assert_not_called()
                    save.assert_not_called()
                    self.assertEqual(state_path.read_bytes(), before)

    def test_invalid_name_dry_run_has_no_batches_or_state_changes(self):
        with tempfile.TemporaryDirectory(prefix=".name_dry_run_", dir=Path(__file__).parent) as folder:
            database_path = Path(folder) / "test.duckdb"
            state_path = Path(folder) / "test_state.json"
            reverse_etl.save_sync_state(state_path, {"cus_test": destination_values(example_row())})
            state_before = state_path.read_bytes()
            connection = duckdb.connect(str(database_path))
            try:
                connection.execute("""CREATE TABLE account_billing (
                    stripe_customer_id VARCHAR, customer_name VARCHAR, company_domain VARCHAR,
                    mrr_cents BIGINT, subscription_status VARCHAR, active_users_30d BIGINT,
                    last_event_at VARCHAR)""")
                # A valid row sorts first; the invalid name must suppress all output batches.
                for row in (example_row(mrr_cents=0),
                            example_row(stripe_customer_id="cus_z_invalid", customer_name=None)):
                    connection.execute("INSERT INTO account_billing VALUES (?, ?, ?, ?, ?, ?, ?)", list(row.values()))
            finally:
                connection.close()
            database_before = database_path.read_bytes()
            with patch.dict(os.environ, {"DRY_RUN": "1"}), patch("sys.argv", [
                "reverse_etl.py", "--database", str(database_path), "--state", str(state_path)
            ]), patch("socket.socket.connect", side_effect=AssertionError("No network")), \
                    patch.object(reverse_etl, "save_sync_state") as save, \
                    redirect_stdout(StringIO()) as output:
                self.assertEqual(reverse_etl.main(), 1)
            self.assertIn("Missing or blank customer_name", output.getvalue())
            self.assertNotIn('"inputs"', output.getvalue())
            self.assertNotIn("Planned upsert batches", output.getvalue())
            save.assert_not_called()
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(database_path.read_bytes(), database_before)

    def test_missing_identifiers_and_duplicates_are_rejected(self):
        for identifier in (None, "", "  "):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                to_upsert_input(example_row(stripe_customer_id=identifier))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            plan_upsert_batches([example_row(), example_row()], {})
        missing_column = example_row()
        del missing_column["stripe_customer_id"]
        with self.assertRaisesRegex(ValueError, "Missing destination columns"):
            to_upsert_input(missing_column)

    def test_new_changed_and_unchanged_use_same_upsert_key(self):
        original = example_row()
        state = {"cus_test": destination_values(original)}
        counts, batches = plan_upsert_batches([original], {})
        self.assertEqual(counts["NEW"], 1)
        self.assertEqual(batches, [{"inputs": [to_upsert_input(original)]}])
        counts, batches = plan_upsert_batches([example_row(mrr_cents=0)], state)
        self.assertEqual(counts["CHANGED"], 1)
        self.assertEqual(batches[0]["inputs"][0]["properties"]["mrr_cents"], "0")
        self.assertEqual(batches[0]["inputs"][0]["idProperty"], "stripe_customer_id")
        self.assertEqual(plan_upsert_batches([original], state),
                         ({"NEW": 0, "CHANGED": 0, "UNCHANGED": 1}, []))

    def test_usage_domain_and_event_changes_are_detected(self):
        state = {"cus_test": destination_values(example_row())}
        for change in ({"active_users_30d": 4}, {"company_domain": None},
                       {"last_event_at": "2026-10-24T11:00:00Z"}):
            with self.subTest(change=change):
                counts, batches = plan_upsert_batches([example_row(**change)], state)
                self.assertEqual(counts["CHANGED"], 1)

    def test_nulls_omitted_and_zero_preserved(self):
        properties = to_upsert_input(example_row(
            company_domain=None, last_event_at=None, mrr_cents=0, active_users_30d=0))["properties"]
        self.assertNotIn("company_domain", properties)
        self.assertNotIn("last_event_at", properties)
        self.assertEqual(properties["mrr_cents"], "0")
        self.assertEqual(properties["active_users_30d"], "0")

    def test_batches_limit_to_100_and_core_state_is_conservatively_changed(self):
        rows = [example_row(stripe_customer_id=f"cus_{index}") for index in range(201)]
        counts, batches = plan_upsert_batches(rows, {})
        self.assertEqual([len(batch["inputs"]) for batch in batches], [100, 100, 1])
        core_state = {"cus_test": {"customer_name": "Example Company",
                                   "subscription_status": "active", "mrr_cents": 4900}}
        self.assertEqual(plan_upsert_batches([example_row()], core_state)[0]["CHANGED"], 1)

    def test_dry_run_entrypoint_is_read_only_and_prints_exact_body(self):
        with tempfile.TemporaryDirectory(prefix=".payload_test_", dir=Path(__file__).parent) as folder:
            database_path = Path(folder) / "test.duckdb"
            state_path = Path(folder) / "test_state.json"
            row = example_row()
            connection = duckdb.connect(str(database_path))
            try:
                connection.execute("""CREATE TABLE account_billing (
                    stripe_customer_id VARCHAR, customer_name VARCHAR, company_domain VARCHAR,
                    mrr_cents BIGINT, subscription_status VARCHAR, active_users_30d BIGINT,
                    last_event_at VARCHAR)""")
                connection.execute("INSERT INTO account_billing VALUES (?, ?, ?, ?, ?, ?, ?)", list(row.values()))
            finally:
                connection.close()
            # Full-schema successful state survives reload; only MRR now differs.
            reverse_etl.save_sync_state(state_path, {"cus_test": destination_values(example_row(mrr_cents=0))})
            database_before = database_path.read_bytes()
            state_before = state_path.read_bytes()
            with patch.dict(os.environ, {"DRY_RUN": "1"}), patch("sys.argv", [
                "reverse_etl.py", "--database", str(database_path), "--state", str(state_path)
            ]), patch("socket.socket.connect", side_effect=AssertionError("No network")), \
                    patch.object(reverse_etl, "save_sync_state", side_effect=AssertionError("No saves")), \
                    redirect_stdout(StringIO()) as output:
                self.assertEqual(reverse_etl.main(), 0)
            self.assertIn("CHANGED: 1", output.getvalue())
            body_start = output.getvalue().index('{')
            payload, unused = json.JSONDecoder().raw_decode(output.getvalue()[body_start:])
            self.assertEqual(payload, {"inputs": [to_upsert_input(row)]})
            self.assertEqual(database_path.read_bytes(), database_before)
            self.assertEqual(state_path.read_bytes(), state_before)
            state_path.unlink()
            with redirect_stdout(StringIO()):
                reverse_etl.print_destination_payloads(database_path, state_path)
            self.assertFalse(state_path.exists())

    def test_six_customer_success_rerun_and_one_field_change(self):
        with tempfile.TemporaryDirectory(prefix=".state_regression_", dir=Path(__file__).parent) as folder:
            state_path = Path(folder) / "test_state.json"
            rows = [example_row(stripe_customer_id=f"cus_{index}") for index in range(6)]
            counts, batches = plan_upsert_batches(rows, {})
            self.assertEqual(counts, {"NEW": 6, "CHANGED": 0, "UNCHANGED": 0})
            self.assertEqual(len(batches[0]["inputs"]), 6)
            sender = Mock(return_value=True)
            with patch("socket.socket.connect", side_effect=AssertionError("No network")):
                outcomes = reverse_etl.sync_with_simulated_sender(rows, state_path, sender)
            self.assertEqual(sender.call_count, 6)
            self.assertEqual(set(outcomes.values()), {"SIMULATED SUCCESS"})
            state = reverse_etl.load_sync_state(state_path)
            self.assertEqual(state, {row["stripe_customer_id"]: destination_values(row) for row in rows})
            self.assertEqual(plan_upsert_batches(rows, state),
                ({"NEW": 0, "CHANGED": 0, "UNCHANGED": 6}, []))
            before = state_path.read_bytes()
            sender.reset_mock()
            outcomes = reverse_etl.sync_with_simulated_sender(rows, state_path, sender)
            sender.assert_not_called()
            self.assertEqual(set(outcomes.values()), {"UNCHANGED"})
            self.assertEqual(state_path.read_bytes(), before)

            # Check every synced field independently against the same saved success.
            for field, value in (("customer_name", "Renamed"), ("subscription_status", "canceled"),
                                 ("mrr_cents", 0), ("company_domain", "new.example"),
                                 ("active_users_30d", 4), ("last_event_at", None)):
                with self.subTest(field=field):
                    changed = [{**row} for row in rows]
                    changed[2][field] = value
                    counts, batches = plan_upsert_batches(changed, state)
                    self.assertEqual(counts, {"NEW": 0, "CHANGED": 1, "UNCHANGED": 5})
                    self.assertEqual(batches, [{"inputs": [to_upsert_input(changed[2])]}])
                    failure = Mock(return_value=False)
                    outcomes = reverse_etl.sync_with_simulated_sender(changed, state_path, failure)
                    failure.assert_called_once()
                    self.assertEqual(outcomes["cus_2"], "FAILED")
                    self.assertEqual(state_path.read_bytes(), before)

            changed = [{**row} for row in rows]
            changed[2]["mrr_cents"] = 0
            reverse_etl.sync_with_simulated_sender(changed, state_path, Mock(return_value=True))
            self.assertEqual(plan_upsert_batches(changed, reverse_etl.load_sync_state(state_path)),
                ({"NEW": 0, "CHANGED": 0, "UNCHANGED": 6}, []))

    def test_incomplete_successful_state_is_rejected_without_sender_calls(self):
        with tempfile.TemporaryDirectory(prefix=".legacy_state_", dir=Path(__file__).parent) as folder:
            state_path = Path(folder) / "test_state.json"
            core_state = {"cus_test": {"customer_name": "Example Company",
                "subscription_status": "active", "mrr_cents": 4900}}
            state_path.write_text(json.dumps(core_state), encoding="utf-8")
            before = state_path.read_bytes()
            sender = Mock(return_value=True)
            with self.assertRaisesRegex(ValueError, "legacy"):
                reverse_etl.sync_with_simulated_sender([example_row()], state_path, sender)
            sender.assert_not_called()
            self.assertEqual(state_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
