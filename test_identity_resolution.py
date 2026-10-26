"""Lesson identity exercise using sample contacts and the real SQL in memory.

No CRM contact source currently exists in the project. raw_crm_contacts is an
explicit test fixture, not a claim that product usage events are CRM contacts.
"""

from pathlib import Path
import unittest
from unittest.mock import patch

import test_account_billing as billing_fixtures


class IdentityResolutionTests(unittest.TestCase):
    add_customer = billing_fixtures.AccountBillingTests.add_customer
    build_model = billing_fixtures.AccountBillingTests.build_model

    def setUp(self):
        billing_fixtures.AccountBillingTests.setUp(self)
        # email and first_name are the lesson's sample contact schema.
        # Only email is required by the identity SELECT.
        self.connection.execute("CREATE TABLE raw_crm_contacts (email VARCHAR, first_name VARCHAR)")
        self.identity_sql = (Path(__file__).parent / "Models" / "contact_account_matches.sql").read_text()
        network = patch("socket.socket.connect", side_effect=AssertionError("No network allowed"))
        network.start()
        self.addCleanup(network.stop)

    def add_contacts(self, emails):
        self.connection.executemany("INSERT INTO raw_crm_contacts VALUES (?, ?)",
                                    [(email, "Sample contact") for email in emails])

    def matches(self):
        return self.connection.execute(self.identity_sql).fetchall()

    def test_sample_contacts_match_or_remain_unmatched(self):
        self.add_customer("cus_acme", "billing@acme.io")
        self.add_customer("cus_globex", "billing@globex.com")
        self.add_customer("cus_gmail", "billing@gmail.com")
        self.build_model()
        self.add_contacts(["ana@acme.io", "bo@globex.com", "cy@gmail.com",
                           "dee@unknown.example", "eli@acme.io"])
        self.assertEqual(self.matches(), [
            ("ana@acme.io", "cus_acme", "domain match"),
            ("bo@globex.com", "cus_globex", "domain match"),
            ("cy@gmail.com", None, "no account"),
            ("dee@unknown.example", None, "no account"),
            ("eli@acme.io", "cus_acme", "domain match"),
        ])

    def test_domain_matching_is_case_insensitive(self):
        self.add_customer("cus_acme", "Billing@ACME.IO")
        self.build_model()
        self.add_contacts(["ANA@AcMe.Io"])
        self.assertEqual(self.matches(), [("ANA@AcMe.Io", "cus_acme", "domain match")])

    def test_all_lesson_free_mail_domains_are_excluded(self):
        free_domains = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me"]
        for index, domain in enumerate(free_domains):
            self.add_customer(f"cus_free_{index}", f"billing@{domain}")
        self.build_model()
        self.assertEqual(self.connection.execute(
            "SELECT count(*) FROM account_billing WHERE company_domain IS NOT NULL").fetchone()[0], 0)
        self.add_contacts([f"person@{domain.upper()}" for domain in free_domains])
        self.assertEqual(len(self.matches()), len(free_domains))
        self.assertTrue(all(row[1:] == (None, "no account") for row in self.matches()))

    def test_contact_side_exclusion_also_blocks_incorrect_free_mail_account(self):
        self.add_customer("cus_bad", "billing@gmail.com")
        self.build_model()
        # Deliberately corrupt only the in-memory model to test the join's guard.
        self.connection.execute("UPDATE account_billing SET company_domain = 'gmail.com'")
        self.add_contacts(["someone@gmail.com"])
        self.assertEqual(self.matches(), [("someone@gmail.com", None, "no account")])

    def test_missing_email_and_unknown_domain_are_preserved(self):
        self.add_customer()
        self.build_model()
        self.add_contacts([None, "", "no-at-sign", "person@unknown.example"])
        rows = self.matches()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row[1:] == (None, "no account") for row in rows))

    def test_subdomain_is_not_silently_treated_as_parent_domain(self):
        self.add_customer("cus_acme", "billing@acme.io")
        self.build_model()
        self.add_contacts(["person@team.acme.io"])
        self.assertEqual(self.matches(), [("person@team.acme.io", None, "no account")])

    def test_shared_company_domain_returns_candidates_not_a_guessed_winner(self):
        self.add_customer("cus_one", "billing@shared.example")
        self.add_customer("cus_two", "finance@shared.example")
        self.build_model()
        self.add_contacts(["person@shared.example"])
        self.assertEqual(self.matches(), [
            ("person@shared.example", "cus_one", "domain match"),
            ("person@shared.example", "cus_two", "domain match"),
        ])

    def test_select_does_not_change_contacts_or_billing_rows(self):
        self.add_customer()
        self.build_model()
        self.add_contacts(["person@acme.io", "person@gmail.com"])
        contacts_before = self.connection.execute("SELECT * FROM raw_crm_contacts").fetchall()
        accounts_before = self.connection.execute("SELECT * FROM account_billing").fetchall()
        self.matches()
        self.assertEqual(self.connection.execute("SELECT * FROM raw_crm_contacts").fetchall(), contacts_before)
        self.assertEqual(self.connection.execute("SELECT * FROM account_billing").fetchall(), accounts_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
