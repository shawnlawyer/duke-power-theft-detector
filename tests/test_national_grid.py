import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest

import app
import national_grid as ng
from test_app import configure_tmp_paths, customer_sign_in


SUB = ng.RESOURCE + "Subscription/auth1"
RETAIL = ng.RESOURCE + "RetailCustomer/auth1"
METER = SUB + "/UsagePoint/electric1"
ACCOUNT = RETAIL + "/Customer/person1/CustomerAccount/account1"


def entry(url, body, links=""):
    return f'<entry><link rel="self" href="{url}"/>{links}<content>{body}</content></entry>'


def feed(*items):
    return ('<feed xmlns="http://www.w3.org/2005/Atom">' + ''.join(items) + '</feed>').encode()


def retail(number="123456"):
    return feed(entry(ACCOUNT, f'<CustomerAccount xmlns="urn:retail"><accountId>{number}</accountId></CustomerAccount>'),
                entry(ACCOUNT + '/CustomerAgreement/electric1', '<CustomerAgreement xmlns="urn:retail"><agreementId>electric1</agreementId></CustomerAgreement>'))


def usage(kind="0"):
    return feed(entry(METER, f'<UsagePoint xmlns="http://naesb.org/espi"><ServiceCategory><kind>{kind}</kind></ServiceCategory></UsagePoint>'))


def intervals(value="250", multiplier="0", quality="0", duration="900", flow="1"):
    rt = ng.RESOURCE + "ReadingType/type1"
    mr = METER + "/MeterReading/reading1"
    return feed(
        entry(rt, f'<ReadingType xmlns="http://naesb.org/espi"><uom>72</uom><kind>12</kind><flowDirection>{flow}</flowDirection><powerOfTenMultiplier>{multiplier}</powerOfTenMultiplier><accumulationBehaviour>4</accumulationBehaviour></ReadingType>'),
        entry(mr, '<MeterReading xmlns="http://naesb.org/espi"/>', f'<link rel="related" href="{rt}"/>'),
        entry(mr + '/IntervalBlock/1', f'<IntervalBlock xmlns="http://naesb.org/espi"><IntervalReading><timePeriod><start>1750000000</start><duration>{duration}</duration></timePeriod><value>{value}</value><ReadingQuality><quality>{quality}</quality></ReadingQuality></IntervalReading></IntervalBlock>'))


def token():
    return {"token_type": "Bearer", "access_token": "secret-access", "refresh_token": "secret-refresh", "expires_in": 3600,
            "resourceURI": SUB, "customerResourceURI": RETAIL, "expires_at": time.time() + 3600}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    for key, value in {"ENABLED": "true", "CLIENT_ID": "NATIONALGRID_test", "CLIENT_SECRET": "client-secret",
                       "AUTHORIZE_URL": "https://greenbuttonconnect.nationalgrid.com/oauth/authorize",
                       "TOKEN_URL": "https://greenbuttonconnect.nationalgrid.com/oauth/token",
                       "REDIRECT_URI": "https://app.homeenergywatch.com" + ng.CALLBACK}.items():
        monkeypatch.setenv("POWER_NATIONAL_GRID_" + key, value)
    user = app.create_customer_user("owner@example.com", "Owner", "customer-password-123")
    app.save_account_profile("123456", display_name="Home", energy_company="National Grid")
    app.add_account_access_email("123456", "owner@example.com", full_name="Owner", access_level="Manager")
    app.grant_account_data_authorization("123456", int(user["id"]))
    calls = []

    def transport(url, **kwargs):
        calls.append((url, kwargs))
        if kwargs.get("form"):
            return json.dumps(token()).encode()
        if url == ng.batch(RETAIL):
            return retail()
        if url == SUB + "/UsagePoint":
            return usage()
        if url == ng.batch(METER):
            return intervals()
        raise AssertionError("Unexpected endpoint: " + url)

    monkeypatch.setattr(ng, "transport", transport)
    client = app.web_app.test_client()
    customer_sign_in(client)
    return client, calls, user


