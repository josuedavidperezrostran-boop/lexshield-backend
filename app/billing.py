from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

import stripe
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from . import availability, sessions, subscriptions
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
from .store import StoreUnavailable, get_store
from .subscriptions import PRODUCT_KEY, AccessState, SubscriptionLookupError

router = APIRouter(prefix="/billing", tags=["billing"])

# A Checkout session_id signs a customer in once, right after paying. It is not a lasting login.
VERIFY_SESSION_MAX_AGE_SECONDS = 24 * 60 * 60
# Checkout coordination for a known customer (shared store, see create_checkout_session).
CHECKOUT_LOCK_SECONDS = 30
CHECKOUT_LOCK_WAIT_SECONDS = 5
# Lifetime of a Checkout session we create; Stripe accepts 30 minutes to 24 hours.
CHECKOUT_SESSION_SECONDS = 60 * 60


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
# Created by hand in each Stripe mode (80% off the first invoice only); Checkout accepts it.
FOUNDER_PROMO_CODE = "FOUNDER80"
DEFAULT_APP = "https://frontend-1-lex-shield2.vercel.app"


def app_base() -> str:
    """Public site origin. FRONTEND_ORIGIN overrides this when a custom domain is attached."""
    configured = settings.frontend_origin.strip().rstrip("/")
    return configured or DEFAULT_APP


def stripe_is_configured() -> bool:
    return settings.active_stripe_mode() is not None


def _require_stripe() -> None:
    if not stripe_is_configured():
        raise HTTPException(
            status_code=503,
            detail="Stripe is not configured for this environment (key, STRIPE_MODE and ENVIRONMENT must agree).",
        )
    stripe.api_key = settings.stripe_secret_key


def _require_price() -> str:
    price_id = settings.stripe_price_id()
    if not price_id:
        raise HTTPException(
            status_code=503,
            detail="No LexShield price is configured for this Stripe mode (STRIPE_PRICE_ID_TEST / _LIVE).",
        )
    return price_id


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
        # Real availability, so the frontend never offers a flow that would fail or pretend.
        "recovery_available": availability.email_flows_available(),
        "new_checkout_available": availability.email_checkout_available(),
    }


@router.get("/me")
def billing_me(authorization: str | None = Header(default=None)) -> dict[str, object]:
    return state_for_authorization(authorization).as_dict()


def acquire_lock(key: str) -> str:
    """Shared-store lock, so every process and instance takes turns. Returns the owner token.

    Waits up to CHECKOUT_LOCK_WAIT_SECONDS for another request to finish (it usually leaves a
    pending session this one then reuses). A holder that dies frees the lock after
    CHECKOUT_LOCK_SECONDS; a holder that is merely slow can outlive it, which is why the
    idempotency key, not the lock, is what prevents a second session. Raises StoreUnavailable
    when the store cannot be used.
    """
    owner = secrets.token_hex(16)
    deadline = time.monotonic() + CHECKOUT_LOCK_WAIT_SECONDS
    while True:
        if get_store().add(key, owner, CHECKOUT_LOCK_SECONDS):
            return owner
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=503,
                detail="Another checkout for this account is in progress. Try again in a moment.",
            )
        time.sleep(0.1)


def release_lock(key: str, owner: str) -> None:
    try:
        get_store().delete_if(key, owner)
    except StoreUnavailable:
        pass  # the lock expires on its own after CHECKOUT_LOCK_SECONDS


def _pending_checkout_url(customer_id: str) -> str | None:
    """The session this account was last sent to, while Stripe still has it open."""
    store = get_store()
    session_id = store.get(f"checkout:session:{customer_id}")
    if not session_id:
        return None
    try:
        session = stripe.checkout.Session.retrieve(session_id)
    except Exception:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail="Could not confirm the pending checkout with Stripe. Try again shortly.",
        ) from None
    if _get(session, "status") == "open" and isinstance(_get(session, "url"), str):
        return _get(session, "url")
    # Expired or finished: the next session is a new attempt.
    store.delete(f"checkout:session:{customer_id}")
    store.delete(f"checkout:attempt:{customer_id}")
    return None


