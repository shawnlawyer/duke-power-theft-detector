"""The sign-in form never writes anything for what was typed into it."""

import pytest

import app
from test_access_control import COMMISSION_EMAIL
from test_app import bootstrap_staff, configure_tmp_paths, token_from_latest_email


@pytest.fixture
def site(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    app.EMAIL_OUTBOX.clear()
    bootstrap_staff()
    return app.web_app.test_client()


def snapshot():
    with app.get_db_connection() as conn:
        tables = [r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                for t in tables if t not in ("auth_rate_limits", "sqlite_sequence")}


def test_unknown_email_saves_nothing_and_sends_nothing(site):
    before = snapshot()
    response = site.post("/login", data={"email": "stranger@example.com"})
    assert response.status_code == 200 and b"Check" in response.data
    assert snapshot() == before
    assert app.EMAIL_OUTBOX == []


def test_invited_customer_gets_link_but_nothing_is_saved_until_it_is_used(site):
    with app.get_db_connection() as conn:
        account = app.get_or_create_account(conn, "acct-1")
        conn.execute("INSERT INTO account_access_emails (account_id, email, access_level, created_at, updated_at) "
                     "VALUES (?, 'invited@example.com', 'Manager', 'now', 'now')", (account["id"],))
        conn.commit()
    before = snapshot()
    site.post("/login", data={"email": "invited@example.com"})
    assert snapshot() == before
    token = token_from_latest_email()
    assert token.startswith("first_")
    assert site.get("/login/token", query_string={"token": token}).status_code == 200
    assert app.get_customer_user_by_email("invited@example.com") is None

    finished = site.post("/login/token", data={"token": token})
    assert finished.headers["Location"].endswith("/customer")
    customer = app.get_customer_user_by_email("invited@example.com")
    assert customer is not None and customer["email_verified"]
    # The link works once.
    again = app.web_app.test_client().post("/login/token", data={"token": token})
    assert again.headers["Location"].endswith("/login")


def test_commission_email_is_provisioned_only_when_the_link_is_used(site):
    before = snapshot()
    site.post("/login", data={"email": COMMISSION_EMAIL})
    assert snapshot() == before
    token = token_from_latest_email()
    site.post("/login/token", data={"token": token})
    staff = app.get_staff_user_by_email(COMMISSION_EMAIL)
    assert staff is not None and app.get_staff_commission_access(int(staff["id"])) is not None


def test_tampered_or_foreign_first_link_is_refused(site):
    forged = app.create_first_sign_in_token("stranger@example.com", "customer")
    assert app.read_first_sign_in_token(forged) is None  # No access, no link.
    assert app.read_first_sign_in_token("first_" + "A" * 80) is None
    response = site.post("/login/token", data={"token": forged})
    assert response.headers["Location"].endswith("/login")
    assert app.get_customer_user_by_email("stranger@example.com") is None


def test_password_reset_for_unknown_email_saves_nothing(site):
    before = snapshot()
    site.post("/forgot-password", data={"email": "stranger@example.com"})
    assert snapshot() == before
    assert app.EMAIL_OUTBOX == []
