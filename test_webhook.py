"""Signed local requests only: no server, API calls, or project database access."""

import hashlib
from contextlib import closing
import hmac
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from webhook_app import create_app


TEST_SECRET = "whsec_local_test_only"
NOW = 1800000000


def sign_payload(body, secret=TEST_SECRET, timestamp=NOW):
    """Generate the timestamp + raw-body HMAC used by Stripe deliveries."""
    signed_bytes = str(timestamp).encode() + b"." + body
    signature = hmac.new(secret.encode(), signed_bytes, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


class WebhookTests(unittest.TestCase):
    def setUp(self):
        temporary_folder = tempfile.TemporaryDirectory(
            prefix=".webhook_test_", dir=Path(__file__).resolve().parent
        )
        self.addCleanup(temporary_folder.cleanup)
        self.inbox_path = Path(temporary_folder.name) / "test_inbox.sqlite"
        self.app = create_app(TEST_SECRET, self.inbox_path)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.body = json.dumps({
            "id": "evt_local_001", "object": "event", "type": "invoice.paid",
            "created": NOW - 3600,  # Old event, freshly signed delivery is valid.
            "data": {"object": {"id": "in_local_001"}},
        }).encode()
        # Freeze only time, never mock signature verification.
        clock = patch("stripe._webhook.time.time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        # Fail immediately if this test workflow attempts an outbound connection.
        network = patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def post(self, signature=None, body=None):
        headers = {} if signature is None else {"Stripe-Signature": signature}
        return self.client.post("/stripe/webhook", data=self.body if body is None else body,
                                headers=headers, content_type="application/json")

    def stored_rows(self):
        with closing(sqlite3.connect(self.inbox_path)) as connection:
            return connection.execute(
                "SELECT id, type, created, payload, processed FROM stripe_events"
            ).fetchall()

    def test_valid_signed_event_is_stored(self):
        response = self.post(sign_payload(self.body))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "stored")
        self.assertEqual(self.stored_rows(), [
            ("evt_local_001", "invoice.paid", NOW - 3600, self.body.decode(), 0)
        ])

    def test_invalid_signature_is_not_stored(self):
        self.assertEqual(self.post(sign_payload(self.body, secret="whsec_wrong")).status_code, 400)
        self.assertEqual(self.stored_rows(), [])

    def test_missing_signature_is_not_stored(self):
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(self.stored_rows(), [])

    def test_duplicate_delivery_is_stored_once_even_after_restart(self):
        signature = sign_payload(self.body)
        self.assertEqual(self.post(signature).status_code, 200)
        # Recreate the app to prove deduplication survives losing Python memory.
        restarted_client = create_app(TEST_SECRET, self.inbox_path).test_client()
        response = restarted_client.post("/stripe/webhook", data=self.body,
                                        headers={"Stripe-Signature": signature})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "duplicate")
        self.assertEqual(len(self.stored_rows()), 1)

    def test_stale_signed_delivery_is_rejected(self):
        self.assertEqual(self.post(sign_payload(self.body, timestamp=NOW - 3600)).status_code, 400)
        self.assertEqual(self.stored_rows(), [])

    def test_five_minute_boundary(self):
        self.assertEqual(self.post(sign_payload(self.body, timestamp=NOW - 301)).status_code, 400)
        self.assertEqual(self.stored_rows(), [])
        self.assertEqual(self.post(sign_payload(self.body, timestamp=NOW - 300)).status_code, 200)
        self.assertEqual(len(self.stored_rows()), 1)

    def test_tampered_body_is_not_stored(self):
        self.assertEqual(self.post(sign_payload(self.body), body=self.body + b" ").status_code, 400)
        self.assertEqual(self.stored_rows(), [])

    def test_signed_malformed_payload_is_not_stored(self):
        for body in (b'not JSON', b'[]'):
            with self.subTest(body=body):
                self.assertEqual(self.post(sign_payload(body), body=body).status_code, 400)
                self.assertEqual(self.stored_rows(), [])

    def test_duplicate_does_not_overwrite_or_reset_processed_flag(self):
        self.post(sign_payload(self.body))
        with closing(sqlite3.connect(self.inbox_path)) as connection:
            with connection:
                connection.execute("UPDATE stripe_events SET processed = 1")
        before = self.stored_rows()
        changed_body = self.body.replace(b'invoice.paid', b'invoice.updated')
        response = self.post(sign_payload(changed_body), body=changed_body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "duplicate")
        self.assertEqual(self.stored_rows(), before)

    def test_bad_or_stale_duplicate_is_still_rejected(self):
        self.post(sign_payload(self.body))
        before = self.stored_rows()
        self.assertEqual(self.post(sign_payload(self.body, secret="whsec_wrong")).status_code, 400)
        self.assertEqual(self.post(sign_payload(self.body, timestamp=NOW - 301)).status_code, 400)
        self.assertEqual(self.stored_rows(), before)

    def test_signed_event_with_missing_fields_is_not_stored(self):
        body = b'{"object":"event","type":"invoice.paid","created":1}'
        self.assertEqual(self.post(sign_payload(body), body=body).status_code, 400)
        self.assertEqual(self.stored_rows(), [])

    def test_storage_failure_is_not_acknowledged(self):
        with patch("webhook_app.sqlite3.connect", side_effect=sqlite3.OperationalError("Test failure")):
            self.assertEqual(self.post(sign_payload(self.body)).status_code, 503)
        self.assertEqual(self.stored_rows(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
