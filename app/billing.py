from __future__ import annotations

import stripe
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .auth import customer_id_from_token, issue_access_token, token_is_valid
from .config import settings

router = APIRouter(prefix="/billing", tags=["billing"])


class CheckoutResponse(BaseModel):
    checkout_url: str


class VerifyRequest(BaseModel):
    session_id: str | None = None
    bypass: bool = False


class AccessResponse(BaseModel):
    access_token: str
    plan: str = "professional"
    amount_usd: int = 1000


FOUNDER_COUPON_ID = "founder80_first_month"
FOUNDER_PROMO_CODE = "FOUNDER80"
LIVE_APP = "https://frontend-1-lex-shield2.vercel.app"


def ensure_founder_promotion() -> None:
    """80% off the first invoice only. Month two bills the full $1,000."""
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


@router.get("/status")
def billing_status() -> dict[str, object]:
    return {
        "product": "LexShield Professional",
        "amount_usd": settings.subscription_amount_cents // 100,
        "founder_code": FOUNDER_PROMO_CODE,
        "interval": "month",
        "checkout_required": not settings.dev_bypass_billing,
        "stripe_configured": stripe_is_configured(),
    }


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout_session() -> CheckoutResponse:
    if not stripe_is_configured():
        raise HTTPException(
            status_code=503,
            detail="STRIPE_SECRET_KEY is not configured. Add test keys to the repo-root .env.",
        )

    stripe.api_key = settings.stripe_secret_key
    try:
        ensure_founder_promotion()
        session = stripe.checkout.Session.create(
            mode="subscription",
            success_url=(
                f"{LIVE_APP}/success?session_id={{CHECKOUT_SESSION_ID}}"
            ),
            cancel_url=f"{LIVE_APP}/#pricing",
            billing_address_collection="required",
            allow_promotion_codes=True,
            line_items=[
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": settings.subscription_amount_cents,
                        "recurring": {"interval": "month"},
                        "product_data": {
                            "name": "LexShield Professional",
                            "description": (
                                "Unlimited contract risk audits under Zero Data Retention. "
                                "Processed in memory and purged after the audit."
                            ),
                        },
                    },
                }
            ],
            metadata={"product": "lexshield-professional"},
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="Unable to start Stripe Checkout.") from exc

    if not session.url:
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL.")
    return CheckoutResponse(checkout_url=session.url)


@router.post("/verify", response_model=AccessResponse)
def verify_checkout(body: VerifyRequest) -> AccessResponse:
    if body.bypass:
        if not settings.dev_bypass_billing:
            raise HTTPException(
                status_code=403,
                detail="Billing bypass is disabled. Complete Stripe Checkout.",
            )
        return AccessResponse(access_token=issue_access_token())

    if not body.session_id:
        raise HTTPException(status_code=400, detail="Missing Stripe session_id.")
    if not stripe_is_configured():
        raise HTTPException(
            status_code=503,
            detail="STRIPE_SECRET_KEY is not configured. Add test keys to the repo-root .env.",
        )

    stripe.api_key = settings.stripe_secret_key
    try:
        session = stripe.checkout.Session.retrieve(body.session_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Unknown Stripe Checkout session.") from exc

    paid = session.payment_status in {"paid", "no_payment_required"}
    complete = session.status == "complete"
    if not (paid or complete):
        raise HTTPException(status_code=402, detail="Subscription payment is not complete.")

    customer = session.customer
    customer_id = customer if isinstance(customer, str) else getattr(customer, "id", None)
    if not customer_id:
        raise HTTPException(status_code=400, detail="Checkout session has no Stripe customer.")
    return AccessResponse(access_token=issue_access_token(customer_id))


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
    if not stripe_is_configured():
        raise HTTPException(
            status_code=503,
            detail="STRIPE_SECRET_KEY is not configured. Add test keys to the repo-root .env.",
        )
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token_is_valid(token):
        raise HTTPException(
            status_code=401,
            detail="LexShield Professional subscription required.",
        )
    customer_id = customer_id_from_token(token)
    if not customer_id:
        raise HTTPException(
            status_code=409,
            detail="Finish Stripe Checkout once so this session can open the customer portal.",
        )

    stripe.api_key = settings.stripe_secret_key
    try:
        configuration = ensure_portal_configuration()
        portal = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{LIVE_APP}/auditor",
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
