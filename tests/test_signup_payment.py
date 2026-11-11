"""Paid sign-up: nothing the visitor typed is written until Stripe confirms payment."""

import pytest

import app
from test_app import (
    FakeStripe,
    FakeStripeCheckoutSession,
    FakeStripeWebhook,
    configure_tmp_paths,
    install_fake_stripe,
    stub_utility_lookup,
    token_from_latest_email,
)

FORM = {
    "full_name": "Home Owner",
    "email": "owner@example.com",
    "password": "customer-password-123",
    "account_number": "duke-123",
    "address": "123 Main St Charlotte NC",
    "zip_code": "28205",
    "plan_id": "home",
    "heating_system": "Heat pump",
    "accept_policies": "yes",
    "confirm_account_authority": "yes",
}
USER_TABLES = (
    "customer_users", "accounts", "household_profiles", "account_access_emails", "customer_billing",
    "audit_events", "account_data_authorizations", "customer_policy_acceptances", "customer_auth_tokens",
)


BASELINE = {}


class FakeStripeSubscription:
    calls = []

    @classmethod
    def modify(cls, subscription_id, **kwargs):
        cls.calls.append((subscription_id, kwargs))
        return {"id": subscription_id}


@pytest.fixture
def paid(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch, company="Duke Energy Carolinas, LLC")
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    FakeStripeSubscription.calls = []
    monkeypatch.setattr(FakeStripe, "Subscription", FakeStripeSubscription, raising=False)
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    global BASELINE
    BASELINE = row_counts()  # The app seeds a placeholder "primary" account.
    return app.web_app.test_client()


def row_counts():
    with app.get_db_connection() as conn:
        return {table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] for table in USER_TABLES}


def start_signup(client, **changes):
    return client.post("/signup", data={**FORM, **changes}, follow_redirects=False)


def checkout_reference():
    return FakeStripeCheckoutSession.calls[0]["client_reference_id"]


def paid_session(reference, **changes):
    return {
        "id": "cs_test_123",
        "client_reference_id": reference,
        "customer": "cus_123",
        "subscription": {"id": "sub_123", "status": "active", "current_period_end": 1784505600},
        "payment_intent": {"id": "pi_123", "charges": {"data": [{"receipt_url": "https://pay.stripe.com/receipts/test"}]}},
        "payment_status": "paid",
        "amount_total": 23988,
        "currency": "usd",
        "metadata": {"plan_id": "home"},
        **changes,
    }


def test_signup_sends_visitor_to_stripe_without_writing_anything(paid):
    before = row_counts()
    response = start_signup(paid)
    call = FakeStripeCheckoutSession.calls[0]

    assert response.status_code == 302
    assert response.headers["Location"] == "https://checkout.stripe.com/c/cs_test_123"
    assert row_counts() == before == BASELINE
    assert call["customer_email"] == "owner@example.com"
    assert call["client_reference_id"].startswith("signup:")
    assert call["success_url"] == "https://app.homeenergywatch.com/signup/complete?session_id={CHECKOUT_SESSION_ID}"
    assert call["cancel_url"] == "https://app.homeenergywatch.com/signup?resume=1"
    # Stripe gets the email to charge and an opaque reference, never the rest of the form.
    flat = repr(call["metadata"]) + repr(call["subscription_data"])
    for private in ("duke-123", "123 Main St", "28205", "Home Owner", "Heat pump", "owner@example.com"):
        assert private not in flat
    # The form is held in the session, encrypted.
    cookie = paid.get_cookie("home_energy_watch_session").value
    for private in ("duke-123", "Main St", "owner@example.com"):
        assert private not in cookie


def test_returning_after_payment_creates_the_account(paid):
    start_signup(paid)
    FakeStripeCheckoutSession.retrieve_result = paid_session(checkout_reference())
    response = paid.get("/signup/complete?session_id=cs_test_123", follow_redirects=False)

    customer = app.get_customer_user_by_email("owner@example.com")
    account = app.find_account("duke-123")
    billing = app.load_customer_billing(int(customer["id"]))
    assert response.status_code == 302 and response.headers["Location"].endswith("/customer")
    assert account["energy_company"] == "Duke Energy Carolinas, LLC"
    assert app.load_household_profile("duke-123")["heating_system"] == "Heat pump"
    assert billing["status"] == "active"
    assert billing["stripe_subscription_id"] == "sub_123"
    assert billing["checkout_session_id"] == "cs_test_123"
    assert FakeStripeSubscription.calls[0][0] == "sub_123"
    assert FakeStripeSubscription.calls[0][1]["metadata"]["customer_user_id"] == str(customer["id"])
    assert FakeStripeSubscription.calls[0][1]["metadata"]["account_id"] == str(account["id"])
    with app.get_db_connection() as conn:
        actions = [row["action"] for row in conn.execute("SELECT action FROM audit_events ORDER BY id")]
    assert {"customer.signup", "customer.policy_accepted", "utility.authorization_granted", "billing.signup_paid"} <= set(actions)
    # Only one checkout was ever started.
    assert len([c for c in FakeStripeCheckoutSession.calls if "mode" in c]) == 1
    # The pending form is gone from the session; a replay does nothing.
    replay = paid.get("/signup/complete?session_id=cs_test_123", follow_redirects=False)
    assert replay.headers["Location"].endswith("/customer")
    assert row_counts()["accounts"] == BASELINE["accounts"] + 1


