from __future__ import annotations

import hashlib
import threading
import time
from typing import Any

import stripe
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from . import sessions, subscriptions
from .auth import (
    TokenInfo,
    bearer_token,
    issue_access_token,
    parse_token,
    token_is_refreshable,
    token_is_valid,
    tokens_available,
)
from .config import settings
from .store import StoreUnavailable
from .subscriptions import PRODUCT_KEY, AccessState, SubscriptionLookupError

router = APIRouter(prefix="/billing", tags=["billing"])

# A Checkout session_id signs a customer in once, right after paying. It is not a lasting login.
VERIFY_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
# Repeated or simultaneous checkout requests for one customer inside this window reuse one session.
CHECKOUT_IDEMPOTENCY_WINDOW_SECONDS = 10 * 60


class CheckoutResponse(BaseModel):
    checkout_url: str


class VerifyRequest(BaseModel):
    session_id: str | None = None
    bypass: bool = False


class AccessResponse(BaseModel):
    access_token: str
    plan: str = "professional"
    amount_usd: int = 100
    subscription: dict[str, object] | None = None


ACCESS_MODEL = "stripe-subscription-v1"
FOUNDER_COUPON_ID = "founder80_first_month"
FOUNDER_PROMO_CODE = "FOUNDER80"
DEFAULT_APP = "https://frontend-1-lex-shield2.vercel.app"


def app_base() -> str:
    """Public site origin. FRONTEND_ORIGIN overrides this when a custom domain is attached."""
    configured = settings.frontend_origin.strip().rstrip("/")
    return configured or DEFAULT_APP


def ensure_founder_promotion() -> None:
    """80% off the first invoice only. Month two bills the full $100."""
    try:
        coupon = stripe.Coupon.retrieve(FOUNDER_COUPON_ID)
    except stripe.InvalidRequestError:
        coupon = stripe.Coupon.create(
            id=FOUNDER_COUPON_ID,
            percent_off=80,
            duration="once",
            name="Founder launch — first month",
        )
    if coupon.percent_off != 80 or coupon.duration != "once":
        raise HTTPException(
            status_code=502,
            detail="FOUNDER80 coupon exists with the wrong duration. Use 80% once.",
        )

    existing = stripe.PromotionCode.list(code=FOUNDER_PROMO_CODE, active=True, limit=1)
    if existing.data:
        return
    stripe.PromotionCode.create(
        code=FOUNDER_PROMO_CODE,
        promotion={"type": "coupon", "coupon": FOUNDER_COUPON_ID},
    )


def stripe_is_configured() -> bool:
    key = settings.stripe_secret_key.strip()
    return key.startswith(("sk_test_", "sk_live_")) and "your_stripe" not in key


def _require_stripe() -> None:
    if not stripe_is_configured():
        raise HTTPException(
            status_code=503,
            detail="STRIPE_SECRET_KEY is not configured. Add test keys to the repo-root .env.",
        )
    stripe.api_key = settings.stripe_secret_key


def _require_tokens() -> None:
    if not tokens_available():
        raise HTTPException(
            status_code=503,
            detail="ACCESS_TOKEN_SECRET is not configured for production.",
        )


def _lookup(customer_id: str, *, fresh: bool = False) -> AccessState:
    try:
        return subscriptions.access_state(customer_id, fresh=fresh)
    except SubscriptionLookupError:
        raise HTTPException(
            status_code=503,
            detail="Could not confirm the subscription with Stripe. Try again shortly.",
        ) from None


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _object_id(value: Any) -> str | None:
    return value if isinstance(value, str) else _get(value, "id")


def _session_active(info: TokenInfo) -> bool:
    try:
        return sessions.session_is_active(info)
    except StoreUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Could not confirm the session right now. Try again shortly.",
        ) from None


def current_session(authorization: str | None) -> TokenInfo:
    """A current, unrevoked token that names a customer, or 401 (503 if the store is down)."""
    token = bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="LexShield Professional subscription required.")
    # Only a current token is accepted here; renewal-stage tokens work at /billing/refresh only.
    if not token_is_valid(token):
        raise HTTPException(status_code=401, detail="Session expired. Renew it at /billing/refresh.")
    info = parse_token(token)
    if info is None or not info.customer_id:
        raise HTTPException(status_code=401, detail="LexShield Professional subscription required.")
    if not _session_active(info):
        raise HTTPException(status_code=401, detail="This session was signed out.")
    return info


def renewable_session(authorization: str | None) -> TokenInfo | None:
    """A renewal-stage (or newer), unrevoked token inside its session limit, else None."""
    token = bearer_token(authorization)
    if not token or not token_is_refreshable(token):
        return None
    info = parse_token(token)
    if info is None or not _session_active(info):
        return None
    return info


def state_for_authorization(authorization: str | None) -> AccessState:
    """Stripe access state for the customer named by a current bearer token."""
    info = current_session(authorization)
    _require_stripe()
    return _lookup(info.customer_id)


