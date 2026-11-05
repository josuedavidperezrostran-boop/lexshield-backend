"""Which flows can work with the current configuration, so none is offered while broken.

Email flows (account recovery and email-verified checkout) need a shared store and a mail
backend that production accepts (app/store.py and app/mail.py refuse memory stores and the
development mailers there), Stripe for this environment, and a private token secret. Checkout
also needs the price for the active Stripe mode. /billing/status reports these flags and the
endpoints answer 503 whenever they are false.
"""

from __future__ import annotations

from .auth import tokens_available
from .config import settings
from .mail import MailUnavailable, get_mailer
from .store import StoreUnavailable, get_store


def store_available() -> bool:
    try:
        get_store()
    except StoreUnavailable:
        return False
    return True


def mail_available() -> bool:
    try:
        get_mailer()
    except MailUnavailable:
        return False
    return True


def email_flows_available() -> bool:
    return (
        settings.active_stripe_mode() is not None
        and tokens_available()
        and store_available()
        and mail_available()
    )


def email_checkout_available() -> bool:
    return email_flows_available() and settings.stripe_price_id() is not None