def start(client):
    response = client.post("/utility-connection/national-grid/start", data={"account_number": "123456"})
    assert response.status_code == 302
    assert response.location.startswith("https://greenbuttonconnect.nationalgrid.com/oauth/authorize?")
    return parse_qs(urlsplit(response.location).query)["state"][0]


def complete(client):
    state = start(client)
    return client.get(ng.CALLBACK, query_string={"state": state, "code": "one-time-code"})


def test_connect_sync_and_repeated_import(setup):
    client, calls, _ = setup
    page = client.get("/customer/utility?account_number=123456")
    assert b"Connect National Grid" in page.data
    assert b'target="_blank"' in page.data
    response = complete(client)
    assert response.status_code == 302
    assert response.headers["Referrer-Policy"] == "no-referrer"
    connection = app.list_utility_connections("123456")[0]
    assert connection["access_method"] == ng.METHOD
    with app.get_db_connection() as conn:
        raw = dict(conn.execute("SELECT * FROM utility_connections WHERE id = ?", (connection["id"],)).fetchone())
    assert "secret-access" not in json.dumps(raw)
    assert "secret-refresh" not in json.dumps(raw)
    first = app.sync_utility_connection("123456", connection["id"])
    second = app.sync_utility_connection("123456", connection["id"])
    assert first["added_count"] == 1
    assert second["added_count"] == 0
    assert second["already_present_count"] == 1
    assert all("api/v2" not in url for url, _ in calls)
    assert b"secret-access" not in client.get(response.location).data


def test_callback_state_and_replay(setup):
    client, calls, _ = setup
    state = start(client)
    client.get(ng.CALLBACK, query_string={"state": "x" * 43, "code": "bad"})
    assert not calls
    client.get(ng.CALLBACK, query_string={"state": state, "code": "good"})
    count = len(calls)
    client.get(ng.CALLBACK, query_string={"state": state, "code": "replay"})
    assert len(calls) == count


def test_denied_does_not_exchange(setup):
    client, calls, _ = setup
    state = start(client)
    client.get(ng.CALLBACK, query_string={"state": state, "error": "access_denied"})
    assert not calls
    assert not app.list_utility_connections("123456")


def test_expired_and_cross_browser(setup):
    client, calls, _ = setup
    state = start(client)
    other = app.web_app.test_client()
    customer_sign_in(other)
    other.get(ng.CALLBACK, query_string={"state": state, "code": "code"})
    assert not calls
    with app.get_db_connection() as conn:
        conn.execute("UPDATE national_grid_oauth_states SET expires_at = 0")
        conn.commit()
    client.get(ng.CALLBACK, query_string={"state": state, "code": "code"})
    assert not calls


def test_disabled_and_missing_consent(setup, monkeypatch):
    client, calls, user = setup
    monkeypatch.setenv("POWER_NATIONAL_GRID_ENABLED", "false")
    response = client.post("/utility-connection/national-grid/start", data={"account_number": "123456"})
    assert "greenbuttonconnect" not in response.location
    monkeypatch.setenv("POWER_NATIONAL_GRID_ENABLED", "true")
    app.revoke_account_data_authorization("123456", user["id"])
    response = client.post("/utility-connection/national-grid/start", data={"account_number": "123456"})
    assert "greenbuttonconnect" not in response.location
    assert not calls


def test_wrong_account_and_gas_not_saved(setup, monkeypatch):
    client, calls, _ = setup
    original = ng.transport
    monkeypatch.setattr(ng, "transport", lambda url, **kw: retail("someone-else") if url == ng.batch(RETAIL) else original(url, **kw))
    complete(client)
    assert not app.list_utility_connections("123456")
    monkeypatch.setattr(ng, "transport", lambda url, **kw: usage("1") if url == SUB + "/UsagePoint" else original(url, **kw))
    complete(client)
    assert not app.list_utility_connections("123456")