@router.get("/status")
def billing_status() -> dict[str, object]:
    return {
        "product": "LexShield Professional",
        "amount_usd": settings.subscription_amount_cents // 100,
        "founder_code": FOUNDER_PROMO_CODE,
        "interval": "month",
        "checkout_required": not settings.billing_bypass_enabled(),
        "stripe_configured": stripe_is_configured(),
        # Tells the frontend that /billing/me and /billing/refresh exist, so a 404 there is a
        # deployment fault rather than an older backend.
        "access_model": ACCESS_MODEL,
    }


@router.get("/me")
def billing_me(authorization: str | None = Header(default=None)) -> dict[str, object]:
    return state_for_authorization(authorization).as_dict()


_customer_locks: dict[str, threading.Lock] = {}
_customer_locks_guard = threading.Lock()


def _customer_lock(customer_id: str) -> threading.Lock:
    with _customer_locks_guard:
        return _customer_locks.setdefault(customer_id, threading.Lock())


def _open_checkout_url(customer_id: str) -> str | None:
    try:
        listed = stripe.checkout.Session.list(customer=customer_id, status="open", limit=10)
    except Exception:  # noqa: BLE001
        return None
    for session in _get(listed, "data") or []:
        metadata = _get(session, "metadata") or {}
        url = _get(session, "url")
        if _get(metadata, "product") == PRODUCT_KEY and isinstance(url, str):
            return url
    return None


def _idempotency_key(customer_id: str) -> str:
    bucket = int(time.time() // CHECKOUT_IDEMPOTENCY_WINDOW_SECONDS)
    return hashlib.sha256(f"lexshield-checkout:{customer_id}:{bucket}".encode()).hexdigest()


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout_session(authorization: str | None = Header(default=None)) -> CheckoutResponse:
    """Start a subscription without ever selling a second one to a known customer.

    The customer comes only from a token this backend signed (current or renewal stage), never
    from ids or emails sent by the browser. A known customer with a subscription to use or fix
    gets 409 and is sent to the portal; an open LexShield Checkout session is reused; otherwise
    the session is created for that same Stripe customer under an idempotency key, and a
    per-process lock serializes simultaneous requests. Without a token the visitor is anonymous
    and Stripe collects the email; looking that email up here would reveal who subscribes.
    """
    _require_stripe()
    signed_in = renewable_session(authorization)
    customer_id = signed_in.customer_id if signed_in else None

    lock = _customer_lock(customer_id) if customer_id else threading.Lock()
    with lock:
        if customer_id:
            if _lookup(customer_id, fresh=True).has_open_subscription:
                raise HTTPException(
                    status_code=409,
                    detail="This account already has a subscription. Manage it in the customer portal.",
                )
            open_url = _open_checkout_url(customer_id)
            if open_url:
                return CheckoutResponse(checkout_url=open_url)
            identity_params: dict[str, str] = {
                "customer": customer_id,
                "idempotency_key": _idempotency_key(customer_id),
            }
        else:
            identity_params = {}
        try:
            ensure_founder_promotion()
            session = stripe.checkout.Session.create(
                mode="subscription",
                success_url=(
                    f"{app_base()}/success?session_id={{CHECKOUT_SESSION_ID}}"
                ),
                cancel_url=f"{app_base()}/#pricing",
                billing_address_collection="required",
                allow_promotion_codes=True,
                line_items=[
                    {
                        "quantity": 1,
                        "price_data": {
                            "currency": "usd",
                            "unit_amount": settings.subscription_amount_cents,  # 10000 = $100 USD / month
                            "recurring": {"interval": "month"},
                            "product_data": {
                                "name": "LexShield Professional",
                                "description": (
                                    "Unlimited contract risk audits. Processed in memory and purged "
                                    "after the audit. Text is sent to an external AI provider "
                                    "(OpenAI) for analysis."
                                ),
                                "metadata": {"product": PRODUCT_KEY},
                            },
                        },
                    }
                ],
                metadata={"product": PRODUCT_KEY},
                subscription_data={"metadata": {"product": PRODUCT_KEY}},
                **identity_params,
            )
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail="Unable to start Stripe Checkout.") from exc

    if not session.url:
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL.")
    return CheckoutResponse(checkout_url=session.url)


