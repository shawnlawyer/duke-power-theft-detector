"""Regression tests for the access-control and request-safety fixes from the security audit."""

import socket

import pytest

import app
from types import SimpleNamespace

from tests.test_app import (
    FIXTURE,
    install_fake_passkeys,
    authorize_account,
    bootstrap_staff,
    configure_tmp_paths,
    customer_sign_in,
    email_token_sign_in,
    install_fake_stripe,
    sign_in,
    stub_utility_lookup,
    token_from_latest_email,
)

COMMISSION_EMAIL = "reviewer@psncuc.gov"


def nc_account(account_number, email=None):
    account = authorize_account(account_number, email=email)
    app.save_household_profile(account_number, {"address": "1 E Edenton St, Raleigh, NC", "zip_code": "27601"})
    return account


def sc_account(account_number, email=None):
    account = authorize_account(account_number, email=email)
    app.save_household_profile(account_number, {"address": "1100 Gervais St, Columbia, SC", "zip_code": "29201"})
    return account


def commission_sign_in(client):
    bootstrap_staff()
    email_token_sign_in(client, COMMISSION_EMAIL)
    staff_user = app.get_staff_user_by_email(COMMISSION_EMAIL)
    assert staff_user is not None
    assert app.get_staff_commission_access(int(staff_user["id"])) is not None
    return staff_user


# --- Uploaded files belong to one account -------------------------------------------------