def test_refresh_and_withdrawal(setup):
    client, calls, user = setup
    complete(client)
    connection = app.list_utility_connections("123456")[0]
    saved = app.load_utility_connection_for_sync("123456", connection["id"])
    payload = json.loads(saved["access_secret"])
    payload["expires_at"] = 0
    ng.save_connection(app, app.find_account("123456"), payload)
    app.sync_utility_connection("123456", connection["id"])
    assert any(kw.get("form", {}).get("grant_type") == "refresh_token" for _, kw in calls)
    app.revoke_account_data_authorization("123456", user["id"])
    before = len(calls)
    with pytest.raises(ValueError, match="authorization"):
        app.sync_utility_connection("123456", connection["id"])
    assert len(calls) == before


def test_provider_revoked_clears_secret(setup, monkeypatch):
    client, _, _ = setup
    complete(client)
    connection = app.list_utility_connections("123456")[0]
    def revoked(*a, **kw):
        raise ng.AuthorizationExpired("National Grid access has ended.")
    monkeypatch.setattr(ng, "transport", revoked)
    with pytest.raises(ng.AuthorizationExpired):
        app.sync_utility_connection("123456", connection["id"])
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT secret_token FROM utility_connections WHERE id = ?", (connection["id"],)).fetchone()["secret_token"] is None


@pytest.mark.parametrize("url", ["http://utilityapi.com/x", "https://evil.test/x", "https://utilityapi.com@evil.test/x",
                                ng.RESOURCE + "../secret", ng.RESOURCE + "Subscription/a?key=x", ng.RESOURCE + "Subscription/%2e%2e"])
def test_reject_unsafe_resource_urls(url):
    with pytest.raises(ng.ConnectionError):
        ng.resource_url(url)


