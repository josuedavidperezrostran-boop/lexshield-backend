"""The demo billing bypass and default token secret never work in production."""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from app.auth import issue_access_token, token_is_valid
from app.config import DEFAULT_TOKEN_SECRET, settings
from app.main import app

SAFE_SECRET = "test-only-secret-for-unit-tests"


class BillingBypassGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = (
            settings.dev_bypass_billing,
            settings.environment,
            settings.access_token_secret,
            os.environ.get("RENDER"),
        )
        os.environ.pop("RENDER", None)
        settings.environment = ""
        settings.access_token_secret = SAFE_SECRET
        settings.dev_bypass_billing = True
        self.client = TestClient(app)

    def tearDown(self) -> None:
        bypass, environment, secret, render = self._saved
        settings.dev_bypass_billing = bypass
        settings.environment = environment
        settings.access_token_secret = secret
        if render is None:
            os.environ.pop("RENDER", None)
        else:
            os.environ["RENDER"] = render

    def _bypass(self):
        return self.client.post("/billing/verify", json={"bypass": True})

    def _analyze_without_token(self):
        # require_subscription rejects the request before any extraction or model call.
        return self.client.post(
            "/analyze",
            files={"file": ("contract.txt", b"not a real contract", "text/plain")},
        )

    def test_local_bypass_issues_token(self) -> None:
        response = self._bypass()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(token_is_valid(response.json()["access_token"]))
        self.assertFalse(self.client.get("/billing/status").json()["checkout_required"])

    def test_environment_production_disables_bypass(self) -> None:
        settings.environment = "production"
        self.assertEqual(self._bypass().status_code, 403)
        self.assertTrue(self.client.get("/billing/status").json()["checkout_required"])
        self.assertEqual(self._analyze_without_token().status_code, 401)

    def test_render_host_disables_bypass(self) -> None:
        os.environ["RENDER"] = "true"
        self.assertEqual(self._bypass().status_code, 403)
        self.assertEqual(self._analyze_without_token().status_code, 401)

    def test_bypass_off_by_default(self) -> None:
        settings.dev_bypass_billing = False
        self.assertEqual(self._bypass().status_code, 403)
        self.assertEqual(self._analyze_without_token().status_code, 401)

    def test_production_rejects_default_secret(self) -> None:
        settings.access_token_secret = DEFAULT_TOKEN_SECRET
        forged = issue_access_token()  # signed with the public default while off production
        settings.environment = "production"
        self.assertFalse(token_is_valid(forged))
        self.assertEqual(self._bypass().status_code, 503)
        with self.assertRaises(RuntimeError):
            issue_access_token()

    def test_production_accepts_private_secret(self) -> None:
        settings.environment = "production"
        self.assertTrue(token_is_valid(issue_access_token()))


if __name__ == "__main__":
    unittest.main()
