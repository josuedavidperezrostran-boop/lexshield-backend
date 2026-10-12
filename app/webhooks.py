"""Stripe webhook. Logs transaction ids only; contract text stays out of the log."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from .config import settings
from .partners import PREMIUM_PARTNER_TIER, PartnerOrder, upsert_partner

logger = logging.getLogger("lexshield.webhooks")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

_PRODUCT = "lexshield-professional"
_OPERATIONAL_METADATA_KEYS = frozenset({"product", "plan", "tier"})
_STRIPE_ID = re.compile(r"^(cs|cus|sub|pi|evt|in|ch|seti|price|prod)_[A-Za-z0-9_]+$")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")
_SAFE_METADATA = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CURRENCY = re.compile(r"^[a-z]{3}$")

_SignatureError = getattr(stripe, "SignatureVerificationError", None)
if _SignatureError is None:
    _SignatureError = stripe.error.SignatureVerificationError


def webhook_secret_configured() -> bool:
    secret = settings.stripe_webhook_secret.strip()
    return secret.startswith("whsec_") and "your_webhook" not in secret


def _stripe_id(value: object, prefix: str) -> str | None:
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str) or len(value) > 255:
        return None
    if value.startswith(prefix + "_") and _STRIPE_ID.fullmatch(value):
        return value
    return None


def _operational_metadata(metadata: object) -> dict[str, str]:
    if not isinstance(metadata, dict):
        return {}
    clean: dict[str, str] = {}
    for key in _OPERATIONAL_METADATA_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and _SAFE_METADATA.fullmatch(value):
            clean[key] = value
    return clean


def _email(session: dict) -> str | None:
    details = session.get("customer_details")
    if not isinstance(details, dict):
        return None
    email = details.get("email")
    if isinstance(email, str) and _EMAIL.fullmatch(email):
        return email
    return None


def _provisioned_at(created: object) -> str | None:
    if isinstance(created, bool) or not isinstance(created, int):
        return None
    if created < 0:
        return None
    return datetime.fromtimestamp(created, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def completed_checkout_order(event: dict) -> PartnerOrder | None:
    """Return a partner row only for a paid LexShield subscription checkout."""
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    session = data.get("object")
    if not isinstance(session, dict):
        return None

    event_id = _stripe_id(event.get("id"), "evt")
    session_id = _stripe_id(session.get("id"), "cs")
    customer_id = _stripe_id(session.get("customer"), "cus")
    provisioned_at = _provisioned_at(event.get("created"))
    metadata = _operational_metadata(session.get("metadata"))
    payment_status = session.get("payment_status")
    status = session.get("status")
    paid = payment_status in {"paid", "no_payment_required"}
    complete = status == "complete"
    if (
        event_id is None
        or session_id is None
        or customer_id is None
        or provisioned_at is None
        or session.get("mode") != "subscription"
        or metadata.get("product") != _PRODUCT
        or not (paid or complete)
        or not isinstance(payment_status, str)
    ):
        return None

    amount_total = session.get("amount_total")
    if isinstance(amount_total, bool) or not isinstance(amount_total, int):
        amount_total = None
    currency = session.get("currency")
    if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
        currency = None

    return PartnerOrder(
        event_id=event_id,
        checkout_session_id=session_id,
        customer_id=customer_id,
        subscription_id=_stripe_id(session.get("subscription"), "sub"),
        payment_intent_id=_stripe_id(session.get("payment_intent"), "pi"),
        email=_email(session),
        tier=PREMIUM_PARTNER_TIER,
        payment_status=payment_status,
        amount_total=amount_total,
        currency=currency,
        provisioned_at=provisioned_at,
    )


def provision_premium_partner(order: PartnerOrder) -> None:
    """Persist the institutional entitlement. The same order rewrites the same row."""
    try:
        upsert_partner(order)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "stripe.partner.provision_failed event_id=%s session_id=%s customer_id=%s error_type=%s",
            order.event_id,
            order.checkout_session_id,
            order.customer_id,
            type(exc).__name__,
        )
        return
    logger.info(
        "stripe.partner.provisioned event_id=%s session_id=%s customer_id=%s "
        "subscription_id=%s payment_intent=%s tier=%s amount_total=%s currency=%s",
        order.event_id,
        order.checkout_session_id,
        order.customer_id,
        order.subscription_id,
        order.payment_intent_id,
        order.tier,
        order.amount_total,
        order.currency,
    )


def _log_checkout(order: PartnerOrder, metadata: dict[str, str]) -> None:
    logger.info(
        "stripe.checkout.session.completed event_id=%s session_id=%s customer_id=%s "
        "subscription_id=%s payment_intent=%s payment_status=%s amount_total=%s "
        "currency=%s metadata=%s tier=%s",
        order.event_id,
        order.checkout_session_id,
        order.customer_id,
        order.subscription_id,
        order.payment_intent_id,
        order.payment_status,
        order.amount_total,
        order.currency,
        metadata,
        order.tier,
    )


@router.post("/stripe")
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, bool]:
    """Verify the raw body against stripe-signature, then provision on a paid checkout."""
    if not webhook_secret_configured():
        logger.error("stripe.webhook secret_not_configured")
        raise HTTPException(status_code=503, detail="STRIPE_WEBHOOK_SECRET is not configured.")

    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    if not signature:
        logger.warning("stripe.webhook missing_signature")
        raise HTTPException(status_code=400, detail="Missing stripe-signature header.")

    try:
        stripe.Webhook.construct_event(
            payload,
            signature,
            settings.stripe_webhook_secret.strip(),
        )
        event = json.loads(payload.decode("utf-8"))
    except _SignatureError:
        logger.warning("stripe.webhook signature_verification_failed")
        raise HTTPException(status_code=400, detail="Invalid Stripe signature.") from None
    except (UnicodeDecodeError, ValueError):
        logger.warning("stripe.webhook invalid_payload")
        raise HTTPException(status_code=400, detail="Invalid webhook payload.") from None

    if not isinstance(event, dict):
        logger.warning("stripe.webhook invalid_payload")
        raise HTTPException(status_code=400, detail="Invalid webhook payload.")

    event_id = _stripe_id(event.get("id"), "evt") or "unparsed"
    event_type = event.get("type") if isinstance(event.get("type"), str) else "unparsed"
    logger.info(
        "stripe.webhook event_id=%s type=%s livemode=%s",
        event_id,
        event_type,
        event.get("livemode") if isinstance(event.get("livemode"), bool) else None,
    )

    if event_type != "checkout.session.completed":
        return {"received": True}

    order = completed_checkout_order(event)
    if order is None:
        logger.info("stripe.webhook ignored event_id=%s reason=unconfirmed_or_unscoped", event_id)
        return {"received": True}

    data = event.get("data")
    session = data.get("object") if isinstance(data, dict) else None
    metadata = session.get("metadata") if isinstance(session, dict) else None
    _log_checkout(order, _operational_metadata(metadata))
    background_tasks.add_task(provision_premium_partner, order)
    return {"received": True}
