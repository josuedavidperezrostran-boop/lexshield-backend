"""Stripe is the source of truth for subscription access.

Access rules (first version):
- active, trialing: access. A cancellation scheduled for the period end keeps access until
  Stripe moves the subscription to canceled.
- past_due: no new analyses; the customer is sent to the portal to update the payment method.
- canceled, unpaid, incomplete, incomplete_expired, paused: no access.

Only subscriptions for the LexShield Professional product count.

Cache: each process keeps its own in-memory map customer -> state for at most
SUBSCRIPTION_CACHE_MAX_SECONDS (10 minutes). Several processes or instances each cache
separately, so a change in Stripe reaches every process within that bound; a restart empties
the cache and the next request asks Stripe. An expired entry is never reused: the next request
asks Stripe again. If Stripe errors or times out, SubscriptionLookupError is raised. Callers
answer 503 (temporary, retry) and must neither grant access nor treat it as a cancellation, so a
positive authorization never outlives the cache bound.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

import stripe

from .config import settings

PRODUCT_KEY = "lexshield-professional"
# Checkout created its product inline before product metadata existed; those products carry
# only this name. Products are created in our own Stripe account, so the name is ours.
LEGACY_PRODUCT_NAME = "LexShield Professional"

ACCESS_STATUSES = frozenset({"active", "trialing"})
PAYMENT_REQUIRED_STATUSES = frozenset({"past_due"})
BLOCKED_STATUSES = frozenset({"canceled", "unpaid", "incomplete", "incomplete_expired", "paused"})
# A customer with one of these must resolve it; never sell them a second subscription.
OPEN_STATUSES = frozenset({"active", "trialing", "past_due", "unpaid", "incomplete", "paused"})

# Higher wins when a customer has several subscriptions.
_PRIORITY = {
    "active": 6,
    "trialing": 6,
    "past_due": 5,
    "unpaid": 4,
    "incomplete": 3,
    "paused": 3,
    "canceled": 1,
    "incomplete_expired": 1,
}

SUBSCRIPTION_CACHE_MAX_SECONDS = 600


class SubscriptionLookupError(Exception):
    """Stripe could not be read. Never treat this as access."""


@dataclass(frozen=True)
class AccessState:
    customer_id: str
    status: Optional[str]
    subscription_id: Optional[str]
    cancel_at_period_end: bool
    current_period_end: Optional[int]

    @property
    def access(self) -> bool:
        return self.status in ACCESS_STATUSES

    @property
    def payment_required(self) -> bool:
        return self.status in PAYMENT_REQUIRED_STATUSES

    @property
    def has_open_subscription(self) -> bool:
        return self.status in OPEN_STATUSES

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "access": self.access,
            "payment_required": self.payment_required,
            "has_subscription": self.has_open_subscription,
            "cancel_at_period_end": self.cancel_at_period_end,
            "current_period_end": self.current_period_end,
        }


_cache: dict[str, tuple[float, AccessState]] = {}
_product_cache: dict[str, bool] = {}
_lock = threading.Lock()


def cache_seconds() -> int:
    return max(0, min(settings.subscription_cache_seconds, SUBSCRIPTION_CACHE_MAX_SECONDS))


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _product_cache.clear()


def _ids(raw: str) -> set[str]:
    return {part.strip() for part in raw.split(",") if part.strip()}


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _product_is_authorized(product_id: str) -> bool:
    if product_id in _ids(settings.stripe_authorized_product_ids):
        return True
    with _lock:
        cached = _product_cache.get(product_id)
    if cached is not None:
        return cached
    product = stripe.Product.retrieve(product_id)
    metadata = _get(product, "metadata") or {}
    authorized = (
        _get(metadata, "product") == PRODUCT_KEY or _get(product, "name") == LEGACY_PRODUCT_NAME
    )
    with _lock:
        _product_cache[product_id] = authorized
    return authorized


def subscription_is_authorized(subscription: Any) -> bool:
    metadata = _get(subscription, "metadata") or {}
    if _get(metadata, "product") == PRODUCT_KEY:
        return True
    items = _get(_get(subscription, "items"), "data") or []
    price_ids = _ids(settings.stripe_authorized_price_ids)
    for item in items:
        price = _get(item, "price") or {}
        if _get(price, "id") in price_ids:
            return True
        product = _get(price, "product")
        product_id = product if isinstance(product, str) else _get(product, "id")
        if isinstance(product_id, str) and _product_is_authorized(product_id):
            return True
    return False


def _period_end(subscription: Any) -> Optional[int]:
    value = _get(subscription, "current_period_end")
    if isinstance(value, int):
        return value
    # Newer Stripe API versions keep the period on each subscription item.
    for item in _get(_get(subscription, "items"), "data") or []:
        value = _get(item, "current_period_end")
        if isinstance(value, int):
            return value
    return None


def _subscription_customer(subscription: Any) -> Optional[str]:
    customer = _get(subscription, "customer")
    return customer if isinstance(customer, str) else _get(customer, "id")


def state_from_subscriptions(customer_id: str, subscriptions: list[Any]) -> AccessState:
    best: Any = None
    for subscription in subscriptions:
        if _subscription_customer(subscription) != customer_id:
            continue
        if _get(subscription, "status") not in _PRIORITY:
            continue
        if not subscription_is_authorized(subscription):
            continue
        if best is None or _PRIORITY[_get(subscription, "status")] > _PRIORITY[_get(best, "status")]:
            best = subscription
    if best is None:
        return AccessState(customer_id, None, None, False, None)
    return AccessState(
        customer_id=customer_id,
        status=_get(best, "status"),
        subscription_id=_get(best, "id"),
        cancel_at_period_end=bool(_get(best, "cancel_at_period_end")),
        current_period_end=_period_end(best),
    )


def _fetch(customer_id: str) -> AccessState:
    stripe.api_key = settings.stripe_secret_key
    try:
        listed = stripe.Subscription.list(customer=customer_id, status="all", limit=20)
        return state_from_subscriptions(customer_id, list(_get(listed, "data") or []))
    except Exception as exc:  # noqa: BLE001
        raise SubscriptionLookupError(type(exc).__name__) from exc


def access_state(customer_id: str, *, fresh: bool = False) -> AccessState:
    """Current access for a Stripe customer, from cache when younger than cache_seconds()."""
    ttl = cache_seconds()
    now = time.monotonic()
    if not fresh and ttl:
        with _lock:
            hit = _cache.get(customer_id)
        if hit and now - hit[0] < ttl:
            return hit[1]
    state = _fetch(customer_id)
    with _lock:
        _cache[customer_id] = (time.monotonic(), state)
    return state