def _checkout_attempt(customer_id: str) -> tuple[str, int]:
    """Attempt id and expiry shared by every retry of one checkout.

    The idempotency key derives from the attempt, not from the clock, so a retry after a crash
    (Stripe created the session, the store never heard of it) gets the same session back. The
    expiry is stored with it because Stripe rejects a reused key with different parameters.
    """
    store = get_store()
    key = f"checkout:attempt:{customer_id}"
    existing = store.get(key)
    if existing and int(existing.split(":", 1)[1]) <= time.time():
        # That attempt's session has expired; replaying its key would return the dead session.
        store.delete_if(key, existing)
    fresh = f"{secrets.token_hex(16)}:{int(time.time()) + CHECKOUT_SESSION_SECONDS}"
    store.add(key, fresh, CHECKOUT_SESSION_SECONDS + 300)
    attempt_id, expires_at = (store.get(key) or fresh).split(":", 1)
    return attempt_id, int(expires_at)


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


def _idempotency_key(customer_id: str, attempt_id: str) -> str:
    return hashlib.sha256(f"lexshield-checkout:{customer_id}:{attempt_id}".encode()).hexdigest()


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout_session(authorization: str | None = Header(default=None)) -> CheckoutResponse:
    """Checkout for a customer identified by a token this backend signed.

    The customer comes only from that token (current or renewal stage), never from ids or emails
    sent by the browser. Visitors without a token get 401 and must verify their email first
    (POST /billing/checkout/start, app/signup.py), so every Checkout belongs to a known customer.
    """
    _require_stripe()
    _require_price()
    signed_in = renewable_session(authorization)
    if signed_in is None:
        raise HTTPException(
            status_code=401,
            detail="Verify your email before checkout: POST /billing/checkout/start.",
        )
    return CheckoutResponse(checkout_url=checkout_for_customer(signed_in.customer_id))


def checkout_for_customer(customer_id: str) -> str:
    """Checkout URL for a known customer, never a second subscription.

    Under a shared-store lock: 409 when a subscription to use or fix exists; otherwise the
    pending session (ours, or any open LexShield session in Stripe) is returned; otherwise one
    is created under an idempotency key tied to the checkout attempt (see _checkout_attempt).
    """
    _require_stripe()
    _require_price()
    lock_key = f"checkout:lock:{customer_id}"
    try:
        owner = acquire_lock(lock_key)
    except StoreUnavailable:
        raise HTTPException(status_code=503, detail="Checkout is unavailable right now. Try again shortly.") from None
    try:
        if _lookup(customer_id, fresh=True).has_open_subscription:
            raise HTTPException(
                status_code=409,
                detail="This account already has a subscription. Manage it in the customer portal.",
            )
        url = _pending_checkout_url(customer_id) or _open_checkout_url(customer_id)
        if url:
            return url
        attempt_id, expires_at = _checkout_attempt(customer_id)
        response = _create_session(
            {
                "customer": customer_id,
                "expires_at": expires_at,
                "idempotency_key": _idempotency_key(customer_id, attempt_id),
            }
        )
        get_store().set(f"checkout:session:{customer_id}", response.session_id, CHECKOUT_SESSION_SECONDS + 300)
        return response.checkout_url
    except StoreUnavailable:
        raise HTTPException(status_code=503, detail="Checkout is unavailable right now. Try again shortly.") from None
    finally:
        release_lock(lock_key, owner)


class _CreatedSession(CheckoutResponse):
    session_id: str


def _create_session(identity_params: dict[str, object]) -> _CreatedSession:
    # The price (and its product, name and description) is configured in Stripe beforehand;
    # Checkout never creates products, prices or coupons.
    price_id = _require_price()
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            success_url=(
                f"{app_base()}/success?session_id={{CHECKOUT_SESSION_ID}}"
            ),
            cancel_url=f"{app_base()}/#pricing",
            billing_address_collection="required",
            allow_promotion_codes=True,
            line_items=[{"price": price_id, "quantity": 1}],
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
    return _CreatedSession(checkout_url=session.url, session_id=_get(session, "id") or "")


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


@router.post("/portal", response_model=PortalResponse)
def create_portal_session(authorization: str | None = Header(default=None)) -> PortalResponse:
    """Uses the portal configuration named for this Stripe mode, or the account default."""
    _require_stripe()
    customer_id = current_session(authorization).customer_id
    configuration = settings.stripe_portal_configuration_id()

    try:
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{app_base()}/auditor",
            **({"configuration": configuration} if configuration else {}),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail="Unable to open the Stripe customer portal.",
        ) from exc
    if not portal.url:
        raise HTTPException(status_code=502, detail="Stripe did not return a portal URL.")
    return PortalResponse(portal_url=portal.url)
