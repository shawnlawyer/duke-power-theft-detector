"""Regression coverage for data-handling hardening (webhooks, deletion, errors, secrets, parsing)."""
import base64
import csv
import json
import logging
import sys
import zipfile
from io import BytesIO, StringIO
from types import SimpleNamespace

import app
import pandas as pd
import pytest
from cryptography.fernet import Fernet
from lxml import etree

from test_app import (
    FIXTURE,
    FakeStripeWebhook,
    authorize_account,
    comparison_csv,
    configure_tmp_paths,
    customer_sign_in,
    install_fake_passkeys,
    install_fake_stripe,
    sign_in,
    stub_utility_lookup,
)

CAVEAT = "Flagged intervals are prompts for review, not proof of theft, tampering, or a billing error."


def flashed_messages(client):
    with client.session_transaction() as browser_session:
        return [message for _category, message in browser_session.get("_flashes", [])]


# 1. Stripe webhook idempotency and ordering


def post_webhook(client):
    return client.post("/stripe/webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=test"})


def test_stripe_webhook_skips_already_processed_event_ids(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_home_energy_watch")
    handled = []
    monkeypatch.setattr(app, "handle_stripe_event", lambda event: handled.append(event["id"]))
    FakeStripeWebhook.event = {"id": "evt_dup_1", "type": "checkout.session.completed", "created": 100, "data": {"object": {}}}
    client = app.web_app.test_client()

    first = post_webhook(client)
    second = post_webhook(client)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.get_json()["status"] == "duplicate"
    assert handled == ["evt_dup_1"]
    with app.get_db_connection() as conn:
        row = conn.execute("SELECT event_type, created FROM processed_stripe_events WHERE event_id = ?", ("evt_dup_1",)).fetchone()
    assert row["event_type"] == "checkout.session.completed"
    assert int(row["created"]) == 100


def test_stripe_subscription_events_apply_in_created_order(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.ensure_database()
    handled = []
    monkeypatch.setattr(app, "handle_stripe_event", lambda event: handled.append(event["id"]))

    def subscription_event(event_id, created, event_type="customer.subscription.updated"):
        return {"id": event_id, "type": event_type, "created": created, "data": {"object": {"id": "sub_order"}}}

    assert app.process_stripe_event(subscription_event("evt_new", 200)) == "processed"
    assert app.process_stripe_event(subscription_event("evt_old", 100, "customer.subscription.deleted")) == "stale"
    assert app.process_stripe_event(subscription_event("evt_old", 100, "customer.subscription.deleted")) == "duplicate"
    assert app.process_stripe_event(subscription_event("evt_newer", 300)) == "processed"
    # A different subscription has its own watermark.
    other = {"id": "evt_other", "type": "customer.subscription.updated", "created": 50, "data": {"object": {"id": "sub_other"}}}
    assert app.process_stripe_event(other) == "processed"
    assert handled == ["evt_new", "evt_newer", "evt_other"]


def test_stripe_webhook_errors_do_not_echo_exception_text(tmp_path, monkeypatch, caplog):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_home_energy_watch")

    def explode(cls, payload, signature, secret):
        raise RuntimeError("internal-detail whsec_test_home_energy_watch")

    monkeypatch.setattr(FakeStripeWebhook, "construct_event", classmethod(explode))
    with caplog.at_level(logging.ERROR, logger=app.APP_LOGGER_NAME):
        response = post_webhook(app.web_app.test_client())

    assert response.status_code == 400
    assert b"internal-detail" not in response.data
    assert b"whsec" not in response.data
    assert any("billing.webhook_failed" in record.getMessage() and "RuntimeError" in record.getMessage() for record in caplog.records)


# 2. Account data deletion and compare upload cleanup


def test_data_deletion_removes_assistant_notes_equipment_and_commission_rows(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    monkeypatch.setenv("POWER_DATA_DELETION_ENABLED", "true")
    monkeypatch.setenv("POWER_DATA_DELETION_POLICY_VERSION", "retention-2026-07")
    commissioner = app.create_first_staff_user("commission@example.gov", "Commissioner One", "test-password-123")
    customer = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.save_account_profile("acct-wipe", display_name="Wipe this home")
    app.add_account_access_email("acct-wipe", "owner@example.com", access_level="Manager")
    app.grant_account_data_authorization("acct-wipe", int(customer["id"]))
    account_id = int(app.find_account("acct-wipe")["id"])
    now = app.timestamp_now()
    with app.get_db_connection() as conn:
        conn.execute(
            "INSERT INTO household_assistant_interactions (account_id, customer_user_id, question, answer, created_at) VALUES (?, ?, ?, ?, ?)",
            (account_id, int(customer["id"]), "private question", "private answer", now),
        )
        conn.execute(
            "INSERT INTO account_notes (account_id, note_date, body, author_kind, author_label, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, "2026-01-01", "private note", "customer", "Owner", now, now),
        )
        conn.execute(
            "INSERT INTO account_equipment_history (account_id, category, label, watts_each, active_from, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, "water_heater", "Tank", 4500, "2020-01-01", now, now),
        )
        commission_id = conn.execute("SELECT id FROM commission_organizations ORDER BY id LIMIT 1").fetchone()["id"]
        columns = app.table_columns(conn, "account_commission_access")
        values = {"account_id": account_id, "commission_id": commission_id}
        for column in columns - set(values):
            values[column] = now
        conn.execute(
            f"INSERT INTO account_commission_access ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            tuple(values.values()),
        )
        conn.commit()
    app.import_interval_file_to_db(FIXTURE, account_number="acct-wipe")
    data_request = app.create_account_deletion_request("acct-wipe", int(customer["id"]))
    app.review_account_deletion_request(int(data_request["id"]), int(commissioner["id"]), "approve", "Verified.")

    result = app.execute_account_data_deletion(int(data_request["id"]), int(commissioner["id"]))

    tables = ("household_assistant_interactions", "account_notes", "account_equipment_history", "account_commission_access")
    with app.get_db_connection() as conn:
        for table in tables:
            assert conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE account_id = ?", (account_id,)).fetchone()["count"] == 0
    for table in tables:
        assert result["deleted_counts"][table] == 1
    assert result["deleted_counts"]["interval_reading_sources"] > 0
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM interval_reading_sources").fetchone()["n"] == 0


def test_compare_upload_files_are_removed_after_report(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    authorize_account("acct-cmp", email="owner@example.com")
    client = app.web_app.test_client()
    customer_sign_in(client)

    response = client.post(
        "/compare",
        data={
            "account_number": "acct-cmp",
            "left_file": (BytesIO(comparison_csv([("2024-01-01T02:00:00-05:00", "2024-01-01T03:00:00-05:00", 0.5)])), "earlier.csv"),
            "right_file": (BytesIO(comparison_csv([("2024-01-01T02:00:00-05:00", "2024-01-01T03:00:00-05:00", 0.8)])), "later.csv"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"Comparison packet" in response.data
    assert CAVEAT.encode() in response.data
    assert list(app.INPUT_DIR.glob("earlier*")) == []
    assert list(app.INPUT_DIR.glob("later*")) == []


# 3. Error message leakage


def test_unexpected_errors_flash_generic_message_but_validation_errors_stay(tmp_path, monkeypatch, caplog):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    authorize_account("acct-err", email="owner@example.com")
    client = app.web_app.test_client()
    customer_sign_in(client)

    def internal_failure(*args, **kwargs):
        raise RuntimeError("connection to db-internal.example:5432 failed")

    monkeypatch.setattr(app, "add_customer_account_note", internal_failure)
    with caplog.at_level(logging.ERROR, logger=app.APP_LOGGER_NAME):
        client.post("/account/notes", data={"account_number": "acct-err", "note_date": "2026-01-01", "body": "x"})
    messages = flashed_messages(client)
    assert app.GENERIC_ERROR_MESSAGE in messages
    assert not any("db-internal" in message for message in messages)
    assert any("RuntimeError" in record.getMessage() for record in caplog.records)

    def validation_failure(*args, **kwargs):
        raise ValueError("Choose a valid note date.")

    monkeypatch.setattr(app, "add_customer_account_note", validation_failure)
    client.post("/account/notes", data={"account_number": "acct-err", "note_date": "bad", "body": "x"})
    assert "Choose a valid note date." in flashed_messages(client)


# 4. Swallowed audit failures are logged


def test_billing_success_logs_refresh_failures_without_changing_flow(tmp_path, monkeypatch, caplog):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    client = app.web_app.test_client()
    customer_sign_in(client)

    def refresh_failure(*args, **kwargs):
        raise RuntimeError("stripe unavailable")

    monkeypatch.setattr(app, "refresh_customer_billing_from_stripe", refresh_failure)
    with caplog.at_level(logging.ERROR, logger=app.APP_LOGGER_NAME):
        response = client.get("/billing/success", query_string={"session_id": "cs_x"})

    assert response.status_code == 302
    assert "Billing is being confirmed." in flashed_messages(client)
    assert any("billing.status_refresh_failed" in record.getMessage() for record in caplog.records)


# 5. Encryption key rotation and resealing


def fernet_key(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed.ljust(32, b"!")[:32]).decode("ascii")


def test_encryption_key_rotation_and_reseal_cli(tmp_path, monkeypatch, capsys, caplog):
    configure_tmp_paths(tmp_path, monkeypatch)
    old_key, new_key = fernet_key(b"old-key"), fernet_key(b"new-key")
    monkeypatch.setenv("POWER_DATA_ENCRYPTION_KEY", old_key)
    app.save_account_profile("acct-keys", display_name="Keys")
    app.save_utility_connection(
        "acct-keys",
        {
            "provider_name": "Duke Energy",
            "connection_label": "Main",
            "access_method": "customer_api_key",
            "access_identifier": "owner@example.com",
            "access_secret": "rotate-me-secret",
        },
    )
    legacy_plain = b"legacy-secret-value"
    stream = app.build_legacy_secret_key_stream(len(legacy_plain))
    legacy_token = base64.urlsafe_b64encode(bytes(b ^ stream[i] for i, b in enumerate(legacy_plain))).decode("ascii")
    with app.get_db_connection() as conn:
        account_id = int(app.find_account("acct-keys")["id"])
        conn.execute(
            "INSERT INTO utility_connections (account_id, provider_name, connection_label, access_method, access_identifier, secret_token, status, created_at, updated_at) VALUES (?, 'Duke Energy', 'Legacy', 'customer_api_key', 'legacy@example.com', ?, 'active', 'now', 'now')",
            (account_id, legacy_token),
        )
        conn.commit()

    # Rotation: new key first, old key still decrypts.
    monkeypatch.setenv("POWER_DATA_ENCRYPTION_KEY", f"{new_key}, {old_key}")
    with app.get_db_connection() as conn:
        tokens = [row["secret_token"] for row in conn.execute("SELECT secret_token FROM utility_connections ORDER BY id").fetchall()]
    assert app.unseal_secret_value(tokens[0]) == "rotate-me-secret"
    with caplog.at_level(logging.WARNING, logger=app.APP_LOGGER_NAME):
        assert app.unseal_secret_value(tokens[1]) == "legacy-secret-value"
    assert any("secrets.legacy_unseal" in record.getMessage() for record in caplog.records)
    fresh = app.seal_secret_value("fresh")
    assert Fernet(new_key.encode()).decrypt(fresh.removeprefix("fernet:v1:").encode()) == b"fresh"

    monkeypatch.setattr(sys, "argv", ["app.py", "--reseal-secrets"])
    app.main()
    assert "Resealed secrets: 2" in capsys.readouterr().out

    with app.get_db_connection() as conn:
        resealed = [row["secret_token"] for row in conn.execute("SELECT secret_token FROM utility_connections ORDER BY id").fetchall()]
    assert all(token.startswith("fernet:v1:") for token in resealed)
    new_only = Fernet(new_key.encode())
    assert new_only.decrypt(resealed[0].removeprefix("fernet:v1:").encode()) == b"rotate-me-secret"
    assert new_only.decrypt(resealed[1].removeprefix("fernet:v1:").encode()) == b"legacy-secret-value"
    monkeypatch.setenv("POWER_DATA_ENCRYPTION_KEY", new_key)
    assert app.unseal_secret_value(resealed[0]) == "rotate-me-secret"


# 6. Household assistant limits


def test_household_assistant_caps_question_length_and_rate(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    monkeypatch.setattr(app, "invoke_bedrock_household_assistant", lambda context: None)
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    authorize_account("acct-ask", email="owner@example.com")
    client = app.web_app.test_client()
    customer_sign_in(client)

    too_long = client.post("/api/household-assistant", json={"account_number": "acct-ask", "question": "x" * 2001})
    assert too_long.status_code == 400
    assert "2000 characters" in too_long.get_json()["error"]

    statuses = [
        client.post("/api/household-assistant", json={"account_number": "acct-ask", "question": "What is my use?"}).status_code
        for _ in range(app.HOUSEHOLD_ASSISTANT_RATE_LIMIT + 1)
    ]
    assert statuses[:-1] == [200] * app.HOUSEHOLD_ASSISTANT_RATE_LIMIT
    assert statuses[-1] == 429


# 7. CSV injection


def test_csv_values_neutralize_tab_and_carriage_return_and_export_uses_helper(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    for prefix in ("=", "+", "-", "@", "\t", "\r"):
        assert app.audit_csv_value(f"{prefix}cmd") == f"'{prefix}cmd"
    assert app.audit_csv_value("plain") == "plain"

    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    authorize_account("acct-csv", email="owner@example.com")
    hostile = app.INPUT_DIR / "=HYPERLINK(1).xml"
    hostile.write_bytes(FIXTURE.read_bytes())
    app.import_interval_file_to_db(hostile, account_number="acct-csv")
    client = app.web_app.test_client()
    customer_sign_in(client)

    response = client.get("/customer/data-export.zip")
    with zipfile.ZipFile(BytesIO(response.data)) as archive:
        name = next(name for name in archive.namelist() if name.endswith("/interval-readings.csv"))
        rows = list(csv.DictReader(StringIO(archive.read(name).decode("utf-8"))))
        source_name = next(name for name in archive.namelist() if name.endswith("/interval-sources.csv"))
        source_rows = list(csv.DictReader(StringIO(archive.read(source_name).decode("utf-8"))))
    assert source_rows
    assert all(row["source_file"] == "'=HYPERLINK(1).xml" for row in source_rows)
    assert rows
    assert all(row["source_file"] == "'=HYPERLINK(1).xml" for row in rows)


# 8. XML external entities


def test_interval_xml_parser_does_not_resolve_external_entities(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT", encoding="utf-8")
    payload = tmp_path / "xxe.xml"
    payload.write_text(
        f'<?xml version="1.0"?>\n<!DOCTYPE feed [<!ENTITY xxe SYSTEM "file://{secret}">]>\n<feed><title>&xxe;</title></feed>',
        encoding="utf-8",
    )

    tree = app.parse_interval_xml_tree(payload)

    assert b"TOP-SECRET-CONTENT" not in etree.tostring(tree)


# 9. Passkey sign counter regression


def test_passkey_login_rejects_non_advancing_sign_count(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    staff_user = app.create_first_staff_user("commission@example.gov", "Commissioner One", "test-password-123")
    install_fake_passkeys(monkeypatch)
    client = app.web_app.test_client()
    sign_in(client)
    client.post("/staff/security/passkeys/start", data={"nickname": "Laptop"})
    assert client.post("/staff/security/passkeys/finish", json={"id": "ignored"}).status_code == 200
    client.post("/logout")

    def set_counter(value):
        monkeypatch.setattr(
            app.AuthenticationResponse,
            "from_dict",
            staticmethod(lambda payload: SimpleNamespace(response=SimpleNamespace(authenticator_data=SimpleNamespace(counter=value, is_user_verified=lambda: True)))),
        )

    def attempt_login():
        client.post("/login/passkey/start", data={"email": "commission@example.gov", "next": "/staff"})
        response = client.post("/login/passkey/finish", json={"id": "ignored"})
        with client.session_transaction() as browser_session:
            signed_in = browser_session.get("staff_user_id")
        client.post("/logout")
        return response, signed_in

    set_counter(5)  # stored count is 7: a cloned authenticator would replay a lower count
    rejected, signed_in = attempt_login()
    assert rejected.status_code == 400
    assert signed_in is None

    set_counter(0)  # 0 means the authenticator does not support counters
    allowed, signed_in = attempt_login()
    assert allowed.status_code == 200
    assert signed_in == int(staff_user["id"])

    set_counter(8)
    advanced, signed_in = attempt_login()
    assert advanced.status_code == 200
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT sign_count FROM passkey_credentials").fetchone()["sign_count"] == 8


# 10. Disclaimers


def test_cli_reports_print_caveat_and_utility_heading(tmp_path, monkeypatch, capsys):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.print_human_report(pd.DataFrame({"suspicious": pd.Series([], dtype=bool)}), None)
    assert CAVEAT in capsys.readouterr().out

    comparison, report_path = app.analyze_interval_file_comparison(FIXTURE, FIXTURE)
    app.print_comparison_report(comparison, report_path)
    output = capsys.readouterr().out
    assert "=== UTILITY EXPORT COMPARISON ===" in output
    assert "DUKE EXPORT COMPARISON" not in output
    assert CAVEAT in output


def test_analysis_surface_always_shows_caveat(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    authorize_account("acct-cav", email="owner@example.com")
    app.save_account_profile("acct-cav", display_name="Main house", energy_company="Duke Energy Progress, LLC")
    app.save_household_profile("acct-cav", {"address": "1 Main Street", "zip_code": "27601"})
    stub_utility_lookup(monkeypatch)
    client = app.web_app.test_client()
    customer_sign_in(client)

    response = client.post(
        "/analyze",
        data={"account_number": "acct-cav", "xml_file": (BytesIO(FIXTURE.read_bytes()), "history.xml")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert CAVEAT.encode() in response.data


@pytest.mark.parametrize("sources", [
    ("national-grid://connection/17", "/private/upload.xml"),
    ("/private/upload.xml", "national-grid://connection/17"),
])
def test_interval_sources_preserve_both_orders_and_conflicting_observations(tmp_path, monkeypatch, sources):
    configure_tmp_paths(tmp_path, monkeypatch)
    frame = pd.DataFrame([{"start_epoch": 1704067200, "duration_s": 900, "wh": 250.0}])
    first = app.import_interval_frame_to_db(frame, sources[0], "acct-sources")
    second = app.import_interval_frame_to_db(frame, sources[1], "acct-sources")
    repeat = app.import_interval_frame_to_db(frame, sources[1], "acct-sources")
    conflict = frame.assign(wh=300.0)
    rejected = app.import_interval_frame_to_db(conflict, sources[1], "acct-sources")
    app.import_interval_frame_to_db(conflict, sources[1], "acct-sources")
    assert first["added_count"] == 1
    assert second["already_present_count"] == repeat["already_present_count"] == 1
    assert rejected["conflicts_skipped_count"] == 1
    with app.get_db_connection() as conn:
        retained = conn.execute("SELECT wh, source_path FROM interval_readings").fetchone()
        rows = conn.execute("SELECT * FROM interval_reading_sources").fetchall()
    assert retained["wh"] == 250.0
    assert retained["source_path"] == sources[0]
    assert len(rows) == 3
    assert {row["source_path"] for row in rows if row["matches_retained_value"]} == set(sources)
    assert [(row["source_path"], row["asserted_wh"]) for row in rows if not row["matches_retained_value"]] == [(sources[1], 300.0)]
    assert all(row["record_kind"] == "observed_import" for row in rows)


def test_interval_sources_account_isolation_and_repeat_timestamps(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    frame = pd.DataFrame([{"start_epoch": 1704067200, "duration_s": 900, "wh": 250.0}])
    monkeypatch.setattr(app, "timestamp_now", lambda: "2026-11-23T00:00:00+00:00")
    app.import_interval_frame_to_db(frame, "same.xml", "acct-one")
    monkeypatch.setattr(app, "timestamp_now", lambda: "2026-11-24T00:00:00+00:00")
    app.import_interval_frame_to_db(frame, "same.xml", "acct-one")
    app.import_interval_frame_to_db(frame.assign(wh=400.0), "same.xml", "acct-two")
    with app.get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM interval_reading_sources ORDER BY account_id").fetchall()
    assert len(rows) == 2
    assert rows[0]["account_id"] != rows[1]["account_id"]
    assert rows[0]["first_observed_at"].startswith("2026-11-23")
    assert rows[0]["last_observed_at"].startswith("2026-11-24")
    assert rows[1]["asserted_wh"] == 400.0
    assert all(row["matches_retained_value"] == 1 for row in rows)


@pytest.mark.parametrize("invalid_wh", [float("nan"), float("inf"), -float("inf")])
def test_interval_sources_and_readings_rollback_together(tmp_path, monkeypatch, invalid_wh):
    configure_tmp_paths(tmp_path, monkeypatch)
    frame = pd.DataFrame([
        {"start_epoch": 1704067200, "duration_s": 900, "wh": 250.0},
        {"start_epoch": 1704068100, "duration_s": 900, "wh": invalid_wh},
    ])
    with pytest.raises(ValueError, match="finite"):
        app.import_interval_frame_to_db(frame, "failed.xml", "acct-rollback")
    with app.get_db_connection() as conn:
        for table in ("interval_readings", "interval_reading_sources", "imported_files"):
            assert conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0


def test_interval_sources_backfill_only_known_canonical_source_once(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    frame = pd.DataFrame([{"start_epoch": 1704067200, "duration_s": 900, "wh": 250.0}])
    app.import_interval_frame_to_db(frame, "known.xml", "acct-legacy")
    app.import_interval_frame_to_db(frame, "historical-duplicate.xml", "acct-legacy")
    with app.get_db_connection() as conn:
        conn.execute("DROP TABLE interval_reading_sources")
    app.SCHEMA_READY_TARGETS.clear()
    app.ensure_database()
    with app.get_db_connection() as conn:
        rows = conn.execute("SELECT * FROM interval_reading_sources").fetchall()
        assert len(rows) == 1
        assert rows[0]["source_path"] == "known.xml"
        assert rows[0]["record_kind"] == "retained_before_tracking"
        assert rows[0]["adapter_id"] == ""
        conn.execute("DELETE FROM interval_reading_sources")
    app.SCHEMA_READY_TARGETS.clear()
    app.ensure_database()
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM interval_reading_sources").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM interval_readings").fetchone()["n"] == 1


@pytest.mark.parametrize("failure", ["revoked", "replaced", "wrong_account"])
def test_interval_sources_respect_connection_guard(tmp_path, monkeypatch, failure):
    configure_tmp_paths(tmp_path, monkeypatch)
    authorize_account("acct-guard", email="owner@example.com")
    app.save_utility_connection("acct-guard", {
        "provider_name": "Duke Energy Carolinas, LLC", "connection_label": "Main account",
        "access_method": "customer_api_key", "access_identifier": "test-account",
        "access_secret": "test-approved-secret",
    })
    connection = app.list_utility_connections("acct-guard")[0]
    guarded = app.load_utility_connection_for_sync("acct-guard", connection["id"])
    frame = pd.DataFrame([{"start_epoch": 1704067200, "duration_s": 900, "wh": 250.0}])
    app.import_interval_frame_to_db(frame, "guarded://test", "acct-guard",
                                   connection_guard=(connection["id"], guarded["access_secret"]))
    with app.get_db_connection() as conn:
        row = conn.execute("SELECT * FROM interval_reading_sources").fetchone()
        assert row["guarded_connection_id"] == connection["id"]
        if failure == "revoked":
            conn.execute("UPDATE account_data_authorizations SET status = 'revoked' WHERE account_id = ?", (app.find_account("acct-guard")["id"],))
        elif failure == "replaced":
            conn.execute("UPDATE utility_connections SET secret_token = ? WHERE id = ?",
                         (app.seal_secret_value("replacement"), connection["id"]))
    with pytest.raises(ValueError):
        app.import_interval_frame_to_db(frame, "refused://test", "wrong-account" if failure == "wrong_account" else "acct-guard",
                                       connection_guard=(connection["id"], guarded["access_secret"]))
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM interval_reading_sources").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM imported_files").fetchone()["n"] == 1


def test_interval_sources_keep_shared_managed_files(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    frame = pd.DataFrame([{"start_epoch": 1704067200, "duration_s": 900, "wh": 250.0}])
    shared = app.INPUT_DIR / "shared.xml"
    shared.write_bytes(FIXTURE.read_bytes())
    app.import_interval_frame_to_db(frame, str(shared), "acct-one")
    app.import_interval_frame_to_db(frame, "original.xml", "acct-two")
    app.import_interval_frame_to_db(frame, str(shared), "acct-two")
    with app.get_db_connection() as conn:
        second = app.find_account("acct-two")["id"]
        conn.execute("DELETE FROM imported_files WHERE account_id = ? AND path = ?", (second, str(shared)))
        assert shared not in app.account_managed_files(conn, app.find_account("acct-one")["id"])