def test_customer_cannot_import_a_file_by_name_from_the_shared_folder(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    stub_utility_lookup(monkeypatch)
    app.ensure_data_dirs()
    authorize_account("acct-attacker", email="owner@example.com")
    app.save_household_profile("acct-attacker", {"address": "1 E Edenton St", "zip_code": "27601"})
    victim_file = app.INPUT_DIR / "victim-history.xml"
    victim_file.write_bytes(FIXTURE.read_bytes())
    client = app.web_app.test_client()
    customer_sign_in(client)

    response = client.post(
        "/api/analyze",
        json={"account_number": "acct-attacker", "input_file": victim_file.name},
    )

    assert response.status_code == 403
    assert app.find_account("acct-attacker") is not None
    with app.get_db_connection() as conn:
        imported = conn.execute("SELECT COUNT(*) AS count FROM imported_files").fetchone()
    assert int(imported["count"]) == 0


def test_resolve_input_file_rejects_sibling_folders_and_the_folder_itself(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.ensure_data_dirs()
    sibling = tmp_path / "input2"
    sibling.mkdir()
    (sibling / "other.xml").write_bytes(FIXTURE.read_bytes())

    with pytest.raises(ValueError):
        app.resolve_input_file("../input2/other.xml")
    with pytest.raises(ValueError):
        app.resolve_input_file(".")


def test_saved_uploads_get_unguessable_names(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.ensure_data_dirs()

    class Upload:
        filename = "Energy Usage.xml"

        def save(self, destination):
            destination.write_bytes(FIXTURE.read_bytes())

    first = app.save_uploaded_file(Upload())
    second = app.save_uploaded_file(Upload())

    assert first != second
    assert first.name != "Energy_Usage.xml"
    assert first.name.startswith("Energy_Usage-") and first.suffix == ".xml"
    assert len(first.stem.rsplit("-", 1)[1]) == 32


# --- Utility export URLs cannot reach private networks -----------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://utility.example.com/export.xml",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::ffff:169.254.169.254]/latest/meta-data/",
        "https://127.0.0.1/export.xml",
        "https://10.0.0.5/export.xml",
        "https://localhost/export.xml",
        "https://metadata.google.internal/computeMetadata/v1/",
        "https://utility.example.com:8443/export.xml",
        "https://user:pass@utility.example.com/export.xml",
        "file:///etc/passwd",
    ],
)
def test_utility_export_url_validation_rejects_unsafe_targets(url):
    with pytest.raises(ValueError):
        app.validate_utility_export_url(url)


def test_utility_export_url_validation_accepts_public_https_and_honors_allowlist(monkeypatch):
    assert app.validate_utility_export_url("https://utility.example.com/export.xml")
    monkeypatch.setenv("POWER_UTILITY_EXPORT_HOSTS", "greenbutton.example.org")
    assert app.validate_utility_export_url("https://api.greenbutton.example.org/export.xml")
    with pytest.raises(ValueError):
        app.validate_utility_export_url("https://utility.example.com/export.xml")


def test_utility_export_fetch_refuses_hosts_that_resolve_to_private_addresses(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr(app.socket, "getaddrinfo", fake_getaddrinfo)

    def must_not_open(*args, **kwargs):
        raise AssertionError("A private address must be refused before any request is made.")

    monkeypatch.setattr(app, "build_utility_export_opener", must_not_open)

    with pytest.raises(ValueError, match="private address"):
        app.fetch_utility_connection_export(
            {"access_identifier": "https://rebind.example.com/export.xml", "access_secret": "secret"}
        )


def test_utility_export_fetch_does_not_follow_redirects():
    handler = app._NoRedirectHandler()
    with pytest.raises(ValueError, match="redirected"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://169.254.169.254/")


def test_saving_a_utility_connection_rejects_a_metadata_url(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    authorize_account("acct-ssrf")
    with pytest.raises(ValueError):
        app.save_utility_connection(
            "acct-ssrf",
            {
                "provider_name": "Duke Energy",
                "access_method": "customer_api_key",
                "access_identifier": "https://169.254.169.254/latest/meta-data/iam/",
                "access_secret": "secret",
            },
        )


# --- Commission users only see their own jurisdiction -------------------------------------


def test_commission_user_cannot_download_reports_or_list_files_outside_jurisdiction(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.ensure_data_dirs()
    nc_account("acct-nc")
    sc_account("acct-sc")
    nc_report = app.OUTPUT_DIR / "nc-report.csv"
    sc_report = app.OUTPUT_DIR / "sc-report.csv"
    nc_report.write_text("date,flagged\n")
    sc_report.write_text("date,flagged\n")
    app.register_report_artifacts("acct-nc", [nc_report])
    app.register_report_artifacts("acct-sc", [sc_report])
    client = app.web_app.test_client()
    commission_sign_in(client)

    assert client.get(f"/reports/{nc_report.name}").status_code == 200
    assert client.get(f"/reports/{sc_report.name}").status_code == 404
    assert client.get("/api/files").status_code == 403


def test_commission_user_cannot_open_an_unassigned_account(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    sc_account("acct-sc-only")
    client = app.web_app.test_client()
    commission_sign_in(client)

    for path in ("/account", "/people", "/utility", "/inventory", "/history"):
        response = client.get(path, query_string={"account_number": "acct-sc-only"})
        assert response.status_code == 200
        assert b"1100 Gervais" not in response.data

    response = client.get("/account", query_string={"account_number": "brand-new-account"})
    assert response.status_code == 200
    assert app.find_account("brand-new-account") is None


def test_commission_user_with_assigned_account_is_redirected_to_it(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    nc_account("acct-nc-home")
    sc_account("acct-sc-home")
    client = app.web_app.test_client()
    commission_sign_in(client)

    response = client.get("/account", query_string={"account_number": "acct-sc-home"})

    assert response.status_code == 200
    assert b"1100 Gervais" not in response.data
    assert b"acct-nc-home" in response.data


def test_commission_user_sees_only_themselves_in_the_staff_list(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    commission_sign_in(client)

    response = client.get("/staff")

    assert response.status_code == 200
    assert COMMISSION_EMAIL.encode() in response.data
    assert b"commission@example.gov" not in response.data


def test_commission_user_can_turn_on_their_own_mfa(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    staff_user = commission_sign_in(client)

    response = client.post("/staff/security/mfa/start", follow_redirects=False)

    assert response.status_code == 302
    assert app.load_staff_mfa_enrollment(int(staff_user["id"])) is not None


# --- Redirects stay on this site ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/account?account_number=1", True),
        ("/", True),
        ("//evil.example", False),
        ("/\\evil.example", False),
        ("/\\/evil.example", False),
        ("https://evil.example", False),
        ("/ok\r\nLocation: https://evil.example", False),
        ("", False),
        (None, False),
    ],
)
def test_is_safe_local_path(value, expected):
    assert app.is_safe_local_path(value) is expected


def test_return_to_with_backslash_does_not_leave_the_site(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    authorize_account("acct-redirect")
    client = app.web_app.test_client()
    sign_in(client)

    response = client.post(
        "/account-access",
        data={
            "account_number": "acct-redirect",
            "email": "helper@example.com",
            "access_level": "Viewer",
            "return_to": "/\\evil.example",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "evil.example" not in response.headers["Location"]


# --- Billing ------------------------------------------------------------------------------


def test_viewer_cannot_open_billing_portal_or_checkout(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    install_fake_stripe(monkeypatch)
    manager = app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    app.create_customer_user("viewer@example.com", "Viewer", "customer-password-123")
    account = authorize_account("acct-billing", email="owner@example.com")
    app.add_account_access_email("acct-billing", "viewer@example.com", access_level="Viewer")
    app.upsert_customer_billing(
        int(manager["id"]),
        "home",
        "active",
        account_id=int(account["id"]),
        stripe_customer_id="cus_manager",
    )
    portal_calls = []
    monkeypatch.setattr(app, "create_customer_portal_session", lambda *args, **kwargs: portal_calls.append(args) or {"url": "https://billing.example/portal"})
    checkout_calls = []
    monkeypatch.setattr(app, "create_customer_checkout_session", lambda *args, **kwargs: checkout_calls.append(args) or {"id": "cs_x", "url": "https://billing.example/checkout"})
    client = app.web_app.test_client()
    customer_sign_in(client, "viewer@example.com")

    portal = client.post("/billing/portal", data={"account_number": "acct-billing"}, follow_redirects=False)
    checkout = client.post("/billing/checkout", data={"account_number": "acct-billing", "plan_id": "home"}, follow_redirects=False)

    assert portal.status_code == 302 and "billing.example" not in portal.headers["Location"]
    assert checkout.status_code == 302 and "billing.example" not in checkout.headers["Location"]
    assert portal_calls == [] and checkout_calls == []


def test_manager_can_still_open_billing_portal(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    app.create_customer_user("owner@example.com", "Home Owner", "customer-password-123")
    authorize_account("acct-billing", email="owner@example.com")
    monkeypatch.setattr(app, "create_customer_portal_session", lambda *args, **kwargs: {"url": "https://billing.example/portal"})
    client = app.web_app.test_client()
    customer_sign_in(client)

    portal = client.post("/billing/portal", data={"account_number": "acct-billing"}, follow_redirects=False)

    assert portal.headers["Location"] == "https://billing.example/portal"


def test_billing_success_ignores_a_checkout_session_started_by_someone_else(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    install_fake_stripe(monkeypatch)
    victim = app.create_customer_user("victim@example.com", "Victim", "customer-password-123")
    attacker = app.create_customer_user("owner@example.com", "Attacker", "customer-password-123")
    victim_account = authorize_account("acct-victim", email="victim@example.com")
    authorize_account("acct-attacker", email="owner@example.com")

    class ForeignSession(dict):
        pass

    foreign = ForeignSession(
        id="cs_victim",
        client_reference_id=str(victim["id"]),
        metadata={"customer_user_id": str(victim["id"]), "account_id": str(victim_account["id"]), "plan_id": "home"},
    )
    monkeypatch.setattr(app.stripe.checkout.Session, "retrieve", staticmethod(lambda *args, **kwargs: foreign))

    with pytest.raises(PermissionError):
        app.refresh_customer_billing_from_stripe(attacker, "cs_victim")


# --- Sign-in ------------------------------------------------------------------------------


def test_staff_email_listed_on_a_household_still_signs_in_as_staff(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    bootstrap_staff()
    authorize_account("acct-hijack", email="owner@example.com")
    app.add_account_access_email("acct-hijack", "commission@example.gov", access_level="Viewer")
    client = app.web_app.test_client()

    email_token_sign_in(client, "commission@example.gov")

    with client.session_transaction() as flask_session:
        assert flask_session.get("staff_user_id") is not None
        assert flask_session.get("customer_user_id") is None


def test_opening_the_sign_in_link_does_not_use_it_up(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    monkeypatch.setenv("POWER_EMAIL_BACKEND", "memory")
    authorize_account("acct-link", email="owner@example.com")
    client = app.web_app.test_client()
    app.EMAIL_OUTBOX.clear()
    client.post("/login", data={"email": "owner@example.com"})
    token = token_from_latest_email()

    for _ in range(3):
        page = client.get("/login/token", query_string={"token": token})
        assert page.status_code == 200
        assert b"Continue to Home Energy Watch" in page.data
        assert b"owner@example.com" in page.data
    with client.session_transaction() as flask_session:
        assert flask_session.get("customer_user_id") is None

    finished = client.post("/login/token", data={"token": token}, follow_redirects=False)
    assert finished.status_code == 302
    with client.session_transaction() as flask_session:
        assert flask_session.get("customer_user_id") is not None


def test_sign_in_requests_are_limited_per_email_across_addresses(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    statuses = []
    for attempt in range(22):
        response = client.post(
            "/login",
            data={"email": "target@example.com"},
            environ_base={"REMOTE_ADDR": f"198.51.100.{attempt + 1}"},
        )
        statuses.append(response.status_code)

    assert statuses[:20] == [200] * 20
    assert statuses[20:] == [429, 429]


def test_passkey_start_answers_the_same_for_unknown_emails(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    first = client.post("/login/passkey/start", data={"email": "nobody@example.com"})
    second = client.post("/login/passkey/start", data={"email": "nobody@example.com"})
    other = client.post("/login/passkey/start", data={"email": "someone-else@example.com"})

    assert first.status_code == second.status_code == 200
    first_key, second_key = first.get_json()["publicKey"], second.get_json()["publicKey"]
    # Stable like a real credential, and the same verification setting real accounts get.
    assert first_key["allowCredentials"] == second_key["allowCredentials"]
    assert first_key["allowCredentials"] != other.get_json()["publicKey"]["allowCredentials"]
    assert first_key.get("userVerification") == "preferred"
    assert client.post("/login/passkey/finish", json={}).status_code == 400


def test_passkey_login_start_uses_one_verification_setting_for_everyone(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    staff_user = app.create_first_staff_user("commission@example.gov", "Commissioner One", "test-password-123")
    server = install_fake_passkeys(monkeypatch)
    client = app.web_app.test_client()
    sign_in(client)
    client.post("/staff/security/passkeys/start", data={"nickname": "Laptop"})
    client.post("/staff/security/passkeys/finish", json={"id": "ignored"})
    client.post("/logout")

    client.post("/login/passkey/start", data={"email": "commission@example.gov"})

    login_call = [call for call in server.begin_calls if call["mode"] == "authenticate"][-1]
    assert login_call["user_verification"] == app.PASSKEY_USER_VERIFICATION
    assert staff_user is not None


def test_staff_passkey_sign_in_without_user_verification_is_refused(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    staff_user = app.create_first_staff_user("commission@example.gov", "Commissioner One", "test-password-123")
    install_fake_passkeys(monkeypatch)
    client = app.web_app.test_client()
    sign_in(client)
    client.post("/staff/security/passkeys/start", data={"nickname": "Laptop"})
    client.post("/staff/security/passkeys/finish", json={"id": "ignored"})
    client.post("/logout")
    monkeypatch.setattr(
        app.AuthenticationResponse,
        "from_dict",
        staticmethod(
            lambda payload: SimpleNamespace(
                response=SimpleNamespace(authenticator_data=SimpleNamespace(counter=9, is_user_verified=lambda: False))
            )
        ),
    )

    client.post("/login/passkey/start", data={"email": "commission@example.gov"})
    finish = client.post("/login/passkey/finish", json={"id": "ignored"})

    assert finish.status_code == 400
    with client.session_transaction() as flask_session:
        assert flask_session.get("staff_user_id") is None
    assert staff_user is not None


def test_staff_passkeys_require_user_verification():
    assert app.passkey_user_verification("staff") == app.UserVerificationRequirement.REQUIRED
    assert app.passkey_user_verification("customer") == app.PASSKEY_USER_VERIFICATION


def test_staff_with_mfa_must_enter_a_code_to_add_a_passkey(tmp_path, monkeypatch):
    import pyotp

    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    sign_in(client)
    staff_user = app.get_staff_user_by_email("commission@example.gov")
    enrollment = app.begin_staff_mfa_enrollment(int(staff_user["id"]))
    app.confirm_staff_mfa_enrollment(int(staff_user["id"]), pyotp.TOTP(str(enrollment["secret"])).now())
    refreshed = app.get_staff_user_by_id(int(staff_user["id"]))
    assert refreshed["mfa_enabled"]
    with client.session_transaction() as flask_session:
        flask_session["staff_auth_version"] = int(refreshed.get("auth_version") or 1)
    monkeypatch.setattr(app, "verify_staff_mfa_code", lambda staff_user_id, code: "authenticator" if code == "111111" else None)

    refused = client.post("/staff/security/passkeys/start", data={"nickname": "Laptop"})
    accepted = client.post("/staff/security/passkeys/start", data={"nickname": "Laptop", "code": "111111"})

    assert refused.status_code == 400
    assert "authenticator code" in refused.get_json()["error"]
    assert accepted.status_code == 200
    assert "challenge" in accepted.get_json()["publicKey"]

    statuses = [
        client.post("/staff/security/passkeys/start", data={"nickname": "Laptop", "code": "000000"}).status_code
        for _ in range(5)
    ]
    assert statuses[-1] == 429


def test_forwarded_host_header_is_ignored_without_trusted_proxy(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    with app.web_app.test_request_context("/", headers={"Host": "app.homeenergywatch.com", "X-Forwarded-Host": "evil.example"}):
        assert app.current_request_host() == "app.homeenergywatch.com"


def test_first_run_needs_the_setup_code_in_production(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    monkeypatch.setattr(app, "is_production_environment", lambda: True)
    client = app.web_app.test_client()
    form = {"email": "first@example.gov", "full_name": "First", "password": "first-password-123"}

    client.post("/first-run", data=form)
    assert app.count_staff_users() == 0

    monkeypatch.setenv("POWER_FIRST_RUN_TOKEN", "setup-code-123")
    client.post("/first-run", data={**form, "setup_token": "wrong"})
    assert app.count_staff_users() == 0

    client.post("/first-run", data={**form, "setup_token": "setup-code-123"})
    assert app.count_staff_users() == 1


# --- Postgres query translation ------------------------------------------------------------


def test_postgres_query_translation_escapes_literal_percent_signs():
    connection = app.DatabaseConnection.__new__(app.DatabaseConnection)
    connection.kind = "postgres"
    translated = connection._translate_query("SELECT 1 FROM t WHERE a LIKE 'x_%' AND b = ?")
    assert translated == "SELECT 1 FROM t WHERE a LIKE 'x_%%' AND b = %s"


# --- Review follow-ups --------------------------------------------------------------------


@pytest.mark.parametrize("address", ["64:ff9b::a9fe:a9fe", "::a9fe:a9fe", "64:ff9b:1::1", "::ffff:10.0.0.1", "::1"])
def test_ipv6_forms_of_private_addresses_are_not_public(address):
    assert app.is_public_ip_address(address) is False


def test_connection_level_check_refuses_a_private_peer(monkeypatch):
    class FakeSocket:
        closed = False

        def getpeername(self):
            return ("169.254.169.254", 443)

        def close(self):
            self.closed = True

    fake_socket = FakeSocket()

    def fake_connect(self):
        self.sock = fake_socket

    monkeypatch.setattr(app.http.client.HTTPSConnection, "connect", fake_connect)
    connection = app._PublicOnlyHTTPSConnection("rebind.example.com")

    with pytest.raises(ValueError, match="private address"):
        connection.connect()
    assert fake_socket.closed


def stripe_event(event_id, event_type, created, obj):
    return {"id": event_id, "type": event_type, "created": created, "data": {"object": obj}}


def test_late_checkout_completion_does_not_revive_a_deleted_subscription(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    applied = []
    monkeypatch.setattr(app, "handle_stripe_event", lambda event: applied.append(event["id"]))

    deleted = stripe_event("evt_deleted", "customer.subscription.deleted", 200, {"id": "sub_1", "status": "canceled"})
    late_checkout = stripe_event("evt_checkout", "checkout.session.completed", 100, {"id": "cs_1", "subscription": "sub_1"})

    assert app.process_stripe_event(deleted) == "processed"
    assert app.process_stripe_event(late_checkout) == "stale"
    assert applied == ["evt_deleted"]


def test_failed_stripe_event_can_be_retried(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    calls = []

    def flaky(event):
        calls.append(event["id"])
        if len(calls) == 1:
            raise RuntimeError("database hiccup")

    monkeypatch.setattr(app, "handle_stripe_event", flaky)
    event = stripe_event("evt_retry", "customer.subscription.updated", 100, {"id": "sub_2", "status": "active"})

    with pytest.raises(RuntimeError):
        app.process_stripe_event(event)
    assert app.process_stripe_event(event) == "processed"
    assert app.process_stripe_event(event) == "duplicate"
    assert calls == ["evt_retry", "evt_retry"]


def test_sign_in_link_that_no_longer_works_goes_back_to_sign_in(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()

    response = client.get("/login/token", query_string={"token": "not-a-real-token"}, follow_redirects=False)

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_production_logs_a_warning_for_unverified_database_tls(monkeypatch):
    events = []
    monkeypatch.setattr(app, "emit_application_log", lambda event, **fields: events.append((event, fields)))
    monkeypatch.setenv("POWER_ENV", "production")
    monkeypatch.setenv("POWER_APP_SECRET", "x" * 40)
    monkeypatch.setenv("POWER_AUDIT_SIGNING_KEY", "y" * 40)
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    monkeypatch.setenv("POWER_DATABASE_URL", "postgresql://u:p@db.example.com/hew?sslmode=require")
    with pytest.raises(RuntimeError):
        app.validate_runtime_security()  # later production checks (encryption key, email) are not configured here

    assert [fields["sslmode"] for event, fields in events if event == "security.database_tls_unverified"] == ["require"]

    events.clear()
    monkeypatch.setenv("POWER_DATABASE_URL", "postgresql://u:p@db.example.com/hew?sslmode=verify-full")
    with pytest.raises(RuntimeError):
        app.validate_runtime_security()
    assert not [event for event, _ in events if event == "security.database_tls_unverified"]
