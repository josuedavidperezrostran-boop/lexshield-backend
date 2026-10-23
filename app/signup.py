"""Checkout for visitors without a token: prove the mailbox first, then pay.

POST /billing/checkout/start {email}
    Always 202 with the same body. In the background: if that email already has a LexShield
    subscription to use or fix, it receives a sign-in link (app/recovery.py) instead of a way to
    pay; otherwise it receives a one-time subscribe link. Same limits as recovery.
POST /billing/checkout/confirm {code}
    The code (15 minutes, consumed atomically once) proves control of the mailbox. Under a
    shared-store lock per email: if a subscription appeared meanwhile, the customer is signed in
    and no Checkout is created; otherwise the Stripe customer for that email is reused, or
    created once under an idempotency key, signed in, and sent to the coordinated Checkout
    (billing.checkout_for_customer). Every Checkout therefore belongs to a known customer.

Typing an email proves nothing; only following the link does. Without a usable store, mail
backend, Stripe mode, price and token secret, both endpoints answer 503 for everyone alike.
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
from .billing import acquire_lock, app_base, checkout_for_customer, release_lock
from .config import settings
from .mail import Message, get_mailer
from .recovery import (
    CODE_TTL_SECONDS,
    CONFIRM_PER_IP_PER_HOUR,
    RECOVER_PER_EMAIL_PER_HOUR,
    RECOVER_PER_IP_PER_HOUR,
    _client_ip,
    _digest,
    _over_limit,
    customer_with_subscription,
    customers_for_email,
    email_tag,
    send_sign_in_link,
    valid_email,
)
from .store import StoreUnavailable, get_store
from .subscriptions import PRODUCT_KEY

logger = logging.getLogger("lexshield.signup")

router = APIRouter(prefix="/billing/checkout", tags=["signup"])

GENERIC_REPLY = {
    "message": "Check your inbox: we sent a link to continue. It expires in 15 minutes."
}
_CODE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


class StartRequest(BaseModel):
    email: str


class ConfirmRequest(BaseModel):
    code: str


class ConfirmResponse(BaseModel):
    access_token: str
    # None when the email already had a subscription: sign in instead of paying.
    checkout_url: str | None = None


def _require_available() -> None:
    if not availability.email_checkout_available():
        raise HTTPException(status_code=503, detail="New subscriptions are unavailable right now.")


def send_subscribe_email(email: str) -> None:
    """Background task. Failures are logged without the address and never reach the caller."""
    tag = email_tag(email)
    try:
        existing = customer_with_subscription(email)
        if existing is not None:
            send_sign_in_link(email, existing)
            logger.info("signup.existing_subscription_sign_in_sent email=%s", tag)
            return
        code = secrets.token_urlsafe(32)
        if not get_store().add(f"signup:code:{_digest(code)}", email, CODE_TTL_SECONDS):
            raise StoreUnavailable("code collision")
        link = f"{app_base()}/subscribe#code={code}"
        get_mailer().send(
            Message(
                to=email,
                subject="Continúe su suscripción a LexShield",
                text=(
                    "Use este enlace para continuar al pago de LexShield Professional. Caduca en "
                    f"15 minutos y solo funciona una vez:\n\n{link}\n\n"
                    "Si no lo pidió, ignore este mensaje."
                ),
            )
        )
        logger.info("signup.link_sent email=%s", tag)
    except Exception as exc:  # noqa: BLE001
        logger.error("signup.failed email=%s error=%s", tag, type(exc).__name__)


@router.post("/start", status_code=202)
def start_checkout(body: StartRequest, request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    email = valid_email(body.email)
    _require_available()
    if _over_limit(f"signup:ip:{_digest(_client_ip(request))}", RECOVER_PER_IP_PER_HOUR):
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
    if not _over_limit(f"signup:email:{_digest(email.lower())}", RECOVER_PER_EMAIL_PER_HOUR):
        background_tasks.add_task(send_subscribe_email, email)
    return GENERIC_REPLY


def _customer_for_verified_email(email: str) -> str:
    """Reuse the Stripe customer for this email, or create exactly one. Caller holds the lock."""
    existing = customers_for_email(email)
    if existing:
        return existing[0]
    created = stripe.Customer.create(
        email=email,
        metadata={"product": PRODUCT_KEY},
        # Two confirmations racing past the lock (it expired) still get one customer.
        idempotency_key=hashlib.sha256(f"lexshield-customer:{email.lower()}".encode()).hexdigest(),
    )
    return created["id"] if isinstance(created, dict) else created.id


@router.post("/confirm", response_model=ConfirmResponse)
def confirm_checkout(body: ConfirmRequest, request: Request) -> ConfirmResponse:
    _require_available()
    if _over_limit(f"signup:confirm-ip:{_digest(_client_ip(request))}", CONFIRM_PER_IP_PER_HOUR):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")
    code = body.code.strip()
    if not _CODE.fullmatch(code):
        raise HTTPException(status_code=400, detail="This link is invalid or has expired.")
    try:
        email = get_store().take(f"signup:code:{_digest(code)}")
    except StoreUnavailable:
        raise HTTPException(status_code=503, detail="New subscriptions are unavailable right now.") from None
    if email is None:
        raise HTTPException(status_code=400, detail="This link is invalid or has expired.")

    lock_key = f"signup:lock:{_digest(email.lower())}"
    try:
        owner = acquire_lock(lock_key)
    except StoreUnavailable:
        raise HTTPException(status_code=503, detail="New subscriptions are unavailable right now.") from None
    try:
        stripe.api_key = settings.stripe_secret_key
        try:
            existing = customer_with_subscription(email)
            customer_id = existing or _customer_for_verified_email(email)
        except subscriptions.SubscriptionLookupError:
            raise HTTPException(status_code=503, detail="Could not reach Stripe. Request a new link shortly.") from None
        except stripe.StripeError:
            raise HTTPException(status_code=503, detail="Could not reach Stripe. Request a new link shortly.") from None
        token = issue_access_token(customer_id)
        if existing:
            return ConfirmResponse(access_token=token)
        return ConfirmResponse(access_token=token, checkout_url=checkout_for_customer(customer_id))
    finally:
        release_lock(lock_key, owner)
