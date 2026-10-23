from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware

from .auditor import (
    apply_truncation_to_integrity,
    build_reference_map,
    compile_partner_opinion,
    scan_financial_and_ip,
    scan_jurisdiction,
    supervise_matrix,
)
from .billing import router as billing_router
from .billing import state_for_authorization
from .compare import compare_contracts
from .config import settings
from .extract import extract_text
from .webhooks import router as stripe_webhook_router
from .paddle_webhooks import router as paddle_webhook_router

app = FastAPI(
    title="LexShield",
    description=(
        "Ephemeral review on gpt-4o. Each desk reads the whole upload once, inside the 128k window, "
        "and the senior partner checks the opening against the signature block. Nothing is written to disk."
    ),
    version="1.1.0",
)

def _allowed_origins() -> list[str]:
    configured = (settings.frontend_origin or "").strip().rstrip("/")
    if not configured:
        configured = "https://frontend-1-lex-shield2.vercel.app"
    return [configured]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(billing_router)
app.include_router(billing_router, prefix="/api")
app.include_router(stripe_webhook_router)
app.include_router(paddle_webhook_router)


def require_subscription(authorization: str | None = Header(default=None)) -> None:
    if settings.billing_bypass_enabled():
        return
    state = state_for_authorization(authorization)
    if state.access:
        return
    if state.payment_required:
        raise HTTPException(
            status_code=402,
            detail="Payment failed. Update the payment method in the customer portal.",
        )
    raise HTTPException(status_code=403, detail="LexShield Professional subscription is not active.")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "retention": "ephemeral"}


@app.post("/analyze")
async def analyze_contract(
    file: UploadFile = File(...),
    _: None = Depends(require_subscription),
) -> dict:
    payload = await file.read()
    # Drop the upload handle immediately; nothing is written to disk.
    await file.close()

    if not payload:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MB in-memory limit.")

    text = await run_in_threadpool(
        extract_text, file.filename or "contract", file.content_type, payload
    )
    del payload

    truncated = False
    if len(text) > settings.max_contract_chars:
        text = text[: settings.max_contract_chars]
        truncated = True

    # Single full-context pass per desk. The text is not split into windows.
    audit = await run_in_threadpool(_review_with_critic, text, file.filename or "contract")
    apply_truncation_to_integrity(audit, truncated)
    return {
        "filename": file.filename,
        "truncated": truncated,
        "contract_text": text,
        **audit,
    }


@app.post("/api/contracts/compare")
async def compare_versions(
    version_a: UploadFile = File(...),
    version_b: UploadFile = File(...),
    _: None = Depends(require_subscription),
) -> dict:
    """Compare two uploads in memory. Nothing is written to disk."""
    payload_a = await version_a.read()
    payload_b = await version_b.read()
    await version_a.close()
    await version_b.close()
    text_a = await run_in_threadpool(
        _read_upload, version_a.filename, version_a.content_type, payload_a
    )
    text_b = await run_in_threadpool(
        _read_upload, version_b.filename, version_b.content_type, payload_b
    )
    del payload_a, payload_b
    report = await run_in_threadpool(compare_contracts, text_a, text_b)
    return {
        "version_a": version_a.filename,
        "version_b": version_b.filename,
        **report,
    }


def _read_upload(filename: str | None, content_type: str | None, payload: bytes) -> str:
    if not payload:
        raise HTTPException(status_code=400, detail="Empty file.")
    if len(payload) > settings.max_upload_bytes:
        raise HTTPException(status_code=413, detail="File exceeds the 10 MB in-memory limit.")
    text = extract_text(filename or "contract", content_type, payload)
    if len(text) > settings.max_contract_chars:
        text = text[: settings.max_contract_chars]
    return text


def _review_with_critic(contract_text: str, filename: str) -> dict:
    """Pass 1 stores the opening map in memory. Pass 2 reads the closing clauses against that map."""
    reference_map = build_reference_map(contract_text)
    with ThreadPoolExecutor(max_workers=2) as pool:
        auditor = pool.submit(scan_financial_and_ip, contract_text, filename, reference_map)
        litigator = pool.submit(scan_jurisdiction, contract_text, filename, reference_map)
        auditor_notes = auditor.result()
        litigator_notes = litigator.result()
    consolidated = compile_partner_opinion(
        contract_text,
        filename,
        auditor_notes,
        litigator_notes,
        reference_map,
    )
    return supervise_matrix(contract_text, filename, consolidated, reference_map)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
    )
