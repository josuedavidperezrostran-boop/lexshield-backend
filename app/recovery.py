"""Account recovery by proof of mailbox control.

POST /billing/recover {email}
    Always answers 202 with the same body, whether or not the email has a subscription. The
    Stripe lookup, code creation and email happen in a background task, so the response time
    does not depend on the account either. Limits: RECOVER_PER_IP_PER_HOUR requests per client
    IP (429 when exceeded, the same for everyone) and RECOVER_PER_EMAIL_PER_HOUR emails per
    address (extra requests are silently dropped).
POST /billing/recover/confirm {code}
    The code is 32 random bytes, stored only as a SHA-256 hash in the shared store for
    CODE_TTL_SECONDS and consumed with an atomic take: it works once. A valid code for a customer
    who still has a subscription to use or fix starts a new session (app/auth.py).

Typing an email proves nothing; only the link sent to that mailbox does. Needs a shared store
(app/store.py) and a mail backend (app/mail.py); without either, both endpoints answer 503 for
every request alike.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets

import stripe
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from . import availability, subscriptions
from .auth import issue_access_token
from .billing import AccessResponse, app_base
from .config import settings
from .mail import Message, get_mailer
from .store import StoreUnavailable, get_store

logger = logging.getLogger("lexshield.recovery")

router = APIRouter(prefix="/billing/recover", tags=["recovery"])

CODE_TTL_SECONDS = 15 * 60
RECOVER_PER_IP_PER_HOUR = 10
RECOVER_PER_EMAIL_PER_HOUR = 3
CONFIRM_PER_IP_PER_HOUR = 30
GENERIC_REPLY = {
    "message": "If this email has a LexShield subscription, a sign-in link is on its way. It expires in 15 minutes."
}

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+$")
_CODE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


class RecoverRequest(BaseModel):
    email: str


class ConfirmRequest(BaseModel):
    code: str


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def valid_email(raw: str) -> str:
    email = raw.strip()
    if not _EMAIL.fullmatch(email.lower()) or len(email) > 254:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")
    return email


def _client_ip(request: Request) -> str:
    if settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _over_limit(key: str, limit: int) -> bool:
    try:
        return get_store().incr(key, 3600) > limit
    except StoreUnavailable:
        raise HTTPException(status_code=503, detail="Account recovery is unavailable right now.") from None


def _require_backends() -> None:
    if not availability.email_flows_available():
        raise HTTPException(status_code=503, detail="Account recovery is unavailable right now.")


def customers_for_email(email: str) -> list[str]:
    """Stripe customers whose email matches (as typed and lower-cased). Raises on Stripe errors."""
    stripe.api_key = settings.stripe_secret_key
    candidates: list[str] = []
    for query in dict.fromkeys([email, email.lower()]):
        listed = stripe.Customer.list(email=query, limit=10)
        for customer in listed.get("data", []) if isinstance(listed, dict) else listed.data:
            cid = customer["id"] if isinstance(customer, dict) else customer.id
            if cid not in candidates:
                candidates.append(cid)
    return candidates


def customer_with_subscription(email: str) -> str | None:
    best: str | None = None
    for cid in customers_for_email(email):
        state = subscriptions.access_state(cid, fresh=True)
        if state.access:
            return cid
        if state.has_open_subscription and best is None:
            best = cid
    return best


def email_tag(email: str) -> str:
    """Short hash for logs; addresses are never logged."""
    return _digest(email.lower())[:12]


def send_sign_in_link(email: str, customer_id: str) -> None:
    """Store a one-time code for customer_id and mail its link. Raises on store or mail errors."""
    code = secrets.token_urlsafe(32)
    if not get_store().add(f"recover:code:{_digest(code)}", customer_id, CODE_TTL_SECONDS):
        raise StoreUnavailable("code collision")
    link = f"{app_base()}/recover#code={code}"
    get_mailer().send(
        Message(
            to=email,
            subject="Su enlace de acceso a LexShield",
            text=(
                "Use este enlace para volver a entrar en LexShield. Caduca en 15 minutos y "
                f"solo funciona una vez:\n\n{link}\n\n"
                "Si no lo pidió, ignore este mensaje: nadie puede entrar sin él."
            ),
        )
    )


def send_recovery_email(email: str) -> None:
    """Background task. Failures are logged without the address and never reach the caller."""
    tag = email_tag(email)
    try:
        customer_id = customer_with_subscription(email)
        if customer_id is None:
            logger.info("recovery.no_subscription email=%s", tag)
            return
        send_sign_in_link(email, customer_id)
        logger.info("recovery.sent email=%s customer=%s", tag, customer_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("recovery.failed email=%s error=%s", tag, type(exc).__name__)


@router.post("", status_code=202)
def request_recovery(body: RecoverRequest, request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    email = valid_email(body.email)
    _require_backends()
    if _over_limit(f"recover:ip:{_digest(_client_ip(request))}", RECOVER_PER_IP_PER_HOUR):
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
    if not _over_limit(f"recover:email:{_digest(email.lower())}", RECOVER_PER_EMAIL_PER_HOUR):
        background_tasks.add_task(send_recovery_email, email)
    return GENERIC_REPLY


@router.post("/confirm", response_model=AccessResponse)
def confirm_recovery(body: ConfirmRequest, request: Request) -> AccessResponse:
    _require_backends()
    if _over_limit(f"recover:confirm-ip:{_digest(_client_ip(request))}", CONFIRM_PER_IP_PER_HOUR):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    code = body.code.strip()
    if not _CODE.fullmatch(code):
        raise HTTPException(status_code=400, detail="This sign-in link is invalid or has expired.")
    try:
        customer_id = get_store().take(f"recover:code:{_digest(code)}")
    except StoreUnavailable:
        raise HTTPException(status_code=503, detail="Account recovery is unavailable right now.") from None
    if customer_id is None:
        raise HTTPException(status_code=400, detail="This sign-in link is invalid or has expired.")
    try:
        state = subscriptions.access_state(customer_id, fresh=True)
    except subscriptions.SubscriptionLookupError:
        raise HTTPException(
            status_code=503,
            detail="Could not confirm the subscription with Stripe. Request a new link shortly.",
        ) from None
    if not state.has_open_subscription:
        raise HTTPException(status_code=403, detail="No LexShield subscription for this account.")
    return AccessResponse(access_token=issue_access_token(customer_id), subscription=state.as_dict())
