"""Local Stripe snapshot-webhook inbox. No API calls or billing updates.

Start with: python -m flask --app webhook_app:create_app run --host 127.0.0.1
Set STRIPE_WEBHOOK_SECRET first. Imports alone do not create an inbox.
"""

from contextlib import closing
import os
from pathlib import Path
import sqlite3

from flask import Flask, jsonify, request
import stripe


# Lesson rule: reject signed delivery timestamps more than five minutes old.
# This checks the signature's timestamp, NOT the event's created field.
SIGNATURE_TOLERANCE_SECONDS = 300


def create_app(webhook_secret=None, inbox_path=None):
    """Create a receiver; tests supply a fake secret and a temporary inbox."""
    secret = webhook_secret if webhook_secret is not None else os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise ValueError("Set STRIPE_WEBHOOK_SECRET to a webhook signing secret starting with whsec_.")
    database_path = Path(inbox_path if inbox_path is not None else os.environ.get(
        "INBOX_DB", str(Path(__file__).resolve().parent / "inbox.sqlite")
    )).resolve()

    # Keep the inbox separate even if an existing project path is supplied by mistake.
    if database_path.suffix.lower() != ".sqlite":
        raise ValueError("The webhook inbox must be a separate .sqlite file.")

    app = Flask(__name__)
    app.config["INBOX_DB"] = str(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        with connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS stripe_events (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                created INTEGER NOT NULL,
                payload TEXT NOT NULL,
                processed INTEGER NOT NULL DEFAULT 0
            )""")

    @app.post("/stripe/webhook")
    def stripe_webhook():
        # Never use request.json first: verification needs the exact original bytes.
        raw_body = request.get_data()
        signature_header = request.headers.get("Stripe-Signature")
        try:
            event = stripe.Webhook.construct_event(
                raw_body, signature_header, secret,
                tolerance=SIGNATURE_TOLERANCE_SECONDS,
            )
        except (ValueError, TypeError, AttributeError, stripe.SignatureVerificationError):
            return jsonify(status="invalid signature or payload"), 400

        # Only verified events reach validation or storage. Snapshot events need
        # these fields; no current-object API fetch or downstream work happens here.
        verified_values = event.to_dict()
        event_id = verified_values.get("id")
        event_type = verified_values.get("type")
        created = verified_values.get("created")
        if (not isinstance(event_id, str) or not event_id.strip()
                or not isinstance(event_type, str) or not event_type.strip()
                or type(created) is not int):
            return jsonify(status="invalid event fields"), 400

        try:
            with closing(sqlite3.connect(database_path)) as connection:
                with connection:
                    # The primary key is persistent deduplication, not a Python set.
                    # A repeat cannot overwrite the payload or reset processed state.
                    result = connection.execute("""INSERT INTO stripe_events
                        (id, type, created, payload) VALUES (?, ?, ?, ?)
                        ON CONFLICT(id) DO NOTHING""",
                        (event_id, event_type, created, raw_body.decode("utf-8")))
                    inserted = result.rowcount == 1
        except sqlite3.Error:
            # Do not acknowledge an event that we failed to durably store.
            app.logger.error("Webhook inbox write failed; event was not acknowledged.")
            return jsonify(status="inbox unavailable; retry delivery"), 503

        # Slow processing belongs in a later worker. Commit the inbox before 200.
        return jsonify(status="stored" if inserted else "duplicate"), 200

    return app
