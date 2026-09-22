"""Regression coverage for passwordless signup and existing billing databases."""
import sqlite3

import app
import pytest
from test_app import configure_tmp_paths, stub_utility_lookup, customer_sign_in


def signup_form(email="owner@example.com", account="second-account"):
    return dict(email=email, full_name="Account owner", account_number=account,
                address="123 Main St Charlotte NC", zip_code="28205",
                accept_policies="yes", confirm_account_authority="yes")


def test_existing_verified_email_cannot_be_used_to_sign_in_via_signup(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    owner = app.create_customer_user("owner@example.com", "Owner")
    client = app.web_app.test_client()
    response = client.post("/signup", data=signup_form())
    assert b"Sign in with your email link" in response.data
    assert app.find_account("second-account") is None
    with client.session_transaction() as session:
        assert "customer_user_id" not in session
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT count(*) FROM customer_policy_acceptances WHERE customer_user_id = ?", (owner["id"],)).fetchone()[0] == 0


def test_authenticated_owner_can_add_account_but_cannot_use_another_identity(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Owner")
    app.create_customer_user("other@example.com", "Other")
    client = app.web_app.test_client()
    customer_sign_in(client)
    assert client.post("/signup", data=signup_form()).status_code == 302
    assert app.find_account("second-account") is not None
    response = client.post("/signup", data=signup_form("other@example.com", "third-account"))
    assert b"Sign in with your email link" in response.data
    assert app.find_account("third-account") is None


def test_passwordless_inserts_work_with_legacy_not_null_column(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    with app.get_db_connection(ensure_schema=False) as conn:
        conn.execute("CREATE TABLE customer_users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, full_name TEXT NOT NULL, password_hash TEXT NOT NULL, is_active INTEGER NOT NULL DEFAULT 1, email_verified_at TEXT, auth_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_login_at TEXT)")
        conn.commit()
    app.ensure_database()
    app.create_customer_user("first@example.com", "First")
    with app.get_db_connection() as conn:
        account = app.get_or_create_account(conn, "shared")
        conn.execute("INSERT INTO account_access_emails (account_id,email,full_name,access_level,created_at,updated_at) VALUES (?,?,'Shared','Manager','now','now')", (account["id"], "shared@example.com"))
        conn.commit()
    assert app.ensure_customer_user_for_email("shared@example.com") is not None
    stub_utility_lookup(monkeypatch)
    app.web_app.config["TESTING"] = True
    assert app.web_app.test_client().post("/signup", data=signup_form()).status_code == 302
    with app.get_db_connection() as conn:
        assert [r[0] for r in conn.execute("SELECT password_hash FROM customer_users").fetchall()] == ["", "", ""]


def legacy_database():
    raw = sqlite3.connect(":memory:")
    raw.row_factory = sqlite3.Row
    conn = app.DatabaseConnection(raw, kind="sqlite", target_label="migration-test")
    conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE customer_users (id INTEGER PRIMARY KEY, email TEXT)")
    conn.execute("CREATE TABLE account_access_emails (account_id INTEGER, email TEXT)")
    conn.execute("CREATE TABLE customer_billing (customer_user_id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL, subscription_status TEXT NOT NULL, stripe_subscription_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO accounts VALUES (1), (2)")
    conn.execute("INSERT INTO customer_users VALUES (1,'one@example.com'), (2,'two@example.com')")
    conn.execute("INSERT INTO customer_billing VALUES (1,'home','active','sub_one','then','now'), (2,'home','active','sub_two','then','now')")
    return conn


def test_billing_migration_preserves_every_row_and_original_table():
    with legacy_database() as conn:
        conn.execute("INSERT INTO account_access_emails VALUES (1,'one@example.com'), (2,'two@example.com')")
        app.migrate_legacy_customer_billing(conn, 1)
        rows = conn.execute("SELECT account_id,stripe_subscription_id FROM customer_billing ORDER BY account_id").fetchall()
        assert [tuple(row) for row in rows] == [(1, "sub_one"), (2, "sub_two")]
        assert conn.execute("SELECT count(*) FROM customer_billing_legacy").fetchone()[0] == 2
        assert conn.execute("SELECT stripe_receipt_url FROM customer_billing LIMIT 1").fetchone()[0] is None


@pytest.mark.parametrize("access", [
    [(1, "one@example.com"), (1, "two@example.com")],
    [(1, "one@example.com")],
    [(1, "one@example.com"), (2, "one@example.com"), (2, "two@example.com")],
])
def test_ambiguous_billing_migration_stops_without_modifying_records(access):
    with legacy_database() as conn:
        for row in access:
            conn.execute("INSERT INTO account_access_emails VALUES (?, ?)", row)
        with pytest.raises(RuntimeError):
            app.migrate_legacy_customer_billing(conn, 1)
        assert conn.execute("SELECT count(*) FROM customer_billing").fetchone()[0] == 2
        assert not app.table_exists(conn, "customer_billing_legacy")
        assert not app.table_exists(conn, "customer_billing_account_migration")
