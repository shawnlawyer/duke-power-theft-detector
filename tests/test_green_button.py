"""Con Edison / Orange & Rockland Green Button connector (switched off in source).

Fixtures are synthetic. They exercise the safety rules but do not prove
compatibility with the utility's real responses.
"""
import json
import time
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

import app
import coned_oru as gb
from test_app import FIXTURE, authorize_account, configure_tmp_paths, customer_sign_in


ACCOUNT = "410000000001"
EMAIL = "green-button-owner@example.com"
PUBLIC = "https://app.homeenergywatch.com"
ROOT = "https://utility.example/gbc/espi/1_1"
TOKEN_URL = f"{ROOT}/oauth/Token"
RESOURCE = f"{ROOT}/resource/Subscription/sub-1"
CUSTOMER = f"{ROOT}/resource/Customer/sub-1"
AUTHORIZATION = f"{ROOT}/resource/Authorization/auth-1"


@pytest.fixture
def utility(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    authorize_account(ACCOUNT, EMAIL)
    app.save_account_profile(ACCOUNT, energy_company="Con Edison")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", PUBLIC)
    for key, value in {"AUTHORIZE_URL": f"{ROOT}/oauth/authorize", "TOKEN_URL": TOKEN_URL,
                       "CLIENT_ID": "fixture-client", "CLIENT_SECRET": "fixture-secret",
                       "BULK_ID": "fixture-bulk", "SCOPE": "configured-broad-scope", "APPROVED": "true",
                       "REDIRECT_URI": PUBLIC + gb.callback_path("CONED")}.items():
        monkeypatch.setenv("POWER_CONED_GREEN_BUTTON_" + key, value)
    # Exercise the unfinished connector only in isolated tests. Production
    # readiness stays False even when an operator configures approval.
    monkeypatch.setattr(gb, "IMPLEMENTATION_READY", True)
    return gb.config("CONED")


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
    secret = gb.serialize_secret("CONED", token_response(**overrides), binding=binding())
    connection_id = gb.save_connection(app, app.find_account(ACCOUNT), "CONED", secret)
    return app.load_utility_connection_for_sync(ACCOUNT, connection_id)


def revoke():
    app.revoke_account_data_authorization(ACCOUNT, app.get_customer_user_by_email(EMAIL)["id"])


def db_row(connection):
    with app.get_db_connection() as conn:
        return dict(conn.execute("SELECT * FROM utility_connections WHERE id=?", (connection["id"],)).fetchone())


def signed_in_client():
    client = app.web_app.test_client()
    customer_sign_in(client, EMAIL)
    return client


def start(client):
    response = client.post("/utility-connection/green-button/coned/start", data={"account_number": ACCOUNT})
    assert response.status_code == 302
    location = urlsplit(response.location)
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{ROOT}/oauth/authorize"
    return parse_qs(location.query)


def callback(client, state, code="fixture-code"):
    return client.get(f"/utility-connection/green-button/coned/callback?state={state}&code={code}")


def utility_responses(document=None, export=None, token=None):
    calls = []

    def respond(cfg, url, **kwargs):
        calls.append((url, kwargs))
        if url == TOKEN_URL:
            return json.dumps(token or token_response()).encode()
        if url == AUTHORIZATION:
            return json.dumps(document or authorization_document()).encode()
        if url == RESOURCE:
            return export if export is not None else FIXTURE.read_bytes()
        raise AssertionError("Unexpected endpoint: " + url)
    return respond, calls


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
    assert gb.IMPLEMENTATION_READY is False


def test_configured_and_approved_is_still_unavailable_while_disabled(utility, monkeypatch):
    monkeypatch.setattr(gb, "IMPLEMENTATION_READY", False)
    assert not gb.available("CONED")
    with pytest.raises(gb.ConnectionError, match="not available"):
        gb.config("CONED")


@pytest.mark.parametrize("name,value", [
    ("APPROVED", "false"), ("CLIENT_SECRET", ""), ("BULK_ID", ""),
    ("AUTHORIZE_URL", "https://other.example/oauth/authorize"),
    ("TOKEN_URL", "http://utility.example/gbc/espi/1_1/oauth/Token"),
    ("REDIRECT_URI", "https://evil.example/utility-connection/green-button/coned/callback"),
    ("REDIRECT_URI", PUBLIC + "/utility-connection/green-button/oru/callback"),
])
def test_config_requires_approval_and_safe_addresses(utility, monkeypatch, name, value):
    monkeypatch.setenv("POWER_CONED_GREEN_BUTTON_" + name, value)
    assert not gb.available("CONED")


def test_providers_are_separate(utility):
    assert not gb.available("ORU")
    with pytest.raises(gb.ConnectionError):
        gb.config("DUKE")


def test_secret_uses_provider_resource_and_expiry():
    payload = gb.parse_secret(gb.serialize_secret(
        "CONED", {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600,
                  "resourceURI": "https://provider.example/resource"}))
    assert payload["resource_url"] == "https://provider.example/resource"
    assert payload["tokens"]["refresh_token"] == "refresh"
    assert payload["tokens"]["expires_at"] >= int(time.time()) + 3500


def test_secret_rejects_missing_provider_resource():
    with pytest.raises(gb.ConnectionError, match="resource"):
        gb.serialize_secret("CONED", {"access_token": "access"})


@pytest.mark.parametrize("expires_in", [-1, 86400 * 31, "soon"])
def test_secret_rejects_invalid_expiry(expires_in):
    with pytest.raises(gb.ConnectionError, match="expiry"):
        gb.serialize_secret("CONED", token_response(expires_in=expires_in))


def test_provider_aliases_are_account_bound():
    assert gb.provider_key("Con Edison") == "CONED"
    assert gb.provider_key("  orange and   rockland ") == "ORU"
    assert gb.provider_key("Duke Energy") is None
    assert gb.provider_key("Not Con Edison at all") is None


def test_secret_never_serializes_unexpected_token_fields():
    payload = json.loads(gb.serialize_secret(
        "ORU", {"access_token": "a", "refresh_token": "r", "resourceURI": "https://provider.example/r",
                "client_secret": "do-not-store"}))
    assert "client_secret" not in payload["tokens"]


def test_token_zero_expiry_is_not_extended(monkeypatch):
    monkeypatch.setattr(gb.time, "time", lambda: 1000)
    payload = gb.parse_secret(gb.serialize_secret("CONED", token_response(expires_in=0)))
    assert payload["tokens"]["expires_at"] == 1000


@pytest.mark.parametrize("url", [
    "http://utility.example/resource", "https://evil.example/resource",
    "https://utility.example:444/resource", "https://user@utility.example/resource",
    "https://utility.example/resource#fragment", "https://utility.example:bad/resource",
])
def test_requests_reject_unsafe_origins_before_http(utility, monkeypatch, url):
    opener = RecordingOpener()
    monkeypatch.setattr(gb, "opener", lambda: opener)
    with pytest.raises(gb.ConnectionError):
        gb.request_bytes(utility, url, headers={"Authorization": "Bearer fixture"})
    assert opener.requests == []


@pytest.mark.parametrize("status", [202, 204])
def test_pending_or_empty_responses_are_not_exports(utility, monkeypatch, status):
    monkeypatch.setattr(gb, "opener", lambda: RecordingOpener(b"<feed/>", status=status))
    with pytest.raises(gb.ConnectionError):
        gb.request_bytes(utility, RESOURCE, headers={})


def test_response_read_is_bounded(utility, monkeypatch):
    monkeypatch.setattr(gb, "opener", lambda: RecordingOpener(b"12345"))
    with pytest.raises(gb.ConnectionError, match="size"):
        gb.request_bytes(utility, TOKEN_URL, headers={}, data=b"grant_type=test", limit=4)


def test_redirect_handler_refuses_to_follow():
    with pytest.raises(gb.ConnectionError, match="redirect"):
        gb.RejectRedirects().redirect_request(None, None, 302, "Moved", {}, "https://evil.example/")


@pytest.mark.parametrize("code,expected", [(401, gb.AuthorizationExpired), (403, gb.AuthorizationExpired),
                                           (500, gb.ConnectionError)])
def test_http_errors_are_classified_and_redacted(utility, monkeypatch, code, expected):
    class Failing:
        def open(self, request, **kwargs):
            raise HTTPError(request.full_url, code, "secret-detail", {}, BytesIO(b"fixture-access"))
    monkeypatch.setattr(gb, "opener", Failing)
    with pytest.raises(expected) as caught:
        gb.request_bytes(utility, RESOURCE, headers={})
    assert "secret" not in str(caught.value) and "fixture" not in str(caught.value)


def test_authorization_mapping_is_bearer_authenticated_and_exact(utility, monkeypatch):
    opener = RecordingOpener(json.dumps(authorization_document()).encode())
    monkeypatch.setattr(gb, "opener", lambda: opener)
    assert gb.validate_customer_resource("CONED", token_response(), ACCOUNT) == binding()
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
    monkeypatch.setattr(gb, "request_bytes", lambda *a, **kw: json.dumps(document).encode())
    with pytest.raises(gb.ConnectionError, match="match"):
        gb.validate_customer_resource("CONED", token_response(), ACCOUNT)


@pytest.mark.parametrize("document", [
    {"accountId": ACCOUNT}, {"unrelated": {"AccountNumber": ACCOUNT}},
    [{"AccountNumber": ACCOUNT}], {"AuthorizationModel": authorization_document()},
])
def test_mapping_does_not_search_unknown_shapes_for_account_numbers(utility, monkeypatch, document):
    monkeypatch.setattr(gb, "request_bytes", lambda *a, **kw: json.dumps(document).encode())
    with pytest.raises(gb.ConnectionError):
        gb.validate_customer_resource("CONED", token_response(), ACCOUNT)


@pytest.mark.parametrize("change", [
    {"customerResourceURI": f"{ROOT}/resource/Customer/other"},
    {"authorizationURI": "https://evil.example/gbc/espi/1_1/resource/Authorization/auth-1"},
    {"resourceURI": f"{ROOT}/resource/Subscription/sub-1?x=1"},
    {"access_token": ""},
])
def test_token_relationships_are_checked_before_http(utility, monkeypatch, change):
    monkeypatch.setattr(gb, "request_bytes", lambda *a, **kw: pytest.fail("must reject before HTTP"))
    with pytest.raises(gb.ConnectionError):
        gb.validate_customer_resource("CONED", token_response(**change), ACCOUNT)


def test_start_sends_bulk_scope_and_registered_return_address(utility):
    params = start(signed_in_client())
    assert params["client_id"] == ["fixture-client"]
    assert params["redirect_uri"] == [PUBLIC + gb.callback_path("CONED")]
    assert params["scope"] == ["configured-broad-scope;BR=fixture-bulk"]
    assert len(params["state"][0]) >= 32


def test_callback_persists_binding_not_configured_resource(utility, monkeypatch):
    client = signed_in_client()
    state = start(client)["state"][0]
    respond, calls = utility_responses()
    monkeypatch.setattr(gb, "request_bytes", respond)
    response = callback(client, state)
    assert response.status_code == 302
    assert "no-store" in response.headers["Cache-Control"]
    saved = app.list_utility_connections(ACCOUNT)
    assert len(saved) == 1 and saved[0]["provider_name"] == "Con Edison"
    payload = gb.parse_secret(app.load_utility_connection_for_sync(ACCOUNT, saved[0]["id"])["access_secret"])
    assert payload["binding"] == binding()
    assert payload["resource_url"] == RESOURCE
    assert [url for url, _ in calls] == [TOKEN_URL, AUTHORIZATION]
    # The return link cannot be replayed.
    monkeypatch.setattr(gb, "request_bytes", lambda *a, **kw: pytest.fail("replay must not exchange a code"))
    callback(client, state)


def test_mismatched_callback_never_saves_connection(utility, monkeypatch):
    client = signed_in_client()
    state = start(client)["state"][0]
    respond, _ = utility_responses(document={**authorization_document(), "AccountNumber": "other"})
    monkeypatch.setattr(gb, "request_bytes", respond)
    callback(client, state)
    assert app.list_utility_connections(ACCOUNT) == []


@pytest.mark.parametrize("failure", ["state", "expired", "consent", "browser", "provider", "denied"])
def test_callback_checks_state_session_and_permission_before_http(utility, monkeypatch, failure):
    client = signed_in_client()
    state = start(client)["state"][0]
    if failure == "expired":
        with app.get_db_connection() as conn:
            conn.execute("UPDATE coned_oru_oauth_states SET expires_at = 0")
            conn.commit()
    elif failure == "consent":
        revoke()
    elif failure == "browser":
        with client.session_transaction() as session:
            session["coned_oru_browser"] = "another-browser"
    monkeypatch.setattr(gb, "request_bytes", lambda *a, **kw: pytest.fail("invalid callback must not exchange code"))
    if failure == "state":
        callback(client, "x" * 40)
    elif failure == "provider":
        monkeypatch.setenv("POWER_ORU_GREEN_BUTTON_APPROVED", "true")
        client.get(f"/utility-connection/green-button/oru/callback?state={state}&code=fixture-code")
    elif failure == "denied":
        client.get(f"/utility-connection/green-button/coned/callback?state={state}&error=access_denied")
    else:
        callback(client, state)
    assert app.list_utility_connections(ACCOUNT) == []


def test_csrf_viewer_and_wrong_utility_cannot_start(utility):
    client = signed_in_client()
    app.web_app.config["CSRF_ENFORCE_TESTS"] = True
    try:
        assert client.post("/utility-connection/green-button/coned/start", data={"account_number": ACCOUNT}).status_code == 400
    finally:
        app.web_app.config["CSRF_ENFORCE_TESTS"] = False
    with app.get_db_connection() as conn:
        conn.execute("UPDATE account_access_emails SET access_level = 'Viewer'")
        conn.commit()
    response = client.post("/utility-connection/green-button/coned/start", data={"account_number": ACCOUNT})
    assert not response.location.startswith(ROOT)
    with app.get_db_connection() as conn:
        conn.execute("UPDATE account_access_emails SET access_level = 'Manager'")
        conn.commit()
    app.save_account_profile(ACCOUNT, energy_company="Duke Energy")
    response = client.post("/utility-connection/green-button/coned/start", data={"account_number": ACCOUNT})
    assert not response.location.startswith(ROOT)


def test_full_sync_imports_and_repeated_sync_does_not_duplicate(utility, monkeypatch):
    connection = saved_connection()
    respond, calls = utility_responses()
    monkeypatch.setattr(gb, "request_bytes", respond)
    first = app.sync_utility_connection(ACCOUNT, connection["id"])
    count = len(app.load_intervals_from_db(ACCOUNT))
    assert first["status"] == "Synced" and count > 0
    app.sync_utility_connection(ACCOUNT, connection["id"])
    assert len(app.load_intervals_from_db(ACCOUNT)) == count
    resource_calls = [kwargs for url, kwargs in calls if url == RESOURCE]
    assert resource_calls and all(kw["headers"]["Authorization"] == "Bearer fixture-access" for kw in resource_calls)
    assert list(app.INPUT_DIR.iterdir()) == []


def test_readiness_blocks_ui_start_callback_manual_and_scheduled_sync(utility, monkeypatch):
    connection = saved_connection()
    client = signed_in_client()
    state = start(client)["state"][0]
    monkeypatch.setattr(gb, "IMPLEMENTATION_READY", False)
    monkeypatch.setattr(gb, "opener", lambda: pytest.fail("readiness gate must prevent HTTP"))
    page = client.get(f"/customer/utility?account_number={ACCOUNT}")
    assert b"/green-button/coned/start" not in page.data
    assert b"Automatic updates are being prepared" in page.data
    response = client.post("/utility-connection/green-button/coned/start", data={"account_number": ACCOUNT})
    assert not response.location.startswith(ROOT)
    previous_secret = db_row(connection)["secret_token"]
    callback(client, state)
    assert db_row(connection)["secret_token"] == previous_secret
    with pytest.raises(ValueError, match="not available"):
        app.sync_utility_connection(ACCOUNT, connection["id"])
    scheduled = app.run_scheduled_utility_sync(ACCOUNT)
    assert scheduled["succeeded"] == 0 and scheduled["failed"] == 1
    assert app.load_intervals_from_db(ACCOUNT).empty


def test_page_shows_connect_button_only_when_ready(utility):
    page = signed_in_client().get(f"/customer/utility?account_number={ACCOUNT}")
    assert b"/utility-connection/green-button/coned/start" in page.data


@pytest.mark.parametrize("scope", ["customer-reduced-scope", None])
def test_repeated_refresh_never_reintroduces_configured_scope(utility, monkeypatch, scope):
    connection = saved_connection(expires_in=0, scope=scope)
    forms = []

    def respond(cfg, url, **kwargs):
        if url == TOKEN_URL:
            forms.append(parse_qs(kwargs["data"].decode()))
            return json.dumps({"access_token": f"refreshed-{len(forms)}", "expires_in": 0}).encode()
        if url == AUTHORIZATION:
            return json.dumps(authorization_document()).encode()
        return FIXTURE.read_bytes()
    monkeypatch.setattr(gb, "request_bytes", respond)
    for _ in range(2):
        gb.fetch_export(app, connection)
        connection = app.load_utility_connection_for_sync(ACCOUNT, connection["id"])
        assert gb.parse_secret(connection["access_secret"])["tokens"]["scope"] == scope
    assert len(forms) == 2
    assert all(form.get("scope") == ([scope] if scope else None) for form in forms)
    assert all(form["refresh_token"] == ["fixture-refresh"] for form in forms)


def test_revoke_during_refresh_cannot_restore_credentials(utility, monkeypatch):
    connection = saved_connection(expires_in=0)

    def respond(cfg, url, **kwargs):
        assert url == TOKEN_URL, "no resource may be fetched after revocation"
        revoke()
        return json.dumps({"access_token": "too-late", "expires_in": 3600}).encode()
    monkeypatch.setattr(gb, "request_bytes", respond)
    with pytest.raises(gb.ConnectionError, match="withdrawn"):
        gb.fetch_export(app, connection)
    row = db_row(connection)
    assert row["secret_token"] is None and row["secret_hash"] is None
    assert row["status"] == "Authorization withdrawn"
    assert app.load_intervals_from_db(ACCOUNT).empty


def test_stale_refresh_cannot_replace_new_connection(utility):
    old = saved_connection(expires_in=0)
    current = saved_connection(access_token="new-connection")
    with pytest.raises(gb.ConnectionError, match="changed"):
        gb.replace_secret(app, dict(old), old["access_secret"])
    assert app.load_utility_connection_for_sync(ACCOUNT, old["id"])["access_secret"] == current["access_secret"]


def test_revoke_during_download_prevents_import(utility, monkeypatch):
    connection = saved_connection()

    def respond(cfg, url, **kwargs):
        if url == AUTHORIZATION:
            return json.dumps(authorization_document()).encode()
        assert url == RESOURCE
        revoke()
        return FIXTURE.read_bytes()
    monkeypatch.setattr(gb, "request_bytes", respond)
    with pytest.raises(ValueError, match="withdrawn"):
        app.sync_utility_connection(ACCOUNT, connection["id"])
    assert app.load_intervals_from_db(ACCOUNT).empty
    assert list(app.INPUT_DIR.iterdir()) == []


def test_reconnect_during_download_preserves_new_connection(utility, monkeypatch):
    connection = saved_connection()

    def respond(cfg, url, **kwargs):
        if url == AUTHORIZATION:
            return json.dumps(authorization_document()).encode()
        saved_connection(access_token="reconnected")
        return FIXTURE.read_bytes()
    monkeypatch.setattr(gb, "request_bytes", respond)
    with pytest.raises(ValueError, match="changed"):
        app.sync_utility_connection(ACCOUNT, connection["id"])
    assert app.load_intervals_from_db(ACCOUNT).empty
    secret = app.load_utility_connection_for_sync(ACCOUNT, connection["id"])["access_secret"]
    assert gb.parse_secret(secret)["tokens"]["access_token"] == "reconnected"


def test_transaction_guard_rejects_revoke_between_parse_and_insert(utility):
    connection = saved_connection()
    frame = app.parse_interval_file(FIXTURE).frame
    revoke()
    with pytest.raises(ValueError):
        app.import_interval_frame_to_db(frame, str(FIXTURE), ACCOUNT,
                                        connection_guard=(connection["id"], connection["access_secret"]))
    assert app.load_intervals_from_db(ACCOUNT).empty


def test_provider_revocation_clears_secret(utility, monkeypatch):
    connection = saved_connection()

    def respond(cfg, url, **kwargs):
        raise gb.AuthorizationExpired("Con Edison access has ended or needs renewal. Connect again.")
    monkeypatch.setattr(gb, "request_bytes", respond)
    with pytest.raises(gb.AuthorizationExpired):
        app.sync_utility_connection(ACCOUNT, connection["id"])
    row = db_row(connection)
    assert row["secret_token"] is None and row["status"] == "Reconnect Con Edison"


def test_sync_lease_blocks_second_worker(utility, monkeypatch):
    connection = saved_connection()
    with app.get_db_connection() as conn:
        conn.execute("INSERT INTO coned_oru_sync_locks VALUES (?, ?, ?)", (connection["id"], "other-worker", int(time.time()) + 300))
        conn.commit()
    monkeypatch.setattr(gb, "request_bytes", lambda *a, **kw: pytest.fail("locked sync must not fetch"))
    with pytest.raises(ValueError, match="already syncing"):
        app.sync_utility_connection(ACCOUNT, connection["id"])


def test_missing_saved_binding_is_not_accepted(utility, monkeypatch):
    connection = saved_connection()
    payload = json.loads(connection["access_secret"])
    payload.pop("binding")
    connection["access_secret"] = json.dumps(payload)
    monkeypatch.setattr(gb, "opener", lambda: pytest.fail("unbound connections must not fetch data"))
    with pytest.raises(gb.ConnectionError, match="mapping"):
        gb.fetch_export(app, connection)


def test_changed_mapping_stops_sync(utility, monkeypatch):
    connection = saved_connection()
    respond, calls = utility_responses(document={**authorization_document(), "RetailCustomerId": "retail-2"})
    monkeypatch.setattr(gb, "request_bytes", respond)
    with pytest.raises(ValueError, match="mapping changed"):
        app.sync_utility_connection(ACCOUNT, connection["id"])
    assert RESOURCE not in [url for url, _ in calls]


def test_generic_feed_form_cannot_inject_green_button_connection(utility):
    with pytest.raises(ValueError, match="sign-in"):
        app.save_utility_connection(ACCOUNT, {"access_method": gb.METHOD})
    assert app.list_utility_connections(ACCOUNT) == []