def test_xml_units_quality_and_untrusted_data():
    assert ng.meter_intervals(intervals("0.25", "3"), METER) == {(1750000000, 900): 250}
    assert ng.meter_intervals(intervals(quality="8"), METER) == {}
    assert ng.meter_intervals(intervals(flow="19"), METER) == {}
    for data in (intervals("NaN"), intervals(duration="0"), intervals(duration="86400"), b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><feed xmlns="http://www.w3.org/2005/Atom"/>'):
        with pytest.raises(ng.ConnectionError):
            ng.meter_intervals(data, METER)


def test_token_urls_and_missing_fields(setup, monkeypatch):
    monkeypatch.setattr(ng, "transport", lambda *a, **kw: json.dumps({**token(), "resourceURI": "https://evil.test/private"}).encode())
    with pytest.raises(ng.ConnectionError):
        ng.exchange({"grant_type": "authorization_code"})


def test_generic_form_cannot_forge_connector(setup):
    with pytest.raises(ValueError, match="National Grid sign-in"):
        app.save_utility_connection("123456", {"access_method": ng.METHOD, "access_secret": "fake"})


def test_csrf_required_and_viewer_cannot_connect(setup):
    client, calls, _ = setup
    app.web_app.config["CSRF_ENFORCE_TESTS"] = True
    try:
        assert client.post("/utility-connection/national-grid/start", data={"account_number": "123456"}).status_code == 400
    finally:
        app.web_app.config["CSRF_ENFORCE_TESTS"] = False
    with app.get_db_connection() as conn:
        conn.execute("UPDATE account_access_emails SET access_level = 'Viewer'")
        conn.commit()
    response = client.post("/utility-connection/national-grid/start", data={"account_number": "123456"})
    assert "greenbuttonconnect" not in response.location
    assert not calls


def test_withdrawal_during_fetch_never_imports(setup, monkeypatch):
    client, _, user = setup
    complete(client)
    connection = app.list_utility_connections("123456")[0]
    original = ng.transport
    def withdraw(url, **kw):
        data = original(url, **kw)
        if url == ng.batch(METER):
            app.revoke_account_data_authorization("123456", user["id"])
        return data
    monkeypatch.setattr(ng, "transport", withdraw)
    with pytest.raises(ng.AuthorizationExpired):
        app.sync_utility_connection("123456", connection["id"])
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM interval_readings").fetchone()["n"] == 0


def test_reconnect_during_fetch_preserves_new_connection(setup, monkeypatch):
    client, _, _ = setup
    complete(client)
    connection = app.list_utility_connections("123456")[0]
    original = ng.transport
    new = {**token(), "access_token": "replacement-token", "meters": [METER]}
    def replace(url, **kw):
        if url == ng.batch(METER):
            ng.save_connection(app, app.find_account("123456"), new)
        return original(url, **kw)
    monkeypatch.setattr(ng, "transport", replace)
    with pytest.raises(ValueError, match="changed before import"):
        app.sync_utility_connection("123456", connection["id"])
    stored = app.load_utility_connection_for_sync("123456", connection["id"])
    assert "replacement-token" in stored["access_secret"]


def test_sync_lease_blocks_second_worker(setup):
    client, calls, _ = setup
    complete(client)
    connection = app.list_utility_connections("123456")[0]
    with app.get_db_connection() as conn:
        conn.execute("INSERT INTO national_grid_sync_locks VALUES (?, ?, ?)", (connection["id"], "other-worker", int(time.time()) + 300))
        conn.commit()
    before = len(calls)
    with pytest.raises(ng.ConnectionError, match="already syncing"):
        app.sync_utility_connection("123456", connection["id"])
    assert len(calls) == before


def test_conflicting_history_is_not_overwritten(setup, monkeypatch):
    client, _, _ = setup
    complete(client)
    connection = app.list_utility_connections("123456")[0]
    app.sync_utility_connection("123456", connection["id"])
    original = ng.transport
    monkeypatch.setattr(ng, "transport", lambda url, **kw: intervals("999") if url == ng.batch(METER) else original(url, **kw))
    result = app.sync_utility_connection("123456", connection["id"])
    assert result["conflicts_skipped_count"] == 1
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT wh FROM interval_readings").fetchone()["wh"] == 250


def test_multiple_accounts_and_gas_are_not_combined(monkeypatch):
    other = RETAIL + "/Customer/person1/CustomerAccount/account2"
    account_feed = retail().decode().replace('</feed>', entry(other, '<CustomerAccount xmlns="urn:retail"><accountId>987654</accountId></CustomerAccount>') + entry(other + '/CustomerAgreement/electric2', '<CustomerAgreement xmlns="urn:retail"/>') + '</feed>')
    point_feed = usage().decode().replace('</feed>', entry(SUB + '/UsagePoint/electric2', '<UsagePoint xmlns="http://naesb.org/espi"><ServiceCategory><kind>0</kind></ServiceCategory></UsagePoint>') + entry(SUB + '/UsagePoint/gas1', '<UsagePoint xmlns="http://naesb.org/espi"><ServiceCategory><kind>1</kind></ServiceCategory></UsagePoint>') + '</feed>')
    monkeypatch.setattr(ng, "transport", lambda url, **kw: account_feed.encode() if url == ng.batch(RETAIL) else point_feed.encode())
    assert ng.matched_usage_points(token(), "123456") == [METER]
    assert ng.matched_usage_points(token(), "987654") == [SUB + '/UsagePoint/electric2']


def test_http_errors_are_redacted(monkeypatch, setup):
    from urllib.error import HTTPError
    class Opener:
        def open(self, req, timeout):
            raise HTTPError(req.full_url, 500, 'secret-customer-token', {}, None)
    monkeypatch.setattr(ng, "build_opener", lambda *a: Opener())
    # setup replaces transport; invoke the real function retained below.
    with pytest.raises(ng.ConnectionError) as failure:
        REAL_TRANSPORT(SUB, token="sensitive")
    assert "secret-customer-token" not in str(failure.value)
    assert "sensitive" not in str(failure.value)


REAL_TRANSPORT = ng.transport
