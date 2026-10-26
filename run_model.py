"""Build and check account_billing; importing this module never opens a database."""

import argparse
from pathlib import Path

import duckdb

from model_checks import MODEL_CHECKS


MODEL_PATH = Path(__file__).resolve().parent / "Models" / "account_billing.sql"


def materialize_model(connection):
    """Keep the old table if building or any quality check fails."""
    query = MODEL_PATH.read_text(encoding="utf-8")
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("CREATE OR REPLACE TABLE account_billing AS " + query)
        failures = []
        for name, check_sql in MODEL_CHECKS.items():
            invalid_rows = connection.execute(check_sql).fetchall()
            if invalid_rows:
                failures.append(f"{name}: {len(invalid_rows)} invalid row(s); example: {invalid_rows[0]!r}")
        if failures:
            raise ValueError("Model checks failed:\n" + "\n".join(failures))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    print("PASS: all billing model quality checks")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path,
                        default=Path(__file__).resolve().parent / "gtm.duckdb")
    parser.add_argument("--apply", action="store_true",
                        help="Explicitly allow rebuilding account_billing in the chosen database.")
    arguments = parser.parse_args()
    if not arguments.apply:
        print("No changes made. Rebuilding requires --apply after reviewing the database changes.")
        return 1
    if not arguments.database.is_file():
        print(f"Database does not exist: {arguments.database}")
        return 1
    try:
        connection = duckdb.connect(str(arguments.database))
        try:
            materialize_model(connection)
            print("Customers sorted by MRR (integer cents):")
            for row in connection.execute("""SELECT stripe_customer_id, customer_name, mrr_cents
                    FROM account_billing ORDER BY mrr_cents DESC""").fetchall():
                print(row)
            total = connection.execute("SELECT coalesce(sum(mrr_cents), 0) FROM account_billing").fetchone()[0]
            print(f"Total MRR: {total} cents")
        finally:
            connection.close()
    except (duckdb.Error, ValueError, OSError) as error:
        print(f"Model build stopped: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
