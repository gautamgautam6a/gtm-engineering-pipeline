"""Preview billing changes or test local state with a simulated sender."""

import argparse
import json
import os
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import duckdb

from hubspot_payloads import DESTINATION_COLUMNS, destination_values, plan_upsert_batches
from source_safety import check_source_row_count
from hubspot_sender import HubSpotSender, SenderStopped


def read_billing_records(database_path):
    """Preview, sender, and payload planner all read the same full records."""
    return read_destination_records(database_path)


def read_destination_records(database_path):
    """Read every destination field in read-only mode."""
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        rows = connection.execute(
            "SELECT " + ", ".join(DESTINATION_COLUMNS)
            + " FROM account_billing ORDER BY stripe_customer_id"
        ).fetchall()
        return [dict(zip(DESTINATION_COLUMNS, row)) for row in rows]
    finally:
        connection.close()


def print_destination_payloads(database_path, state_path):
    """DRY_RUN=1 prints full request bodies. No sender or state writer is called."""
    previous_state = load_sync_state(state_path)
    counts, batches = plan_upsert_batches(read_destination_records(database_path), previous_state)
    print("DRY_RUN=1: HubSpot disconnected; no requests or state changes.")
    print(" | ".join(f"{label}: {count}" for label, count in counts.items()))
    for batch in batches:
        print(json.dumps(batch, indent=2, sort_keys=True, allow_nan=False))
    print(f"Planned upsert batches: {len(batches)}")


def validate_stripe_customer_ids(customer_records):
    """Check every ID before allowing any planned actions to be printed."""
    seen_stripe_customer_ids = set()
    for row_number, customer_record in enumerate(customer_records, start=1):
        stripe_customer_id = customer_record.get("stripe_customer_id")
        if not isinstance(stripe_customer_id, str) or not stripe_customer_id.strip():
            raise ValueError(f"Row {row_number} has a missing or blank customer ID.")
        if stripe_customer_id in seen_stripe_customer_ids:
            raise ValueError(f"Duplicate customer ID: {stripe_customer_id!r}.")
        seen_stripe_customer_ids.add(stripe_customer_id)


def comparable_values(customer_record):
    """Use the payload planner's exact typed mapping for successful state."""
    return destination_values(customer_record)


def classify_customer(customer_record, previous_state):
    """Compare current values with the last values supplied for this ID."""
    stripe_customer_id = customer_record["stripe_customer_id"]
    current_values = comparable_values(customer_record)
    if stripe_customer_id not in previous_state:
        return "NEW"
    if current_values != previous_state[stripe_customer_id]:
        return "CHANGED"
    return "UNCHANGED"


def validate_state(state):
    """Reject damaged state rather than silently forgetting earlier successes."""
    if not isinstance(state, dict):
        raise ValueError("State must be a JSON object keyed by customer ID.")
    for stripe_customer_id, values in state.items():
        if not isinstance(stripe_customer_id, str) or not stripe_customer_id.strip():
            raise ValueError("State contains a missing or blank customer ID.")
        full_fields = set(DESTINATION_COLUMNS) - {"stripe_customer_id"}
        if not isinstance(values, dict) or set(values) != full_fields:
            raise ValueError(f"State has invalid or legacy fields for {stripe_customer_id!r}; "
                             "incomplete state needs explicit migration, not invented field values.")
        destination_values({"stripe_customer_id": stripe_customer_id, **values})


