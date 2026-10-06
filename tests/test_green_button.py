import json
import time
from io import BytesIO
from urllib.parse import parse_qs

import app
import pytest

from test_app import FIXTURE, authorize_account, configure_tmp_paths, customer_sign_in


ACCOUNT = "410000000001"
EMAIL = "green-button-owner@example.com"
ROOT = "https://utility.example/gbc/espi/1_1"
RESOURCE = f"{ROOT}/resource/Subscription/sub-1"
CUSTOMER = f"{ROOT}/resource/Customer/sub-1"
AUTHORIZATION = f"{ROOT}/resource/Authorization/auth-1"


@pytest.fixture
def utility(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    authorize_account(ACCOUNT, EMAIL)
    app.save_account_profile(ACCOUNT, energy_company="Con Edison")
    config = {
        "label": "Con Edison", "authorize_url": f"{ROOT}/oauth/authorize",
        "token_url": f"{ROOT}/oauth/Token", "client_id": "fixture-client",
        "client_secret": "fixture-secret", "bulk_id": "fixture-bulk",
        "approved": True, "scope": "configured-broad-scope",
        "resource_url": "https://wrong.example/shared-resource",
    }
    monkeypatch.setitem(app.GREEN_BUTTON_CONFIG, "CONED", config)
    # Exercise the unfinished connector only in isolated tests. Production
    # readiness must remain false even when an operator configures approval.
    monkeypatch.setattr(app, "GREEN_BUTTON_IMPLEMENTATION_READY", True)
    return config


def token_response(**overrides):
    return {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
            "expires_in": 3600, "scope": "customer-reduced-scope",
            "resourceURI": RESOURCE, "customerResourceURI": CUSTOMER,
            "authorizationURI": AUTHORIZATION, **overrides}


def authorization_document():
    return {"AccountNumber": ACCOUNT, "ResourceURI": RESOURCE,
            "RetailCustomerId": "retail-1", "AuthorizationUri": AUTHORIZATION,
            "subscriptions": [{"Id": "sub-1", "AuthorizationId": "auth-1"}]}


def binding():
    return {"account_number": ACCOUNT, "subscription_id": "sub-1",
            "retail_customer_id": "retail-1", "resource_uri": RESOURCE,
            "customer_resource_uri": CUSTOMER, "authorization_uri": AUTHORIZATION}


def saved_connection(**overrides):
    secret = app.serialize_green_button_secret("CONED", token_response(**overrides), binding=binding())
    saved = app.save_green_button_connection(ACCOUNT, "CONED", secret, RESOURCE)
    return app.load_utility_connection_for_sync(ACCOUNT, saved["id"])


def revoke():
    app.revoke_account_data_authorization(ACCOUNT, app.get_customer_user_by_email(EMAIL)["id"])


def db_connection_row(connection):
    with app.get_db_connection() as conn:
        return dict(conn.execute("SELECT * FROM utility_connections WHERE id=?", (connection["id"],)).fetchone())


def signed_in_client():
    app.web_app.config.update(TESTING=True)
    client = app.web_app.test_client()
    customer_sign_in(client, EMAIL)
    return client


def set_pending(client):
    with client.session_transaction() as session:
        session[app.GREEN_BUTTON_PENDING_SESSION_KEY] = {
            "provider_key": "CONED", "account_number": ACCOUNT,
            "state": "fixture-state", "started_at": app.timestamp_now(),
        }


class Response(BytesIO):
    def __init__(self, content=b"", status=200):
        super().__init__(content)
        self.status = status


class RecordingOpener:
    def __init__(self, content=b"", status=200):
        self.content, self.status, self.requests = content, status, []

    def open(self, request, **kwargs):
        self.requests.append(request)
        return Response(self.content, self.status)


def test_unfinished_connector_is_disabled_in_source():
    assert app.GREEN_BUTTON_IMPLEMENTATION_READY is False


def test_green_button_secret_uses_provider_resource_and_expiry():
    payload = app.serialize_green_button_secret(
        "CONED",
        {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600, "resourceURI": "https://provider.example/resource"},
    )
    parsed = app.parse_green_button_secret(payload)
    assert parsed["resource_url"] == "https://provider.example/resource"
    assert parsed["tokens"]["refresh_token"] == "refresh"
    assert parsed["tokens"]["expires_at"] >= int(time.time()) + 3500


def test_green_button_secret_rejects_missing_provider_resource():
    try:
        app.serialize_green_button_secret("CONED", {"access_token": "access"})
    except ValueError as exc:
        assert "resource" in str(exc).lower()
    else:
        raise AssertionError("missing provider resource must be rejected")


def test_green_button_provider_aliases_are_account_bound():
    assert app.green_button_provider_key("Con Edison") == "CONED"
    assert app.green_button_provider_key("Orange and Rockland") == "ORU"
    assert app.green_button_provider_key("Duke Energy") is None


def test_green_button_secret_never_serializes_unexpected_token_fields():
    payload = json.loads(app.serialize_green_button_secret(
        "ORU", {"access_token": "a", "refresh_token": "r", "resourceURI": "https://provider.example/r", "client_secret": "do-not-store"}
    ))
    assert "client_secret" not in payload["tokens"]


def test_token_zero_expiry_is_not_extended(monkeypatch):
    monkeypatch.setattr(app.time, "time", lambda: 1000)
    payload = app.parse_green_button_secret(app.serialize_green_button_secret("CONED", token_response(expires_in=0)))
    assert payload["tokens"]["expires_at"] == 1000


@pytest.mark.parametrize("url", [
    "http://utility.example/resource", "https://evil.example/resource",
    "https://utility.example:444/resource", "https://user@utility.example/resource",
    "https://utility.example/resource#fragment", "https://utility.example:bad/resource",
])
def test_requests_reject_unsafe_origins_before_http(utility, monkeypatch, url):
    opener = RecordingOpener()
    monkeypatch.setattr(app, "no_redirect_opener", lambda: opener)
    with pytest.raises(ValueError):
        app.green_button_request(utility, url, headers={"Authorization": "Bearer fixture"})
    assert opener.requests == []


@pytest.mark.parametrize("status", [202, 204])
def test_pending_or_empty_responses_are_not_exports(utility, monkeypatch, status):
    monkeypatch.setattr(app, "no_redirect_opener", lambda: RecordingOpener(status=status))
    with pytest.raises(ValueError):
        app.green_button_request(utility, RESOURCE, headers={})


def test_token_response_read_is_bounded(utility, monkeypatch):
    monkeypatch.setattr(app, "no_redirect_opener", lambda: RecordingOpener(b"12345"))
    with pytest.raises(ValueError, match="size"):
        app.green_button_request(utility, utility["token_url"], headers={}, data=b"grant_type=test", limit=4)


def test_authorization_mapping_is_bearer_authenticated_and_exact(utility, monkeypatch):
    opener = RecordingOpener(json.dumps(authorization_document()).encode())
    monkeypatch.setattr(app, "no_redirect_opener", lambda: opener)
    assert app.validate_green_button_customer_resource("CONED", token_response(), ACCOUNT) == binding()
    assert len(opener.requests) == 1
    assert opener.requests[0].full_url == AUTHORIZATION
    assert opener.requests[0].get_header("Authorization") == "Bearer fixture-access"


@pytest.mark.parametrize("change", [
    {"AccountNumber": "different-account"}, {"AccountNumber": None},
    {"ResourceURI": f"{ROOT}/resource/Subscription/other"},
    {"subscriptions": [{"Id": "other"}]},
    {"subscriptions": [{"Id": "sub-1"}, {"Id": "other"}]},
    {"RetailCustomerId": ""}, {"AuthorizationUri": f"{ROOT}/resource/Authorization/other"},
])
def test_mapping_rejects_mismatch_or_ambiguous_identity(utility, monkeypatch, change):
    document = {**authorization_document(), **change}
    monkeypatch.setattr(app, "green_button_request", lambda *a, **kw: json.dumps(document).encode())
    with pytest.raises(ValueError, match="match"):
        app.validate_green_button_customer_resource("CONED", token_response(), ACCOUNT)


@pytest.mark.parametrize("document", [
    {"accountId": ACCOUNT}, {"unrelated": {"AccountNumber": ACCOUNT}},
    [{"AccountNumber": ACCOUNT}], {"AuthorizationModel": authorization_document()},
])
def test_mapping_does_not_search_unknown_shapes_for_account_numbers(utility, monkeypatch, document):
    monkeypatch.setattr(app, "green_button_request", lambda *a, **kw: json.dumps(document).encode())
    with pytest.raises(ValueError):
        app.validate_green_button_customer_resource("CONED", token_response(), ACCOUNT)


def test_customer_uri_must_refer_to_same_subscription(utility, monkeypatch):
    monkeypatch.setattr(app, "green_button_request", lambda *a, **kw: pytest.fail("must reject before HTTP"))
    with pytest.raises(ValueError):
        app.validate_green_button_customer_resource("CONED", token_response(customerResourceURI=f"{ROOT}/resource/Customer/other"), ACCOUNT)


def test_callback_persists_binding_not_configured_shared_resource(utility, monkeypatch):
    client = signed_in_client()
    set_pending(client)
    requests = []

    def respond(config, url, **kwargs):
        requests.append(url)
        return json.dumps(token_response() if url == utility["token_url"] else authorization_document()).encode()

    monkeypatch.setattr(app, "green_button_request", respond)
    response = client.get("/utility-connection/green-button/CONED/callback?state=fixture-state&code=fixture-code")
    assert response.status_code == 302
    saved = app.list_utility_connections(ACCOUNT)
    assert len(saved) == 1
    connection = app.load_utility_connection_for_sync(ACCOUNT, saved[0]["id"])
    payload = app.parse_green_button_secret(connection["access_secret"])
    assert payload["binding"] == binding()
    assert payload["resource_url"] == RESOURCE
    assert requests == [utility["token_url"], AUTHORIZATION]


def test_mismatched_callback_never_saves_connection(utility, monkeypatch):
    client = signed_in_client()
    set_pending(client)
    def respond(config, url, **kwargs):
        document = token_response() if url == utility["token_url"] else {**authorization_document(), "AccountNumber": "other"}
        return json.dumps(document).encode()
    monkeypatch.setattr(app, "green_button_request", respond)
    client.get("/utility-connection/green-button/CONED/callback?state=fixture-state&code=fixture-code")
    assert app.list_utility_connections(ACCOUNT) == []


@pytest.mark.parametrize("failure", ["state", "expired", "consent"])
def test_callback_checks_session_and_permission_before_http(utility, monkeypatch, failure):
    client = signed_in_client()
    set_pending(client)
    if failure == "expired":
        with client.session_transaction() as session:
            pending = dict(session[app.GREEN_BUTTON_PENDING_SESSION_KEY])
            pending["started_at"] = "2000-01-01T00:00:00"
            session[app.GREEN_BUTTON_PENDING_SESSION_KEY] = pending
    elif failure == "consent":
        revoke()
    monkeypatch.setattr(app, "green_button_request", lambda *a, **kw: pytest.fail("invalid callback must not exchange code"))
    state = "wrong-state" if failure == "state" else "fixture-state"
    client.get(f"/utility-connection/green-button/CONED/callback?state={state}&code=fixture-code")
    assert app.list_utility_connections(ACCOUNT) == []


def test_readiness_blocks_ui_start_callback_manual_and_scheduled_sync(utility, monkeypatch):
    connection = saved_connection()
    client = signed_in_client()
    monkeypatch.setattr(app, "GREEN_BUTTON_IMPLEMENTATION_READY", False)
    monkeypatch.setattr(app, "no_redirect_opener", lambda: pytest.fail("readiness gate must prevent HTTP"))
    assert not app.green_button_provider_is_ready("CONED")
    assert not any(guide["id"] == "green_button_coned" for guide in app.list_utility_access_guides())
    page = client.get(f"/customer/utility?account_number={ACCOUNT}")
    assert b"/green-button/CONED/start" not in page.data
    start = client.post("/utility-connection/green-button/CONED/start", data={"account_number": ACCOUNT})
    assert start.status_code == 302
    assert not start.location.startswith("https://utility.example")
    with client.session_transaction() as session:
        assert app.GREEN_BUTTON_PENDING_SESSION_KEY not in session
    set_pending(client)
    previous_secret = db_connection_row(connection)["secret_token"]
    client.get("/utility-connection/green-button/CONED/callback?state=fixture-state&code=fixture-code")
    assert db_connection_row(connection)["secret_token"] == previous_secret
    with pytest.raises(ValueError, match="not available"):
        app.sync_utility_connection(ACCOUNT, connection["id"])
    scheduled = app.run_scheduled_utility_sync(ACCOUNT)
    assert scheduled["succeeded"] == 0
    assert scheduled["failed"] == 1
    assert app.load_intervals_from_db(ACCOUNT).empty


@pytest.mark.parametrize("path", ["callback", "refresh", "resource"])
def test_all_bearer_and_basic_requests_reject_redirects(utility, monkeypatch, path):
    requests = []
    class RedirectingOpener:
        def open(self, request, **kwargs):
            requests.append(request)
            return app._RejectRedirects().redirect_request(request, None, 302, "Moved", {}, "https://evil.example/")
    monkeypatch.setattr(app, "no_redirect_opener", RedirectingOpener)
    if path == "callback":
        client = signed_in_client()
        set_pending(client)
        client.get("/utility-connection/green-button/CONED/callback?state=fixture-state&code=fixture-code")
        assert app.list_utility_connections(ACCOUNT) == []
    else:
        connection = saved_connection(expires_in=0 if path == "refresh" else 3600)
        if path == "resource":
            monkeypatch.setattr(app, "validate_green_button_customer_resource", lambda *a: binding())
        with pytest.raises(ValueError, match="redirect"):
            app.fetch_green_button_export(connection)
    assert len(requests) == 1
    assert requests[0].full_url == (RESOURCE if path == "resource" else utility["token_url"])
    assert requests[0].get_header("Authorization").startswith("Bearer " if path == "resource" else "Basic ")


@pytest.mark.parametrize("scope", ["customer-reduced-scope", None])
def test_repeated_refresh_never_reintroduces_configured_scope(utility, monkeypatch, scope):
    connection = saved_connection(expires_in=0, scope=scope)
    forms = []
    def respond(config, url, **kwargs):
        if url == config["token_url"]:
            forms.append(parse_qs(kwargs["data"].decode()))
            return json.dumps({"access_token": f"refreshed-{len(forms)}", "expires_in": 0}).encode()
        if url == AUTHORIZATION:
            return json.dumps(authorization_document()).encode()
        return FIXTURE.read_bytes()
    monkeypatch.setattr(app, "green_button_request", respond)
    for _ in range(2):
        app.fetch_green_button_export(connection)
        connection = app.load_utility_connection_for_sync(ACCOUNT, connection["id"])
        assert app.parse_green_button_secret(connection["access_secret"])["tokens"]["scope"] == scope
    assert len(forms) == 2
    assert all(form.get("scope") == ([scope] if scope else None) for form in forms)
    assert all(form["refresh_token"] == ["fixture-refresh"] for form in forms)


def test_revoke_during_refresh_cannot_restore_credentials(utility, monkeypatch):
    connection = saved_connection(expires_in=0)
    def respond(config, url, **kwargs):
        assert url == config["token_url"], "no resource may be fetched after revocation"
        revoke()
        return json.dumps({"access_token": "too-late", "expires_in": 3600}).encode()
    monkeypatch.setattr(app, "green_button_request", respond)
    with pytest.raises(ValueError, match="withdrawn"):
        app.fetch_green_button_export(connection)
    row = db_connection_row(connection)
    assert row["secret_token"] is None and row["secret_hash"] is None
    assert row["status"] == "Authorization withdrawn"
    assert app.load_intervals_from_db(ACCOUNT).empty


def test_stale_refresh_cannot_replace_new_connection(utility):
    old = saved_connection(expires_in=0)
    current = saved_connection(access_token="new-connection")
    with pytest.raises(ValueError, match="changed"):
        app.update_green_button_connection_secret(ACCOUNT, old["id"], old["access_secret"], expected_secret=old["access_secret"])
    assert app.load_utility_connection_for_sync(ACCOUNT, old["id"])["access_secret"] == current["access_secret"]


def test_revoke_during_download_prevents_import(utility, monkeypatch):
    connection = saved_connection()
    def respond(config, url, **kwargs):
        if url == AUTHORIZATION:
            return json.dumps(authorization_document()).encode()
        assert url == RESOURCE
        revoke()
        return FIXTURE.read_bytes()
    monkeypatch.setattr(app, "green_button_request", respond)
    with pytest.raises(ValueError, match="withdrawn"):
        app.sync_utility_connection(ACCOUNT, connection["id"])
    assert app.load_intervals_from_db(ACCOUNT).empty
    assert list(app.INPUT_DIR.iterdir()) == []


def test_transaction_guard_rejects_revoke_between_parse_and_insert(utility):
    connection = saved_connection()
    frame = app.parse_interval_file(FIXTURE).frame
    revoke()
    with pytest.raises(ValueError, match="withdrawn"):
        app.import_interval_frame_to_db(frame, str(FIXTURE), ACCOUNT, utility_connection_guard=connection)
    assert app.load_intervals_from_db(ACCOUNT).empty
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM imported_files").fetchone()["n"] == 0


def test_transaction_guard_imports_valid_data_and_deduplicates(utility):
    connection = saved_connection()
    frame = app.parse_interval_file(FIXTURE).frame
    app.import_interval_frame_to_db(frame, str(FIXTURE), ACCOUNT, utility_connection_guard=connection)
    app.import_interval_frame_to_db(frame, str(FIXTURE), ACCOUNT, utility_connection_guard=connection)
    assert len(app.load_intervals_from_db(ACCOUNT)) == len(frame)


def test_missing_saved_binding_is_not_accepted(utility, monkeypatch):
    connection = saved_connection()
    payload = json.loads(connection["access_secret"])
    payload.pop("binding")
    connection["access_secret"] = json.dumps(payload)
    monkeypatch.setattr(app, "no_redirect_opener", lambda: pytest.fail("unbound connections must not fetch data"))
    with pytest.raises(ValueError, match="mapping"):
        app.fetch_green_button_export(connection)


def test_generic_feed_form_cannot_inject_green_button_connection(utility):
    with pytest.raises(ValueError, match="sign-in"):
        app.save_utility_connection(ACCOUNT, {"access_method": app.GREEN_BUTTON_ACCESS_METHOD})
    assert app.list_utility_connections(ACCOUNT) == []
