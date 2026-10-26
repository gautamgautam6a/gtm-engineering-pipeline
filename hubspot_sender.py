"""One-customer HubSpot upserts. Construct this sender only after --write."""

from email.utils import parsedate_to_datetime
import math
import os
import time

import requests

from hubspot_payloads import to_upsert_input


OBJECT_TYPE_ID = "2-270744393"
EXPECTED_PORTAL_ID = 247794315
ACCOUNT_DETAILS_URL = "https://api.hubapi.com/account-info/v3/details"
UPSERT_URL = f"https://api.hubapi.com/crm/v3/objects/{OBJECT_TYPE_ID}/batch/upsert"


class SenderStopped(RuntimeError):
    """A sanitized error: stop this run without advancing the pending record."""


class HubSpotSender:
    """Send one input at a time, so each success can be saved independently."""

    def __init__(self, key, session=None, sleep=None, clock=None):
        if not isinstance(key, str) or not key.strip():
            raise ValueError("HUBSPOT_API_KEY is missing; no requests made.")
        self._key = key
        self._account_verified = False
        self.session = session if session is not None else requests.Session()
        # No hidden library retries: the bounded loop below owns retries.
        if session is None:
            self.session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        self.sleep = sleep if sleep is not None else time.sleep
        self.clock = clock if clock is not None else time.time
        self.max_attempts = 3
        self.max_wait_seconds = 30

    @classmethod
    def from_environment(cls):
        # Never print the key, request headers, raw exceptions, or response bodies.
        return cls(os.environ.get("HUBSPOT_API_KEY", ""))

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.session.close()

    def verify_account(self):
        """Fail closed unless the credential identifies our Developer test portal.

        Official endpoint: GET /account-info/v3/details. Never log its body.
        Cache only within this sender session; a new run must verify again.
        """
        if self._account_verified:
            return
        try:
            response = self.session.get(
                ACCOUNT_DETAILS_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=(10, 30), allow_redirects=False,
            )
        except requests.RequestException:
            raise SenderStopped("Account identity request failed; no record writes permitted.") from None
        try:
            if response.status_code != 200:
                raise SenderStopped(
                    f"Account identity check returned HTTP {response.status_code}; no record writes permitted."
                )
            try:
                details = response.json()
            except ValueError:
                raise SenderStopped("Invalid account identity response; no record writes permitted.") from None
            if (not isinstance(details, dict) or type(details.get("portalId")) is not int
                    or details["portalId"] != EXPECTED_PORTAL_ID
                    or details.get("accountType") != "DEVELOPER_TEST"):
                raise SenderStopped("Expected Developer test portal not verified; no record writes permitted.")
            self._account_verified = True
        finally:
            response.close()

    def retry_delay(self, retry_after, attempt):
        """Honor seconds or an HTTP date. Never retry earlier than requested."""
        if retry_after is None:
            return float(2 ** attempt)
        try:
            delay = float(retry_after)
        except (ValueError, TypeError):
            try:
                delay = parsedate_to_datetime(retry_after).timestamp() - self.clock()
            except (ValueError, TypeError, OverflowError):
                raise SenderStopped("Invalid Retry-After header; stopping safely.") from None
        if not math.isfinite(delay) or delay > self.max_wait_seconds:
            raise SenderStopped("Rate-limit wait exceeds this run's budget; retry later.")
        return max(0.0, delay)

    def confirmed_success(self, data, customer_id):
        """HTTP success alone is insufficient: require a completed matching result."""
        if not isinstance(data, dict):
            raise SenderStopped("Unrecognized response; write outcome is unverified.")
        if data.get("errors") or data.get("numErrors", 0) != 0:
            return False
        if data.get("status") != "COMPLETE":
            raise SenderStopped("Batch is not complete; write outcome is unverified.")
        results = data.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise SenderStopped("Expected one successful result; write outcome is unverified.")
        result = results[0]
        properties = result.get("properties")
        if (not isinstance(result.get("id"), str) or not result["id"].strip()
                or not isinstance(properties, dict)
                or properties.get("stripe_customer_id") != customer_id
                or result.get("errors") or result.get("archived") is True):
            raise SenderStopped("Result does not confirm the intended customer; state not advanced.")
        return True

    def __call__(self, customer_id, values):
        body = {"inputs": [to_upsert_input({**values, "stripe_customer_id": customer_id})]}
        self.verify_account()
        for attempt in range(self.max_attempts):
            try:
                response = self.session.post(
                    UPSERT_URL,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json=body, timeout=(10, 30), allow_redirects=False,
                )
            except requests.Timeout:
                raise SenderStopped("Request timed out; outcome unknown. Verify before retrying.") from None
            except requests.RequestException:
                raise SenderStopped("Connection failed; outcome unknown. Verify before retrying.") from None

            try:
                status = response.status_code
                retry_after = response.headers.get("Retry-After")
                if status in (200, 201, 207):
                    try:
                        data = response.json()
                    except ValueError:
                        raise SenderStopped("Invalid JSON response; write outcome is unverified.") from None
                    return self.confirmed_success(data, customer_id)
            finally:
                response.close()

            if status in (401, 403):
                raise SenderStopped(f"HTTP {status}: authentication or permission denied; run stopped.")
            if status in (400, 422):
                print(f"HTTP {status}: record rejected for {customer_id}; state not advanced.")
                return False
            if status == 429 or 500 <= status < 600:
                if attempt + 1 == self.max_attempts:
                    raise SenderStopped(f"HTTP {status}: retry limit reached; state not advanced.")
                delay = self.retry_delay(retry_after, attempt)
                print(f"HTTP {status}: retrying {customer_id} after {delay:g} seconds.")
                self.sleep(delay)
                continue
            # Includes redirects and async 202 responses; never follow or assume success.
            raise SenderStopped(f"HTTP {status}: write not confirmed; run stopped.")