def load_sync_state(state_path):
    """A missing file means no successes yet; damaged files stop the run."""
    try:
        with state_path.open("r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        validate_state(state)
        return state
    except FileNotFoundError:
        print(f"No state file at {state_path}; starting with empty state.")
        return {}
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Malformed state file {state_path}: {error}") from error


def save_sync_state(state_path, state):
    """Finish a sibling temporary file before atomically replacing old state."""
    validate_state(state)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=state_path.parent,
            prefix=state_path.name + ".", suffix=".tmp", delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(state, temporary_file, indent=2, sort_keys=True, allow_nan=False)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, state_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def sync_with_simulated_sender(customer_records, state_path, simulated_sender):
    """Local demo only. Caller supplies a temporary path and a fake sender.

    Run one writer at a time. Normal preview never calls this function.
    """
    return sync_records(customer_records, state_path, simulated_sender, simulated=True)


def sync_records(customer_records, state_path, sender, simulated=False):
    """Validate the complete source first; save only individually confirmed writes.

    The caller must opt into real sending. Use one state writer at a time.
    """
    validate_stripe_customer_ids(customer_records)
    previous_state = load_sync_state(state_path)
    check_source_row_count(len(customer_records), len(previous_state))
    # Validate the entire input before simulating any sends or saving anything.
    for record in customer_records:
        validate_state({record["stripe_customer_id"]: comparable_values(record)})
    outcomes = {}
    for record in customer_records:
        stripe_customer_id = record["stripe_customer_id"]
        classification = classify_customer(record, previous_state)
        if classification == "UNCHANGED":
            outcomes[stripe_customer_id] = "UNCHANGED"
            continue
        current_values = comparable_values(record)
        try:
            succeeded = sender(stripe_customer_id, current_values.copy()) is True
        except SenderStopped:
            raise
        except Exception:
            # Never print raw HTTP exceptions, which may contain credentials.
            if not simulated:
                raise SenderStopped("Sender failed unexpectedly; pending state not advanced.") from None
            print(f"SIMULATED FAILURE | {stripe_customer_id}")
            succeeded = False
        if not succeeded:
            outcomes[stripe_customer_id] = "FAILED"
            continue
        updated_state = dict(previous_state)
        updated_state[stripe_customer_id] = current_values
        save_sync_state(state_path, updated_state)
        previous_state = updated_state
        outcomes[stripe_customer_id] = "SIMULATED SUCCESS" if simulated else "CONFIRMED SUCCESS"
    return outcomes


def print_planned_syncs(customer_records, previous_state):
    """Print classifications without updating the supplied previous state."""
    validate_state(previous_state)
    check_source_row_count(len(customer_records), len(previous_state))
    # Prepare everything first so an invalid value cannot cause a partial preview.
    planned_records = [
        (record, comparable_values(record), classify_customer(record, previous_state))
        for record in customer_records
    ]
    counts = {"NEW": 0, "CHANGED": 0, "UNCHANGED": 0}
    print("Preview only: no records will be sent or saved.")
    for record, current_values, classification in planned_records:
        stripe_customer_id = record["stripe_customer_id"]
        counts[classification] += 1
        print(
            f"{classification} | stripe_customer_id={stripe_customer_id!r} | "
            f"customer_name={record['customer_name']!r} | "
            f"subscription_status={record['subscription_status']!r} | "
            f"mrr_cents={current_values['mrr_cents']}"
        )
        if classification == "CHANGED":
            previous_values = previous_state[stripe_customer_id]
            for field_name, current_value in current_values.items():
                if previous_values[field_name] != current_value:
                    print(
                        f"  {field_name}: {previous_values[field_name]} -> {current_value}"
                    )
    print(" | ".join(f"{label}: {count}" for label, count in counts.items()))
    print(f"Planned sync actions: {counts['NEW'] + counts['CHANGED']}")


def simulated_record(customer_id, customer_name, status, mrr_cents):
    """Explicit made-up full record used only by the local simulation demo."""
    return {
        "stripe_customer_id": customer_id, "customer_name": customer_name,
        "subscription_status": status, "mrr_cents": mrr_cents,
        "company_domain": "example.com", "active_users_30d": 0, "last_event_at": None,
    }


def run_simulated_tests():
    """Exercise made-up records only; this mode does not open DuckDB."""
    print("SIMULATED TESTS: made-up customers, no database access.")
    previous_record = simulated_record("cus_test001", "Example Company", "active", 4900)
    previous_state = {previous_record["stripe_customer_id"]: comparable_values(previous_record)}
    test_cases = [
        ("New customer", simulated_record("cus_test002", "New Company", "active", 4900), "NEW"),
        ("MRR $49 -> $0", simulated_record("cus_test001", "Example Company", "active", 0), "CHANGED"),
        ("Same integer cents (4900 = 4900)", simulated_record("cus_test001", "Example Company", "active", 4900), "UNCHANGED"),
        ("Status changed", simulated_record("cus_test001", "Example Company", "canceled", 4900), "CHANGED"),
        ("Name changed", simulated_record("cus_test001", "Renamed Company", "active", 4900), "CHANGED"),
    ]
    for description, current_record, expected in test_cases:
        actual = classify_customer(current_record, previous_state)
        if actual != expected:
            raise ValueError(f"FAIL: {description}: expected {expected}, got {actual}.")
        print(f"PASS | {description} | {actual}")
    print("\nSimulated MRR-change preview (name and status stay the same):")
    print_planned_syncs([test_cases[1][1]], previous_state)
    run_state_tests()
    return 0


def run_state_tests():
    """Use real JSON persistence, but only inside an automatically cleaned folder."""
    def check(condition, description):
        if not condition:
            raise ValueError(f"FAIL | {description}")
        print(f"PASS | {description}")

    def simulated_success(stripe_customer_id, values):
        return True

    def simulated_failure(stripe_customer_id, values):
        return False

    customer = simulated_record("cus_demo001", "Demo Company", "active", 4900)
    changed_customer = simulated_record("cus_demo001", "Demo Company", "active", 0)
    with tempfile.TemporaryDirectory(prefix=".gtm_state_demo_", dir=Path(__file__).resolve().parent) as folder:
        state_path = Path(folder) / "simulated_sync_state.json"
        print("\nPERSISTENCE TESTS: temporary state only; no HubSpot connection.")
        state = load_sync_state(state_path)
        check(state == {} and not state_path.exists(), "Missing state stays missing")
        check(classify_customer(customer, state) == "NEW", "NEW customer")
        sync_with_simulated_sender([customer], state_path, simulated_success)
        state = load_sync_state(state_path)
        check(state[customer["stripe_customer_id"]]["mrr_cents"] == 4900, "Success persisted and reloaded")
        check(classify_customer(customer, state) == "UNCHANGED", "UNCHANGED customer")
        check(classify_customer(changed_customer, state) == "CHANGED", "CHANGED: $49 -> $0")

        original_bytes = state_path.read_bytes()
        outcomes = sync_with_simulated_sender([changed_customer], state_path, simulated_failure)
        check(outcomes[customer["stripe_customer_id"]] == "FAILED" and state_path.read_bytes() == original_bytes,
              "Failed update preserves saved $49 byte-for-byte")
        check(classify_customer(changed_customer, load_sync_state(state_path)) == "CHANGED",
              "Failed update remains eligible for retry")

        outcomes = sync_with_simulated_sender([changed_customer], state_path, simulated_success)
        state = load_sync_state(state_path)
        check(outcomes[customer["stripe_customer_id"]] == "SIMULATED SUCCESS" and state[customer["stripe_customer_id"]]["mrr_cents"] == 0,
              "Retry succeeds and persists $0")

        def must_not_send(stripe_customer_id, values):
            raise AssertionError("UNCHANGED customer must not be sent")

        outcomes = sync_with_simulated_sender([changed_customer], state_path, must_not_send)
        check(outcomes[customer["stripe_customer_id"]] == "UNCHANGED", "Successful update followed by UNCHANGED")
        original_bytes = state_path.read_bytes()
        with redirect_stdout(StringIO()):
            print_planned_syncs([customer], load_sync_state(state_path))
        check(state_path.read_bytes() == original_bytes, "Preview leaves saved state unchanged")

        second_customer = simulated_record("cus_demo002", "Second Company", "active", 1000)
        outcomes = sync_with_simulated_sender(
            [customer, second_customer], state_path,
            lambda stripe_customer_id, values: stripe_customer_id == second_customer["stripe_customer_id"],
        )
        state = load_sync_state(state_path)
        check(outcomes[customer["stripe_customer_id"]] == "FAILED" and state[customer["stripe_customer_id"]]["mrr_cents"] == 0
              and state[second_customer["stripe_customer_id"]]["mrr_cents"] == 1000,
              "Mixed outcomes save only the successful customer")

        original_bytes = state_path.read_bytes()
        with patch.object(os, "replace", side_effect=OSError("Simulated replacement failure")):
            try:
                save_sync_state(state_path, {})
            except OSError:
                pass
            else:
                raise ValueError("FAIL | Expected replacement failure")
        check(state_path.read_bytes() == original_bytes and not list(Path(folder).glob("*.tmp")),
              "Save failure preserves old state and cleans temporary file")

        for malformed_text in ("{broken JSON", '[]', '{"cus_bad": {"mrr": "oops"}}'):
            state_path.write_text(malformed_text, encoding="utf-8")
            try:
                load_sync_state(state_path)
            except ValueError as error:
                check("Malformed state file" in str(error)
                      and state_path.read_text(encoding="utf-8") == malformed_text,
                      "Malformed state stops clearly without overwriting it")
            else:
                raise ValueError("FAIL | Malformed state was accepted")
    print("All local state tests passed; temporary test files removed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--simulate", action="store_true",
        help="Run simulated classification and temporary JSON persistence tests.",
    )
    parser.add_argument("--write", action="store_true",
                        help="Explicitly enable real upserts to the configured HubSpot test object.")
    parser.add_argument("--database", type=Path,
                        default=Path(__file__).resolve().parent / "gtm.duckdb",
                        help="Existing database to preview in read-only mode.")
    parser.add_argument("--state", type=Path,
                        default=Path(__file__).resolve().parent / "sync_state.json",
                        help="Successful-sync state; only explicit --write updates it.")
    arguments = parser.parse_args()
    if arguments.write and (arguments.simulate or os.environ.get("DRY_RUN") == "1"):
        print("Stopped: --write cannot be combined with --simulate or DRY_RUN=1.")
        return 1
    if arguments.simulate:
        try:
            return run_simulated_tests()
        except (OSError, ValueError) as error:
            print(f"Simulation stopped: {error}")
            return 1

    # Resolve the database relative to this file, not the terminal's folder.
    database_path = arguments.database
    if not database_path.is_file():
        print(f"Preview stopped: database file not found at {database_path}")
        return 1

    try:
        if arguments.write:
            records = read_billing_records(database_path)
            previous_state = load_sync_state(arguments.state)
            # Reuse planner validation and the row-count guard before making a sender.
            counts, batches = plan_upsert_batches(records, previous_state)
            print("Write plan: " + " | ".join(f"{label}: {count}" for label, count in counts.items()))
            if not batches:
                print("No changed records; no requests or state changes.")
                return 0
            with HubSpotSender.from_environment() as sender:
                outcomes = sync_records(records, arguments.state, sender)
            successful = sum(value == "CONFIRMED SUCCESS" for value in outcomes.values())
            failed = sum(value == "FAILED" for value in outcomes.values())
            print(f"Confirmed writes: {successful}; failed records: {failed}")
            return 1 if failed else 0
        if os.environ.get("DRY_RUN") == "1":
            print_destination_payloads(database_path, arguments.state)
            return 0
        customer_records = read_billing_records(database_path)
        validate_stripe_customer_ids(customer_records)
        state_path = arguments.state
        previous_state = load_sync_state(state_path)
        print(f"Real billing data; preview reads {state_path} but never writes it.")
        print_planned_syncs(customer_records, previous_state)
    except duckdb.Error as error:
        mode = "Sync" if arguments.write else "Preview"
        print(
            f"{mode} stopped: could not read account_billing. "
            "Check that the database is available and the billing table exists."
        )
        print(f"Details: {error}")
        return 1
    except (ValueError, OSError, SenderStopped) as error:
        mode = "Sync" if arguments.write else "Preview"
        print(f"{mode} stopped: {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
