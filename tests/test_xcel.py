import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from urllib.error import HTTPError

import app
import xcel as xc
from test_app import configure_tmp_paths, customer_sign_in

REAL_TRANSPORT = xc.transport

SUB = xc.RESOURCE + "Subscription/sub1"
METER = SUB + "/UsagePoint/meter1"


def entry(url, body, links=""):
    return f'<entry><link rel="self" href="{url}"/>{links}<content>{body}</content></entry>'


def feed(*items):
    return ('<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(items) + '</feed>').encode()


def usage(kind="0", meter=METER):
    return feed(entry(meter, f'<UsagePoint xmlns="http://naesb.org/espi"><ServiceCategory><kind>{kind}</kind></ServiceCategory></UsagePoint>'))


def intervals(value="250", quality="0", duration="900", multiplier="0", meter=METER):
    rt = xc.RESOURCE + "ReadingType/type1"
    mr = meter + "/MeterReading/read1"
    return feed(entry(rt, f'<ReadingType xmlns="http://naesb.org/espi"><uom>72</uom><kind>12</kind><flowDirection>1</flowDirection><accumulationBehaviour>4</accumulationBehaviour><powerOfTenMultiplier>{multiplier}</powerOfTenMultiplier></ReadingType>'),
        entry(mr, '<MeterReading xmlns="http://naesb.org/espi"/>', f'<link rel="related" href="{rt}"/>'),
        entry(mr + '/IntervalBlock/1', f'<IntervalBlock xmlns="http://naesb.org/espi"><IntervalReading><timePeriod><start>1750000000</start><duration>{duration}</duration></timePeriod><value>{value}</value><ReadingQuality><quality>{quality}</quality></ReadingQuality></IntervalReading></IntervalBlock>'))


def token():
    return {"token_type": "Bearer", "access_token": "secret-access", "refresh_token": "secret-refresh", "expires_in": 3600,
            "resourceURI": xc.RESOURCE + "Batch/Subscription/sub1", "authorizationURI": xc.RESOURCE + "Authorization/sub1"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    for key, value in {"ENABLED": "true", "CLIENT_ID": "test-xcel", "CLIENT_SECRET": "client-secret",
                       "REDIRECT_URI": "https://app.homeenergywatch.com" + xc.CALLBACK}.items():
        monkeypatch.setenv("POWER_XCEL_" + key, value)
    user = app.create_customer_user("owner@example.com", "Owner", "customer-password-123")
    app.save_account_profile("123456", display_name="Home", energy_company="Xcel Energy")
    app.add_account_access_email("123456", "owner@example.com", full_name="Owner", access_level="Manager")
    app.grant_account_data_authorization("123456", int(user["id"]))
    calls = []

    def transport(url, **kwargs):
        calls.append((url, kwargs))
        if kwargs.get("form"):
            return json.dumps(token()).encode()
        if url == SUB + "/UsagePoint":
            return usage()
        if url == xc.RESOURCE + "Batch/" + METER[len(xc.RESOURCE):]:
            return intervals()
        raise AssertionError("Unexpected URL: " + url)

    monkeypatch.setattr(xc, "transport", transport)
    monkeypatch.setattr(xc, "start_queue_worker", lambda api: None)  # Tests run the queue directly.
    client = app.web_app.test_client()
    customer_sign_in(client)
    return client, calls, user


def start(client):
    response = client.post("/utility-connection/xcel/start", data={"account_number": "123456"})
    assert response.status_code == 302
    assert response.location.startswith(xc.BASE + "oauth/authorize?")
    params = parse_qs(urlsplit(response.location).query)
    assert params["scope"] == [xc.SCOPE]
    return params["state"][0]


def complete(client):
    state = start(client)
    result = client.get(xc.CALLBACK, query_string={"state": state, "code": "one-time-code"})
    assert result.status_code == 302
    assert result.location.endswith("/xcel/meters")
    assert not app.list_utility_connections("123456")
    result = client.post("/utility-connection/xcel/meters", data={"meters": [METER], "confirmed": "yes"})
    assert result.status_code == 302
    return app.list_utility_connections("123456")[0]


def test_connect_select_sync_idempotency(setup):
    client, calls, _ = setup
    assert b"Connect Xcel" in client.get("/customer/utility?account_number=123456").data
    state = start(client)
    result = client.get(xc.CALLBACK, query_string={"state": state, "code": "code"})
    assert "no-store" in result.headers["Cache-Control"]
    assert result.headers["Referrer-Policy"] == "no-referrer"
    assert not app.list_utility_connections("123456")
    page = client.get(result.location)
    assert page.status_code == 200
    assert b"Choose the electric meters" in page.data
    assert b"secret-access" not in page.data
    with app.get_db_connection() as conn:
        sealed = conn.execute("SELECT pending_secret FROM xcel_oauth_states").fetchone()["pending_secret"]
        assert "secret-access" not in sealed
    client.post("/utility-connection/xcel/meters", data={"meters": [METER], "confirmed": "yes"})
    connection = app.list_utility_connections("123456")[0]
    assert app.sync_utility_connection("123456", connection["id"])["added_count"] == 1
    assert app.sync_utility_connection("123456", connection["id"])["already_present_count"] == 1
    with app.get_db_connection() as conn:
        raw = dict(conn.execute("SELECT * FROM utility_connections WHERE id = ?", (connection["id"],)).fetchone())
        assert "secret-access" not in json.dumps(raw)
        assert conn.execute("SELECT pending_secret FROM xcel_oauth_states").fetchone()["pending_secret"] is None
    assert all("RetailCustomer" not in url for url, _ in calls)


def test_disabled_blocks_all_entry_points(setup, monkeypatch):
    client, calls, _ = setup
    connection = complete(client)
    calls.clear()
    monkeypatch.setenv("POWER_XCEL_ENABLED", "false")
    client.post("/utility-connection/xcel/start", data={"account_number": "123456"})
    client.get(xc.CALLBACK, query_string={"code": "code", "state": "s" * 43})
    assert client.post(xc.NOTIFICATION).status_code == 415
    assert b"Automatic updates are being prepared" in client.get("/customer/utility?account_number=123456").data
    with pytest.raises(xc.ConnectionError):
        app.sync_utility_connection("123456", connection["id"])
    app.run_scheduled_utility_sync()
    assert not calls


def test_state_denial_replay_cross_browser(setup):
    client, calls, _ = setup
    state = start(client)
    other = app.web_app.test_client()
    customer_sign_in(other)
    other.get(xc.CALLBACK, query_string={"state": state, "code": "code"})
    assert not calls
    client.get(xc.CALLBACK, query_string={"state": state, "error": "access_denied"})
    client.get(xc.CALLBACK, query_string={"state": state, "code": "replay"})
    assert not calls
    state = start(client)
    with app.get_db_connection() as conn:
        conn.execute("UPDATE xcel_oauth_states SET expires_at = 1")
        conn.commit()
    client.get(xc.CALLBACK, query_string={"state": state, "code": "expired"})
    assert not calls


def test_selection_rejects_tampering_and_unconfirmed(setup):
    client, calls, _ = setup
    state = start(client)
    client.get(xc.CALLBACK, query_string={"state": state, "code": "code"})
    for form in ({"meters": [METER]}, {"meters": [METER + "wrong"], "confirmed": "yes"},
                 {"meters": [METER, METER], "confirmed": "yes"}):
        client.post("/utility-connection/xcel/meters", data=form)
        assert not app.list_utility_connections("123456")


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_csrf_and_notification_has_no_network_or_connection_effect(setup, monkeypatch, enabled):
    client, calls, _ = setup
    monkeypatch.setenv("POWER_XCEL_ENABLED", enabled)
    if enabled == "false":
        monkeypatch.delenv("POWER_XCEL_CLIENT_ID")
        monkeypatch.delenv("POWER_XCEL_CLIENT_SECRET")
    app.web_app.config["CSRF_ENFORCE_TESTS"] = True
    try:
        assert client.post("/utility-connection/xcel/start", data={"account_number": "123456"}).status_code == 400
        body = f'<BatchList xmlns="http://naesb.org/espi"><resources>{xc.RESOURCE}Batch/Subscription/sub1</resources></BatchList>'
        anonymous = app.web_app.test_client()
        response = anonymous.post(xc.NOTIFICATION, data=body, content_type="application/atom+xml")
        assert response.status_code == 202
        assert response.json == {"status": "accepted"}
        assert anonymous.post(xc.NOTIFICATION, data=body, content_type="application/atom+xml").json == response.json
        assert client.post(xc.NOTIFICATION, data="<broken", content_type="application/xml").status_code == 400
        assert client.post(xc.NOTIFICATION, data='<!DOCTYPE foo [<!ENTITY e SYSTEM "file:///etc/passwd">]><foo>&e;</foo>', content_type="application/xml").status_code == 400
        assert client.post(xc.NOTIFICATION, data=body.replace(xc.RESOURCE, "https://evil.example/"), content_type="application/atom+xml").status_code == 400
        assert client.post(xc.NOTIFICATION, data="x" * 65537, content_type="application/xml").status_code == 413
        assert client.post(xc.NOTIFICATION, json={}).status_code == 415
        assert not calls
        assert not app.list_utility_connections("123456")
        with app.get_db_connection() as conn:
            for table in ("xcel_oauth_states", "xcel_meter_bindings", "xcel_sync_locks"):
                assert conn.execute("SELECT COUNT(*) AS n FROM " + table).fetchone()["n"] == 0
    finally:
        app.web_app.config["CSRF_ENFORCE_TESTS"] = False


@pytest.mark.parametrize("url", ["http://xe.planetecosystems.com/x", xc.RESOURCE + "../secret", xc.RESOURCE + "%2e%2e/secret", xc.RESOURCE + "Subscription/a?x=y", xc.RESOURCE + "Subscription/a#frag", "https://xe.planetecosystems.com@evil.example/x"])
def test_reject_unsafe_addresses(url):
    with pytest.raises(xc.ConnectionError):
        xc.resource_url(url)


@pytest.mark.parametrize("args", [{"value": "nan"}, {"value": "-1"}, {"duration": "0"}, {"multiplier": "99"}, {"meter": SUB + "/UsagePoint/other"}])
def test_invalid_or_cross_meter_intervals(args):
    with pytest.raises(xc.ConnectionError):
        xc.meter_intervals(intervals(**args), METER)


def test_units_quality_and_entities():
    assert xc.meter_intervals(intervals(multiplier="3"), METER) == {(1750000000, 900): 250000}
    assert xc.meter_intervals(intervals(quality="7"), METER) == {}
    with pytest.raises(xc.ConnectionError):
        xc.xml_root(b'<!DOCTYPE foo [<!ENTITY e SYSTEM "file:///etc/passwd">]><foo>&e;</foo>')
    with pytest.raises(xc.ConnectionError):
        xc.entries(b'<feed xmlns="http://www.w3.org/2005/Atom"><link rel="next" href="https://evil.example"/></feed>')


def test_refresh_rotation_and_revocation(setup, monkeypatch):
    client, calls, _ = setup
    connection = complete(client)
    loaded = app.load_utility_connection_for_sync("123456", connection["id"])
    payload = json.loads(loaded["access_secret"])
    payload["expires_at"] = 0
    with app.get_db_connection() as conn:
        conn.execute("UPDATE utility_connections SET secret_token = ? WHERE id = ?", (app.seal_secret_value(json.dumps(payload)), connection["id"]))
        conn.commit()
    original = xc.transport
    def rotated(url, **kwargs):
        if kwargs.get("form"):
            data = token()
            data["refresh_token"] = "rotated-refresh"
            return json.dumps(data).encode()
        return original(url, **kwargs)
    monkeypatch.setattr(xc, "transport", rotated)
    app.sync_utility_connection("123456", connection["id"])
    assert "rotated-refresh" in app.load_utility_connection_for_sync("123456", connection["id"])["access_secret"]
    def revoked(*args, **kwargs):
        raise xc.AuthorizationExpired("Revoked")
    monkeypatch.setattr(xc, "transport", revoked)
    with pytest.raises(xc.AuthorizationExpired):
        app.sync_utility_connection("123456", connection["id"])
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT secret_token FROM utility_connections WHERE id = ?", (connection["id"],)).fetchone()["secret_token"] is None


def test_reject_generic_secret_form(setup):
    with pytest.raises(ValueError, match="Xcel sign-in"):
        app.save_utility_connection("123456", {"access_method": xc.METHOD, "access_secret": "secret"})


def test_permissions_withdrawn_before_selection_and_sync(setup):
    client, calls, user = setup
    state = start(client)
    client.get(xc.CALLBACK, query_string={"state": state, "code": "code"})
    app.revoke_account_data_authorization("123456", int(user["id"]))
    client.post("/utility-connection/xcel/meters", data={"meters": [METER], "confirmed": "yes"})
    assert not app.list_utility_connections("123456")
    app.grant_account_data_authorization("123456", int(user["id"]))
    connection = complete(client)
    app.revoke_account_data_authorization("123456", int(user["id"]))
    calls.clear()
    with pytest.raises(ValueError):
        app.sync_utility_connection("123456", connection["id"])
    assert not calls


def test_lease_blocks_competing_sync(setup):
    client, calls, _ = setup
    connection = complete(client)
    calls.clear()
    with app.get_db_connection() as conn:
        conn.execute("INSERT INTO xcel_sync_locks VALUES (?, ?, ?)", (connection["id"], "another-worker", int(time.time()) + 600))
        conn.commit()
    with pytest.raises(xc.ConnectionError, match="already syncing"):
        app.sync_utility_connection("123456", connection["id"])
    assert not calls


def test_meter_binding_collision_rolls_back(setup):
    client, _, user = setup
    complete(client)
    app.save_account_profile("another", energy_company="Xcel Energy")
    app.add_account_access_email("another", "owner@example.com", full_name="Owner", access_level="Manager")
    app.grant_account_data_authorization("another", int(user["id"]))
    existing = app.list_utility_connections("123456")[0]
    payload = json.loads(app.load_utility_connection_for_sync("123456", existing["id"])["access_secret"])
    with pytest.raises(xc.ConnectionError, match="already linked"):
        xc.save_connection(app, app.find_account("another"), payload)
    assert not app.list_utility_connections("another")


def test_token_rejects_changed_subscription_and_bad_credentials(setup, monkeypatch):
    previous = xc.exchange({"grant_type": "authorization_code", "code": "code"})
    for changed in ({"resourceURI": xc.RESOURCE + "Batch/Subscription/other"},
                    {"access_token": "token\r\ninjection"}, {"expires_in": -1},
                    {"authorizationURI": "https://evil.example/"}):
        monkeypatch.setattr(xc, "transport", lambda *a, **kw: json.dumps({**token(), **changed}).encode())
        with pytest.raises(xc.ConnectionError):
            xc.exchange({"grant_type": "refresh_token", "refresh_token": "refresh"}, previous)


def test_transport_revocation_redirects_and_disabled_guard(setup, monkeypatch):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 410, "gone", {}, None)
    monkeypatch.setattr(xc, "build_opener", lambda handler: Opener())
    with pytest.raises(xc.AuthorizationExpired):
        REAL_TRANSPORT(METER, token="access")
    assert len(calls) == 1
    assert xc.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example") is None
    monkeypatch.setenv("POWER_XCEL_ENABLED", "false")
    with pytest.raises(xc.ConnectionError):
        REAL_TRANSPORT(METER, token="access")
    assert len(calls) == 1



# --- Registered URLs, notification queue and service check ---------------------

def notify(client, sub="sub1", query="", content_type="application/atom+xml"):
    body = f'<BatchList xmlns="http://naesb.org/espi"><resources>{xc.RESOURCE}Batch/Subscription/{sub}{query}</resources></BatchList>'
    return client.post(xc.NOTIFICATION, data=body, content_type=content_type)


def queued():
    with app.get_db_connection() as conn:
        return [r["connection_id"] for r in conn.execute("SELECT connection_id FROM xcel_fetch_queue")]


def test_registered_paths():
    assert xc.CALLBACK == "/api/integrations/xcel/callback"
    assert xc.NOTIFICATION == "/api/integrations/xcel/notifications"


@pytest.mark.parametrize("uri,ok", [
    ("https://homeenergywatch.com/api/integrations/xcel/callback", True),
    ("https://app.homeenergywatch.com/api/integrations/xcel/callback", True),
    ("https://evil.example/api/integrations/xcel/callback", False),
    ("https://homeenergywatch.com/utility-connection/xcel/callback", False),
    ("http://homeenergywatch.com/api/integrations/xcel/callback", False),
])
def test_redirect_uri_must_be_registered_path_on_our_domain(setup, monkeypatch, uri, ok):
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    monkeypatch.setenv("POWER_XCEL_REDIRECT_URI", uri)
    assert xc.available() is ok


def test_public_domain_serves_notifications_and_forwards_callback(setup, monkeypatch):
    monkeypatch.setenv("POWER_MARKETING_HOSTS", "homeenergywatch.com")
    monkeypatch.setenv("POWER_PUBLIC_BASE_URL", "https://app.homeenergywatch.com")
    apex = app.web_app.test_client()
    body = f'<BatchList xmlns="http://naesb.org/espi"><resources>{xc.RESOURCE}Batch/Subscription/unknown</resources></BatchList>'
    response = apex.post(xc.NOTIFICATION, data=body, content_type="application/atom+xml", base_url="https://homeenergywatch.com")
    assert response.status_code == 202  # Served on the apex, not redirected.
    callback = apex.get(xc.CALLBACK + "?state=abc&code=xyz", base_url="https://homeenergywatch.com")
    assert callback.status_code == 302
    assert callback.location == "https://app.homeenergywatch.com" + xc.CALLBACK + "?state=abc&code=xyz"


def test_notification_queues_fetch_built_from_our_own_bindings(setup):
    client, calls, _ = setup
    connection = complete(client)
    calls.clear()
    response = notify(app.web_app.test_client(), query="?published-min=2026-01-01T00:00:00Z&amp;published-max=2026-01-02T00:00:00Z")
    assert response.status_code == 202 and response.json == {"status": "accepted"}
    assert queued() == [connection["id"]]
    assert not calls  # Nothing is fetched while answering Xcel.
    result = xc.process_fetch_queue(app)
    assert result == {"processed": 1, "failed": 0}
    assert queued() == []
    fetched = [url for url, _ in calls if not _.get("form")]
    assert xc.RESOURCE + "Batch/" + METER[len(xc.RESOURCE):] in fetched
    assert all("published-" not in url and "evil" not in url for url in fetched)
    assert all(kw.get("token") == "secret-access" for url, kw in calls if not kw.get("form"))
    assert len(app.load_intervals_from_db("123456")) == 1


def test_unknown_or_disabled_notifications_queue_nothing(setup, monkeypatch):
    client, calls, _ = setup
    complete(client)
    notify(app.web_app.test_client(), sub="someone-else")
    assert queued() == []
    monkeypatch.setenv("POWER_XCEL_ENABLED", "false")
    notify(app.web_app.test_client())
    assert queued() == []
    assert xc.process_fetch_queue(app) == {"processed": 0, "failed": 0}


def test_repeated_notifications_collapse_to_one_fetch(setup):
    client, calls, _ = setup
    connection = complete(client)
    for _ in range(5):
        notify(app.web_app.test_client())
    assert queued() == [connection["id"]]


def test_revoked_connection_is_not_queued(setup):
    client, _, user = setup
    complete(client)
    app.revoke_account_data_authorization("123456", int(user["id"]))
    notify(app.web_app.test_client())
    assert queued() == []


def test_service_check_uses_client_credentials_only(setup, monkeypatch):
    monkeypatch.setenv("POWER_XCEL_ENABLED", "false")  # Works before customer collection is switched on.
    seen = []
    app_info = xc.RESOURCE + "ApplicationInformation/app42"

    def transport(url, **kwargs):
        seen.append((url, kwargs))
        if kwargs.get("form"):
            return json.dumps({"access_token": "client-token", "token_type": "Bearer", "expires_in": 86400,
                               "scope": "FB=34 35", "resourceURI": app_info}).encode()
        if url == app_info:
            return (b'<entry xmlns="http://www.w3.org/2005/Atom"><content><ApplicationInformation xmlns="http://naesb.org/espi">'
                    b'<dataCustodianApplicationStatus>2</dataCustodianApplicationStatus>'
                    b'<redirect_uri>https://homeenergywatch.com/api/integrations/xcel/callback</redirect_uri>'
                    b'<thirdPartyNotifyUri>https://homeenergywatch.com/api/integrations/xcel/notifications</thirdPartyNotifyUri>'
                    b'</ApplicationInformation></content></entry>')
        if url == xc.RESOURCE + "ReadServiceStatus":
            return b'<ServiceStatus xmlns="http://naesb.org/espi"><currentStatus>1</currentStatus></ServiceStatus>'
        raise AssertionError(url)
    monkeypatch.setattr(xc, "transport", transport)
    result = xc.check_service()
    assert seen[0][1]["form"] == {"grant_type": "client_credentials", "scope": "FB=34 35"}
    assert all(kw.get("service_check") for _, kw in seen)
    assert result["service_status"] == "1"
    assert result["application_information"] == "app42"
    assert result["registered_notification_uri"].endswith(xc.NOTIFICATION)


def test_service_check_rejects_foreign_application_address(setup, monkeypatch):
    monkeypatch.setattr(xc, "transport", lambda url, **kw: json.dumps({
        "access_token": "t", "token_type": "Bearer", "resourceURI": "https://evil.example/ApplicationInformation/1"}).encode())
    with pytest.raises(xc.ConnectionError):
        xc.check_service()


def test_real_transport_allows_service_check_without_collection_switch(monkeypatch):
    monkeypatch.setenv("POWER_XCEL_ENABLED", "false")
    for key in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        monkeypatch.delenv("POWER_XCEL_" + key, raising=False)
    with pytest.raises(xc.ConnectionError):
        REAL_TRANSPORT(xc.RESOURCE + "ReadServiceStatus", token="t", service_check=True)
    with pytest.raises(xc.ConnectionError):
        REAL_TRANSPORT(xc.RESOURCE + "ReadServiceStatus", token="t")