def test_email_confirmation_after_payment_does_not_charge_again(paid, monkeypatch):
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    app.EMAIL_OUTBOX.clear()
    start_signup(paid)
    FakeStripeCheckoutSession.retrieve_result = paid_session(checkout_reference())
    response = paid.get("/signup/complete?session_id=cs_test_123")
    assert b"Check your email" in response.data
    token = token_from_latest_email()
    confirmation = paid.post("/customer/verify-email", data={"token": token}, follow_redirects=False)

    assert confirmation.headers["Location"].endswith("/customer")
    assert len([c for c in FakeStripeCheckoutSession.calls if "mode" in c]) == 1


@pytest.mark.parametrize("change", [
    {"payment_status": "unpaid"},
    {"client_reference_id": "signup:someone-else"},
])
def test_unpaid_or_foreign_checkout_writes_nothing(paid, change):
    start_signup(paid)
    session = paid_session(checkout_reference())
    session.update(change)
    FakeStripeCheckoutSession.retrieve_result = session
    response = paid.get("/signup/complete?session_id=cs_test_123", follow_redirects=False)
    assert "/signup?resume=1" in response.headers["Location"]
    assert row_counts() == BASELINE


def test_session_id_must_match_this_browsers_checkout(paid):
    start_signup(paid)
    FakeStripeCheckoutSession.retrieve_result = paid_session(checkout_reference(), id="cs_other")
    paid.get("/signup/complete?session_id=cs_other")
    assert row_counts()["customer_users"] == 0


def test_other_browser_cannot_complete_and_nothing_is_written(paid):
    start_signup(paid)
    FakeStripeCheckoutSession.retrieve_result = paid_session(checkout_reference())
    other = app.web_app.test_client()
    response = other.get("/signup/complete?session_id=cs_test_123", follow_redirects=True)
    assert b"no longer has your sign-up details" in response.data
    assert row_counts()["customer_users"] == 0


def test_cancel_returns_to_a_prefilled_form(paid):
    start_signup(paid)
    page = paid.get("/signup?resume=1")
    assert b"duke-123" in page.data and b"owner@example.com" in page.data
    assert row_counts()["customer_users"] == 0


def test_paid_visitor_who_never_returned_finishes_from_signup_page(paid):
    start_signup(paid)
    FakeStripeCheckoutSession.retrieve_result = paid_session(checkout_reference())
    response = paid.get("/signup", follow_redirects=False)
    assert response.headers["Location"].endswith("/signup/complete?session_id=cs_test_123")


def test_webhook_for_unfinished_signup_writes_no_user_data(paid, monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_home_energy_watch")
    start_signup(paid)
    FakeStripeWebhook.event = {
        "id": "evt_signup",
        "type": "checkout.session.completed",
        "data": {"object": paid_session(checkout_reference(), subscription="sub_123")},
    }
    response = app.web_app.test_client().post(
        "/stripe/webhook", data=b'{"id":"evt_signup"}', headers={"Stripe-Signature": "t=1,v1=x"}
    )
    assert response.status_code == 200
    counts = row_counts()
    assert {k: v for k, v in counts.items() if k != "audit_events"} == {k: v for k, v in BASELINE.items() if k != "audit_events"}
    with app.get_db_connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT action, account_id, actor_id FROM audit_events")]
    # Only the event id is logged, with no account or customer attached.
    assert rows == [{"action": "billing.webhook_processed", "account_id": None, "actor_id": None}]


def test_duplicate_account_is_refused_before_payment(paid):
    app.create_customer_user("someone@example.com", "Someone")
    with app.get_db_connection() as conn:
        app.get_or_create_account(conn, "duke-123")
        conn.commit()
    response = start_signup(paid)
    assert response.status_code == 200
    assert b"already registered" in response.data
    assert FakeStripeCheckoutSession.calls == []


def test_stripe_failure_leaves_nothing_behind(paid, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("stripe down")
    monkeypatch.setattr(FakeStripeCheckoutSession, "create", staticmethod(fail))
    response = start_signup(paid)
    assert response.status_code == 200
    with paid.session_transaction() as session:
        assert app.PENDING_SIGNUP_SESSION_KEY not in session
    assert row_counts()["customer_users"] == 0


def test_free_signup_without_payment_is_unchanged(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    monkeypatch.setenv("POWER_BILLING_ENABLED", "false")
    client = app.web_app.test_client()
    client.post("/signup", data=FORM)
    assert app.get_customer_user_by_email("owner@example.com") is not None
