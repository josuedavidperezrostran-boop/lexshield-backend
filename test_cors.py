"""CORS preflight allows only the configured frontend origin."""

from __future__ import annotations

import os
import unittest

# Origins are captured when the middleware is built, so this must precede the app import.
os.environ["FRONTEND_ORIGIN"] = "  https://desk.example.test/  "

from fastapi.testclient import TestClient

from app.main import app

ORIGIN = "https://desk.example.test"
REJECTED = (
    "https://evil-example.vercel.app",
    "https://example.com",
)


class CorsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def _preflight(self, origin: str) -> object:
        return self.client.options(
            "/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )

    def test_configured_origin_is_allowed(self) -> None:
        response = self._preflight(ORIGIN)
        self.assertEqual(response.headers.get("access-control-allow-origin"), ORIGIN)

    def test_unconfigured_origins_are_rejected(self) -> None:
        for origin in REJECTED:
            with self.subTest(origin=origin):
                response = self._preflight(origin)
                self.assertIsNone(response.headers.get("access-control-allow-origin"))


if __name__ == "__main__":
    unittest.main()
