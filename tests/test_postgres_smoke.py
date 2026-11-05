"""Postgres smoke test.

Skipped unless POWER_TEST_DATABASE_URL points at a disposable Postgres
database, for example:

    POWER_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/hew_smoke

The test never drops anything. It runs the app's migrations twice to prove
they are idempotent and writes rows with unique identifiers, so it can be
re-run against the same database. Run it with POWER_ENV=test semantics: the
production sslmode requirement does not apply to a local CI service.
"""

import base64
import os
import uuid

import pytest


TEST_DATABASE_URL = (os.getenv("POWER_TEST_DATABASE_URL") or "").strip()

# CI sets POWER_TEST_REQUIRE_DATABASE=1 so a missing URL fails instead of
# silently skipping.
if not TEST_DATABASE_URL and (os.getenv("POWER_TEST_REQUIRE_DATABASE") or "").strip() == "1":
    raise RuntimeError("POWER_TEST_REQUIRE_DATABASE=1 but POWER_TEST_DATABASE_URL is not set.")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Set POWER_TEST_DATABASE_URL to a disposable Postgres database to run the Postgres smoke test.",
)


@pytest.fixture
def pg_app(tmp_path, monkeypatch):
    import app

    monkeypatch.setattr(app, "INPUT_DIR", tmp_path / "input")
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(app, "DB_PATH", tmp_path / "output" / "unused.db")
    monkeypatch.setattr(app, "DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("POWER_DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POWER_ENV", "test")
    monkeypatch.setenv("POWER_APP_SECRET", "home-energy-watch-postgres-smoke-secret-0001")
    monkeypatch.setenv("POWER_AUDIT_SIGNING_KEY", "home-energy-watch-postgres-smoke-audit-0001")
    monkeypatch.setenv(
        "POWER_DATA_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"home-energy-watch-test-key-0001!").decode("ascii"),
    )
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "disabled")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    monkeypatch.setenv("POWER_MARKETING_BASE_URL", "https://homeenergywatch.com")
    monkeypatch.setenv("POWER_STAFF_MFA_REQUIRED", "false")
    monkeypatch.delenv("POWER_DATA_DELETION_ENABLED", raising=False)
    monkeypatch.delenv("POWER_BILLING_ENABLED", raising=False)
    app.SCHEMA_READY_TARGETS.clear()
    app.ensure_data_dirs()
    yield app
    app.SCHEMA_READY_TARGETS.clear()


def test_database_settings_select_postgres(pg_app):
    status = pg_app.build_database_status()

    assert status["database_backend"] == "postgres"
    assert "@" not in status["database_target"].split("//", 1)[-1].split("@")[-1]


def test_migrations_are_idempotent(pg_app):
    for _ in range(2):
        with pg_app.get_db_connection(ensure_schema=False) as conn:
            assert conn.kind == "postgres"
            pg_app.migrate_database(conn)
            conn.commit()

    # The app's own startup path (advisory lock + migrate) must also be a no-op now.
    for _ in range(2):
        pg_app.SCHEMA_READY_TARGETS.clear()
        pg_app.ensure_database()

    with pg_app.get_db_connection(ensure_schema=False) as conn:
        for table in ("accounts", "customer_users", "account_access_emails", "account_notes"):
            assert pg_app.table_exists(conn, table), table
        assert {"account_id", "note_date", "body"} <= pg_app.table_columns(conn, "account_notes")


def test_customer_account_and_note_round_trip(pg_app):
    suffix = uuid.uuid4().hex[:12]
    email = f"pg-smoke-{suffix}@example.com"
    account_number = f"PGSMOKE{suffix}".upper()

    customer = pg_app.create_customer_user(email, "Postgres Smoke")
    assert customer["email"] == email
    assert pg_app.get_customer_user_by_email(email)["id"] == customer["id"]

    account = pg_app.save_account_profile(
        account_number,
        display_name="Smoke Test Home",
        energy_company="Duke Energy Progress, LLC",
    )
    assert account["account_number"] == account_number

    access = pg_app.add_account_access_email(account_number, email, "Postgres Smoke", "Manager")
    assert access["access_level"] == "Manager"
    assert pg_app.get_customer_account_access(email, account_number)["access_level"] == "Manager"

    note = pg_app.add_customer_account_note(account_number, customer, "2026-01-02", "Space heater on overnight.")
    assert note["id"] > 0
    assert note["body"] == "Space heater on overnight."

    notes = pg_app.list_account_notes(account_number, actor={"kind": "customer", "user": customer})
    assert [item["id"] for item in notes] == [note["id"]]
    assert notes[0]["note_date"] == "2026-01-02"
    assert notes[0]["author_kind"] == "customer"
    assert notes[0]["can_delete"] is True

    pg_app.delete_customer_account_note(account_number, note["id"], customer)
    assert pg_app.list_account_notes(account_number) == []


def test_health_endpoint_against_postgres(pg_app):
    pg_app.web_app.config["TESTING"] = True
    client = pg_app.web_app.test_client()

    response = client.get("/health", headers={"Host": "app.homeenergywatch.com"})

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
