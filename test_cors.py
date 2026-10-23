"""CORS preflight allows only the configured frontend origin.

The app reads FRONTEND_ORIGIN once, when app.main is imported. Other tests import the app
first, so these checks build the same CORS settings after changing the configuration
instead of depending on import order.
"""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app, cors_options

CONFIGURED = "  https://desk.example.test/  "
ORIGIN = "https://desk.example.test"
REJECTED = (
    "https://evil-example.vercel.app",
    "https://example.com",
)


def _probe_app() -> FastAPI:
    probe = FastAPI()
    probe.add_middleware(CORSMiddleware, **cors_options())

    @probe.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return probe


class CorsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._origin = settings.frontend_origin
        settings.frontend_origin = CONFIGURED
        self.client = TestClient(_probe_app())

    def tearDown(self) -> None:
        settings.frontend_origin = self._origin

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

    def test_empty_origin_falls_back_to_the_public_site(self) -> None:
        settings.frontend_origin = "   "
        self.assertEqual(cors_options()["allow_origins"], ["https://frontend-1-lex-shield2.vercel.app"])

    def test_app_uses_cors_options(self) -> None:
        cors = [entry for entry in app.user_middleware if entry.cls is CORSMiddleware]
        self.assertEqual(len(cors), 1)
        self.assertEqual(set(cors[0].kwargs), set(cors_options()))
        self.assertEqual(len(cors[0].kwargs["allow_origins"]), 1)


if __name__ == "__main__":
    unittest.main()