@router.post("/verify", response_model=AccessResponse)
def verify_checkout(body: VerifyRequest) -> AccessResponse:
    """Exchange a fresh, completed LexShield Checkout session for a token.

    The token only identifies the Stripe customer. Access is read from the current subscription.
    """
    _require_tokens()
    if body.bypass:
        if not settings.billing_bypass_enabled():
            raise HTTPException(
                status_code=403,
                detail="Billing bypass is disabled. Complete Stripe Checkout.",
            )
        return AccessResponse(access_token=issue_access_token())

    if not body.session_id:
        raise HTTPException(status_code=400, detail="Missing Stripe session_id.")
    _require_stripe()
    try:
        session = stripe.checkout.Session.retrieve(body.session_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Unknown Stripe Checkout session.") from exc

    metadata = _get(session, "metadata") or {}
    if _get(session, "mode") != "subscription" or _get(metadata, "product") != PRODUCT_KEY:
        raise HTTPException(status_code=403, detail="This Checkout session is not a LexShield subscription.")
    created = _get(session, "created")
    if not isinstance(created, int) or time.time() - created > VERIFY_SESSION_MAX_AGE_SECONDS:
        raise HTTPException(
            status_code=410,
            detail="This Checkout link can no longer sign you in. Recover access instead.",
        )
    if _get(session, "status") != "complete":
        raise HTTPException(status_code=402, detail="Subscription payment is not complete.")

    customer_id = _object_id(_get(session, "customer"))
    subscription_id = _object_id(_get(session, "subscription"))
    if not customer_id or not subscription_id:
        raise HTTPException(status_code=400, detail="Checkout session has no Stripe subscription.")
    try:
        subscription = stripe.Subscription.retrieve(subscription_id)
        matches = _object_id(_get(subscription, "customer")) == customer_id and (
            subscriptions.subscription_is_authorized(subscription)
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="Could not confirm the subscription with Stripe. Try again shortly.",
        ) from exc
    if not matches:
        raise HTTPException(status_code=403, detail="This Checkout session is not a LexShield subscription.")

    state = _lookup(customer_id, fresh=True)
    return AccessResponse(access_token=issue_access_token(customer_id), subscription=state.as_dict())


@router.post("/refresh", response_model=AccessResponse)
def refresh_access(authorization: str | None = Header(default=None)) -> AccessResponse:
    """Renew the token without Checkout or a charge.

    Authenticated by a token this backend signed for a Stripe customer, current or in its
    renewal stage (see app/auth.py). Always re-reads Stripe, and renews only while the customer
    has a LexShield subscription to use or fix (active, trialing, past_due, unpaid, incomplete,
    paused). The new token still carries no access: each call re-checks the subscription.
    """
    _require_tokens()
    info = renewable_session(authorization)
    if info is None:
        raise HTTPException(
            status_code=401,
            detail="This session can no longer be renewed. Recover access by email.",
        )
    _require_stripe()
    state = _lookup(info.customer_id, fresh=True)
    if not state.has_open_subscription:
        raise HTTPException(status_code=403, detail="No LexShield subscription to renew for this customer.")
    # Same session and same start: renewal never extends the session limit.
    token = issue_access_token(info.customer_id, auth_at=info.auth_at, session_id=info.session_id)
    return AccessResponse(access_token=token, subscription=state.as_dict())


@router.post("/logout")
def logout(authorization: str | None = Header(default=None)) -> dict[str, bool]:
    """Revoke this session everywhere. Accepts renewal-stage tokens so old devices can sign out."""
    info = renewable_session(authorization)
    if info is not None:
        try:
            sessions.revoke_session(info)
        except StoreUnavailable:
            raise HTTPException(status_code=503, detail="Could not sign out right now. Try again.") from None
    return {"signed_out": True}


@router.post("/sessions/revoke-all")
def revoke_all_sessions(authorization: str | None = Header(default=None)) -> dict[str, bool]:
    """Sign this customer out on every device, including this one."""
    info = current_session(authorization)
    try:
        sessions.revoke_all(info.customer_id)
    except StoreUnavailable:
        raise HTTPException(status_code=503, detail="Could not sign out right now. Try again.") from None
    return {"signed_out": True}


class PortalResponse(BaseModel):
    portal_url: str


def ensure_portal_configuration() -> str:
    listed = stripe.billing_portal.Configuration.list(active=True, limit=20)
    for config in listed.data:
        if (config.metadata or {}).get("product") == "lexshield-professional":
            return config.id
    created = stripe.billing_portal.Configuration.create(
        metadata={"product": "lexshield-professional"},
        business_profile={"headline": "LexShield Professional"},
        features={
            "invoice_history": {"enabled": True},
            "payment_method_update": {"enabled": True},
            "subscription_cancel": {
                "enabled": True,
                "mode": "at_period_end",
                "proration_behavior": "none",
            },
        },
    )
    return created.id


@router.post("/portal", response_model=PortalResponse)
def create_portal_session(authorization: str | None = Header(default=None)) -> PortalResponse:
    _require_stripe()
    customer_id = current_session(authorization).customer_id

    try:
        configuration = ensure_portal_configuration()
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{app_base()}/auditor",
            configuration=configuration,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail="Unable to open the Stripe customer portal.",
        ) from exc
    if not portal.url:
        raise HTTPException(status_code=502, detail="Stripe did not return a portal URL.")
    return PortalResponse(portal_url=portal.url)
