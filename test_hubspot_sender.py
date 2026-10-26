"""Mocked HTTP only. Every database/state file is disposable test data."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
from email.utils import format_datetime
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import duckdb
import requests

from hubspot_payloads import destination_values, to_upsert_input
from hubspot_sender import HubSpotSender, SenderStopped, UPSERT_URL, ACCOUNT_DETAILS_URL, EXPECTED_PORTAL_ID
import reverse_etl
import test_hubspot_payloads as payload_fixtures


def success_body(customer_id="cus_test"):
    return {"status": "COMPLETE", "numErrors": 0, "results": [
        {"id": "mock_record_id", "properties": {"stripe_customer_id": customer_id}}
    ]}


def response(status=200, body=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = success_body() if body is None else body
    return result


class HubSpotSenderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix=".sender_test_", dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        self.state_path = Path(folder.name) / "test_state.json"
        self.database_path = Path(folder.name) / "test.duckdb"
        self.row = payload_fixtures.example_row()
        self.values = destination_values(self.row)
        self.session = Mock()
        self.session.get.return_value = response(body={"portalId": EXPECTED_PORTAL_ID,
                                                     "accountType": "DEVELOPER_TEST"})
        self.sleep = Mock()
        # This is a dummy string, never a real credential or environment lookup.
        self.sender = HubSpotSender("unit-test-placeholder", session=self.session,
                                    sleep=self.sleep, clock=lambda: 1000)
        block_network = patch("socket.socket.connect", side_effect=AssertionError("No network allowed"))
        block_network.start()
        self.addCleanup(block_network.stop)

    def create_source(self, rows):
        connection = duckdb.connect(str(self.database_path))
        try:
            connection.execute("""CREATE TABLE account_billing (
                stripe_customer_id VARCHAR, customer_name VARCHAR, company_domain VARCHAR,
                mrr_cents BIGINT, subscription_status VARCHAR, active_users_30d BIGINT,
                last_event_at VARCHAR)""")
            for row in rows:
                connection.execute("INSERT INTO account_billing VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [row[field] for field in payload_fixtures.example_row()])
        finally:
            connection.close()

    def run_cli(self, flags=(), environment=None):
        settings = {"HUBSPOT_API_KEY": "unit-test-placeholder", "DRY_RUN": "0"}
        settings.update(environment or {})
        with patch.dict(os.environ, settings), patch("sys.argv", ["reverse_etl.py",
            "--database", str(self.database_path), "--state", str(self.state_path), *flags
        ]), redirect_stdout(StringIO()) as output:
            result = reverse_etl.main()
        self.assertNotIn("unit-test-placeholder", output.getvalue())
        return result, output.getvalue()

    def test_success_uses_exact_one_record_payload_and_disables_redirects(self):
        reply = response()
        self.session.post.return_value = reply
        self.assertTrue(self.sender("cus_test", self.values))
        args, kwargs = self.session.post.call_args
        self.assertEqual(args, (UPSERT_URL,))
        self.assertEqual(kwargs["json"], {"inputs": [to_upsert_input(self.row)]})
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (10, 30))
        reply.close.assert_called_once()
        self.sleep.assert_not_called()

    def test_matching_developer_portal_is_checked_before_writes_once_per_sender(self):
        self.session.post.return_value = response()
        self.assertTrue(self.sender("cus_test", self.values))
        self.assertTrue(self.sender("cus_test", self.values))
        self.session.get.assert_called_once_with(ACCOUNT_DETAILS_URL,
            headers={"Authorization": "Bearer unit-test-placeholder"},
            timeout=(10, 30), allow_redirects=False)
        calls = [call[0] for call in self.session.method_calls]
        self.assertLess(calls.index("get"), calls.index("post"))
        # A new sender must check again, even when reusing an HTTP session.
        HubSpotSender("unit-test-placeholder", session=self.session)("cus_test", self.values)
        self.assertEqual(self.session.get.call_count, 2)

    def test_unverified_portal_blocks_all_writes_and_preserves_state(self):
        reverse_etl.save_sync_state(self.state_path, {"cus_test": self.values})
        before = self.state_path.read_bytes()
        for details in ({"portalId": 123, "accountType": "DEVELOPER_TEST"},
                        {"portalId": EXPECTED_PORTAL_ID, "accountType": "STANDARD"},
                        {"portalId": str(EXPECTED_PORTAL_ID), "accountType": "DEVELOPER_TEST"},
                        {}, [], {"portalId": EXPECTED_PORTAL_ID}):
            with self.subTest(details=details):
                reply = response(body=details)
                self.session.get.return_value = reply
                with self.assertRaises(SenderStopped):
                    reverse_etl.sync_records([{**self.row, "mrr_cents": 0}],
                                             self.state_path, self.sender)
                self.session.post.assert_not_called()
                reply.close.assert_called_once()
                self.assertEqual(self.state_path.read_bytes(), before)

    def test_account_http_or_json_failure_blocks_writes(self):
        replies = [response(status) for status in (301, 401, 403, 429, 500)]
        invalid_json = response()
        invalid_json.json.side_effect = ValueError("unit-test-placeholder")
        replies.append(invalid_json)
        for reply in replies:
            with self.subTest(status=reply.status_code):
                self.session.get.return_value = reply
                with self.assertRaises(SenderStopped) as caught:
                    self.sender("cus_test", self.values)
                self.assertNotIn("unit-test-placeholder", str(caught.exception))
                self.session.post.assert_not_called()
                reply.close.assert_called_once()
        self.assertFalse(self.state_path.exists())

    def test_account_connection_failure_preserves_state_and_stops_cli(self):
        self.create_source([{**self.row, "mrr_cents": 0}])
        reverse_etl.save_sync_state(self.state_path, {"cus_test": self.values})
        before = self.state_path.read_bytes()
        for error in (requests.Timeout("unit-test-placeholder"),
                      requests.ConnectionError("unit-test-placeholder")):
            self.session.get.side_effect = error
            with patch("hubspot_sender.requests.Session", return_value=self.session):
                result, output = self.run_cli(["--write"])
            self.assertEqual(result, 1)
            self.assertIn("Account identity request failed", output)
            self.session.post.assert_not_called()
            self.assertEqual(self.state_path.read_bytes(), before)

    def test_invalid_timestamp_stops_before_account_or_write_request(self):
        with self.assertRaisesRegex(ValueError, "last_event_at"):
            self.sender("cus_test", {**self.values, "last_event_at": "not-a-date"})
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_missing_key_is_rejected_without_constructing_http_session(self):
        with patch.dict(os.environ, {"HUBSPOT_API_KEY": ""}), patch("hubspot_sender.requests.Session") as factory:
            with self.assertRaisesRegex(ValueError, "HUBSPOT_API_KEY is missing"):
                HubSpotSender.from_environment()
            factory.assert_not_called()

    def test_timeout_and_network_errors_are_sanitized_and_never_retried(self):
        for exception in (requests.Timeout("unit-test-placeholder"),
                          requests.ConnectionError("unit-test-placeholder")):
            with self.subTest(exception=type(exception).__name__):
                self.session.reset_mock()
                self.session.post.side_effect = exception
                with self.assertRaises(SenderStopped) as caught:
                    self.sender("cus_test", self.values)
                self.assertNotIn("unit-test-placeholder", str(caught.exception))
                self.assertEqual(self.session.post.call_count, 1)
        self.sleep.assert_not_called()

    def test_authentication_and_permission_failures_stop_without_retry(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.session.reset_mock()
                self.session.post.return_value = response(status)
                with self.assertRaisesRegex(SenderStopped, f"HTTP {status}"):
                    self.sender("cus_test", self.values)
                self.assertEqual(self.session.post.call_count, 1)
        self.sleep.assert_not_called()

    def test_invalid_record_http_errors_do_not_confirm_success(self):
        for status in (400, 422):
            self.session.post.return_value = response(status)
            with redirect_stdout(StringIO()):
                self.assertFalse(self.sender("cus_test", self.values))
        self.sleep.assert_not_called()

    def test_rate_limit_honors_retry_after_seconds(self):
        self.session.post.side_effect = [response(429, headers={"Retry-After": "5"}), response()]
        self.assertTrue(self.sender("cus_test", self.values))
        self.sleep.assert_called_once_with(5.0)
        self.assertEqual(self.session.post.call_count, 2)
        self.assertEqual(self.session.post.call_args_list[0], self.session.post.call_args_list[1])

    def test_rate_limit_honors_http_date(self):
        date = format_datetime(datetime.fromtimestamp(1007, tz=timezone.utc), usegmt=True)
        self.session.post.side_effect = [response(429, headers={"Retry-After": date}), response()]
        self.assertTrue(self.sender("cus_test", self.values))
        self.sleep.assert_called_once_with(7.0)

    def test_invalid_or_over_budget_retry_after_stops_without_early_retry(self):
        for value in ("31", "NaN", "inf", "not-a-date"):
            with self.subTest(value=value):
                self.session.reset_mock()
                self.session.post.return_value = response(429, headers={"Retry-After": value})
                with self.assertRaises(SenderStopped):
                    self.sender("cus_test", self.values)
                self.assertEqual(self.session.post.call_count, 1)
        self.sleep.assert_not_called()

    def test_server_errors_and_rate_limits_have_bounded_retries(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                self.session.reset_mock()
                self.sleep.reset_mock()
                self.session.post.return_value = response(status)
                with self.assertRaisesRegex(SenderStopped, "retry limit"):
                    self.sender("cus_test", self.values)
                self.assertEqual(self.session.post.call_count, 3)
                self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1.0, 2.0])

    def test_temporary_server_error_then_success(self):
        self.session.post.side_effect = [response(503), response()]
        self.assertTrue(self.sender("cus_test", self.values))
        self.sleep.assert_called_once_with(1.0)

    def test_partial_batch_errors_are_not_success_even_with_result(self):
        for status in (200, 207):
            for error_fields in ({"numErrors": 1}, {"errors": [{"message": "hidden"}]}):
                self.session.post.return_value = response(status, {**success_body(), **error_fields})
                self.assertFalse(self.sender("cus_test", self.values))

    def test_pending_redirect_and_unexpected_http_status_are_unconfirmed(self):
        for status in (202, 204, 301, 302, 404):
            with self.subTest(status=status):
                self.session.post.return_value = response(status)
                with self.assertRaises(SenderStopped):
                    self.sender("cus_test", self.values)

    def test_ambiguous_or_mismatched_response_is_not_success(self):
        for body in ([], {}, {"status": "PENDING"},
                     {**success_body(), "results": []},
                     {**success_body(), "results": success_body()["results"] * 2},
                     success_body("different_customer"),
                     {**success_body(), "results": [{"id": "record"}]},
                     {**success_body(), "results": [{"properties": {"stripe_customer_id": "cus_test"}}]}):
            with self.subTest(body=body):
                self.session.post.return_value = response(body=body)
                with self.assertRaises(SenderStopped):
                    self.sender("cus_test", self.values)

    def test_invalid_json_is_unconfirmed_and_response_closed(self):
        reply = response()
        reply.json.side_effect = ValueError("unit-test-placeholder")
        self.session.post.return_value = reply
        with self.assertRaisesRegex(SenderStopped, "Invalid JSON"):
            self.sender("cus_test", self.values)
        reply.close.assert_called_once()

    def test_default_preview_and_dry_run_never_construct_a_sender_or_save(self):
        self.create_source([self.row])
        database_before = self.database_path.read_bytes()
        for dry_run in ("0", "1"):
            with patch.object(reverse_etl.HubSpotSender, "from_environment") as factory, \
                    patch.object(reverse_etl, "save_sync_state") as save:
                result, output = self.run_cli(environment={"DRY_RUN": dry_run})
            self.assertEqual(result, 0)
            factory.assert_not_called()
            save.assert_not_called()
        self.assertFalse(self.state_path.exists())
        self.assertEqual(self.database_path.read_bytes(), database_before)

    def test_conflicting_write_flags_stop_before_sender_creation(self):
        for flags, environment in ((["--write"], {"DRY_RUN": "1"}),
                                   (["--write", "--simulate"], {})):
            with patch.object(reverse_etl.HubSpotSender, "from_environment") as factory:
                result, output = self.run_cli(flags, environment)
            self.assertEqual(result, 1)
            self.assertIn("cannot be combined", output)
            factory.assert_not_called()

    def test_cli_write_missing_key_has_no_http_calls_or_state(self):
        self.create_source([self.row])
        with patch("hubspot_sender.requests.Session") as factory:
            result, output = self.run_cli(["--write"], {"HUBSPOT_API_KEY": ""})
        self.assertEqual(result, 1)
        self.assertIn("HUBSPOT_API_KEY is missing", output)
        factory.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_cli_writes_six_then_zero_then_one_using_mocked_http(self):
        rows = [payload_fixtures.example_row(stripe_customer_id=f"cus_{index}") for index in range(6)]
        self.create_source(rows)
        database_before = self.database_path.read_bytes()
        self.session.post.side_effect = lambda *args, **kwargs: response(
            body=success_body(kwargs["json"]["inputs"][0]["id"]))
        with patch("hubspot_sender.requests.Session", return_value=self.session):
            result, output = self.run_cli(["--write"])
        self.assertEqual(result, 0)
        self.assertEqual(self.session.post.call_count, 6)
        self.assertIn("Confirmed writes: 6", output)
        saved = reverse_etl.load_sync_state(self.state_path)
        self.assertEqual(saved, {row["stripe_customer_id"]: destination_values(row) for row in rows})
        with patch.object(reverse_etl.HubSpotSender, "from_environment") as factory:
            result, output = self.run_cli(["--write"])
        self.assertEqual(result, 0)
        factory.assert_not_called()
        self.assertEqual(self.database_path.read_bytes(), database_before)

        # Change disposable state to represent one older successful MRR value.
        saved["cus_2"]["mrr_cents"] = 0
        reverse_etl.save_sync_state(self.state_path, saved)
        self.session.post.reset_mock()
        with patch("hubspot_sender.requests.Session", return_value=self.session):
            result, output = self.run_cli(["--write"])
        self.assertEqual(result, 0)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.session.post.call_args.kwargs["json"]["inputs"][0]["id"], "cus_2")

    def test_mixed_record_results_save_only_confirmed_success(self):
        second = payload_fixtures.example_row(stripe_customer_id="cus_second")
        self.session.post.side_effect = [response(400), response(body=success_body("cus_second"))]
        with redirect_stdout(StringIO()):
            outcomes = reverse_etl.sync_records([self.row, second], self.state_path, self.sender)
        self.assertEqual(outcomes, {"cus_test": "FAILED", "cus_second": "CONFIRMED SUCCESS"})
        self.assertEqual(reverse_etl.load_sync_state(self.state_path), {"cus_second": destination_values(second)})

    def test_partial_response_preserves_state_bytes(self):
        reverse_etl.save_sync_state(self.state_path, {"cus_test": self.values})
        before = self.state_path.read_bytes()
        self.session.post.return_value = response(207, {**success_body(), "numErrors": 1})
        outcome = reverse_etl.sync_records([{**self.row, "mrr_cents": 0}], self.state_path, self.sender)
        self.assertEqual(outcome["cus_test"], "FAILED")
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_timeout_keeps_prior_success_but_not_uncertain_customer(self):
        second = payload_fixtures.example_row(stripe_customer_id="cus_second")
        self.session.post.side_effect = [response(), requests.Timeout("unit-test-placeholder")]
        with self.assertRaises(SenderStopped):
            reverse_etl.sync_records([self.row, second], self.state_path, self.sender)
        self.assertEqual(reverse_etl.load_sync_state(self.state_path), {"cus_test": self.values})

    def test_auth_failure_preserves_state_and_stops_remaining_customers(self):
        reverse_etl.save_sync_state(self.state_path, {"cus_test": self.values})
        before = self.state_path.read_bytes()
        self.session.post.return_value = response(403)
        with self.assertRaises(SenderStopped):
            reverse_etl.sync_records([{**self.row, "mrr_cents": 0},
                payload_fixtures.example_row(stripe_customer_id="cus_second")], self.state_path, self.sender)
        self.assertEqual(self.session.post.call_count, 1)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_guards_and_invalid_records_stop_cli_before_sender(self):
        for scenario in ("row_count", "invalid_name", "malformed_state"):
            with self.subTest(scenario=scenario):
                self.database_path.unlink(missing_ok=True)
                self.state_path.unlink(missing_ok=True)
                rows = [self.row]
                if scenario == "row_count":
                    state = {f"old_{i}": self.values for i in range(6)}
                    reverse_etl.save_sync_state(self.state_path, state)
                elif scenario == "invalid_name":
                    rows.append(payload_fixtures.example_row(stripe_customer_id="cus_z", customer_name=None))
                else:
                    self.state_path.write_text("{broken JSON", encoding="utf-8")
                self.create_source(rows)
                before = self.state_path.read_bytes() if self.state_path.exists() else None
                with patch.object(reverse_etl.HubSpotSender, "from_environment") as factory:
                    result, output = self.run_cli(["--write"])
                self.assertEqual(result, 1)
                factory.assert_not_called()
                self.assertEqual(self.state_path.read_bytes() if self.state_path.exists() else None, before)

    def test_save_failure_after_confirmed_write_keeps_old_state_and_stops(self):
        reverse_etl.save_sync_state(self.state_path, {"cus_test": self.values})
        before = self.state_path.read_bytes()
        self.session.post.return_value = response()
        with patch.object(reverse_etl.os, "replace", side_effect=OSError("Test save failure")):
            with self.assertRaises(OSError):
                reverse_etl.sync_records([{**self.row, "mrr_cents": 0}], self.state_path, self.sender)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_unknown_sender_error_never_prints_raw_exception(self):
        sender = Mock(side_effect=RuntimeError("unit-test-placeholder"))
        with redirect_stdout(StringIO()) as output, self.assertRaises(SenderStopped) as caught:
            reverse_etl.sync_records([self.row], self.state_path, sender)
        self.assertNotIn("unit-test-placeholder", str(caught.exception) + output.getvalue())
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
