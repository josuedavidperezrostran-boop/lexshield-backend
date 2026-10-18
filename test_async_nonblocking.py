"""Health stays responsive while /analyze runs synchronous work off the event loop."""

from __future__ import annotations

import asyncio
import io
import time
import unittest

import httpx
from docx import Document
from httpx import ASGITransport

import app.main as app_main
from app.config import settings
from app.main import app, require_subscription

_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes() -> bytes:
    document = Document()
    document.add_paragraph(
        "This dummy agreement is long enough to pass the readable text minimum."
    )
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _slow_review(contract_text: str, filename: str) -> dict:
    time.sleep(2)
    return {"status": "stub"}


def _allow_subscription() -> None:
    return None


class AsyncNonblockingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._review = app_main._review_with_critic
        self._bypass = settings.dev_bypass_billing
        self._overrides = dict(app.dependency_overrides)
        settings.dev_bypass_billing = False
        app_main._review_with_critic = _slow_review

    def tearDown(self) -> None:
        app_main._review_with_critic = self._review
        settings.dev_bypass_billing = self._bypass
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self._overrides)

    def test_health_returns_while_analyze_is_running(self) -> None:
        asyncio.run(self._health_during_analyze())

    def test_analyze_without_token_is_rejected(self) -> None:
        asyncio.run(self._unauthorized_analyze())

    async def _health_during_analyze(self) -> None:
        app.dependency_overrides[require_subscription] = _allow_subscription
        payload = _docx_bytes()
        files = {"file": ("contract.docx", payload, _DOCX_TYPE)}
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.perf_counter()
            analyze_task = asyncio.create_task(client.post("/analyze", files=files))
            await asyncio.sleep(0.05)
            health_started = time.perf_counter()
            health = await client.get("/health")
            health_elapsed = time.perf_counter() - health_started
            total_elapsed = time.perf_counter() - started
            analyze_still_running = not analyze_task.done()
            print(
                f"health_elapsed={health_elapsed:.3f}s total_before_health_done={total_elapsed:.3f}s "
                f"analyze_still_running={analyze_still_running} health={health.status_code}",
                flush=True,
            )
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json(), {"status": "ok", "retention": "ephemeral"})
            self.assertLess(health_elapsed, 1.0)
            self.assertLess(total_elapsed, 1.0)
            self.assertTrue(analyze_still_running)

            analyze = await analyze_task

        self.assertEqual(analyze.status_code, 200)
        body = analyze.json()
        self.assertEqual(body["filename"], "contract.docx")
        self.assertIs(body["truncated"], False)
        self.assertIsInstance(body["contract_text"], str)
        self.assertGreater(len(body["contract_text"]), 0)
        self.assertEqual(body["status"], "stub")
        self.assertEqual(
            set(body),
            {"filename", "truncated", "contract_text", "status"},
        )

    async def _unauthorized_analyze(self) -> None:
        app.dependency_overrides.pop(require_subscription, None)
        files = {"file": ("contract.docx", _docx_bytes(), _DOCX_TYPE)}
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/analyze", files=files)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "LexShield Professional subscription required.")


if __name__ == "__main__":
    unittest.main()
