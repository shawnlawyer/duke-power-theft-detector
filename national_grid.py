"""National Grid's utility-hosted Green Button OAuth connector.

No generic UtilityAPI collection/purchase endpoints are used. Credentials remain
server-side; provider URLs and Atom links are validated before authenticated I/O.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import flash, redirect, request, session, url_for
from lxml import etree
import pandas as pd

METHOD = "national_grid_oauth"
CALLBACK = "/utility-connection/national-grid/callback"
RESOURCE = "https://utilityapi.com/DataCustodian/espi/1_1/resource/"
NS = {"a": "http://www.w3.org/2005/Atom", "e": "http://naesb.org/espi"}
LIMIT = 25 * 1024 * 1024


class ConnectionError(ValueError):
    pass


class AuthorizationExpired(ConnectionError):
    pass


def config():
    values = {k: os.getenv("POWER_NATIONAL_GRID_" + k, "").strip() for k in
              ("CLIENT_ID", "CLIENT_SECRET", "AUTHORIZE_URL", "TOKEN_URL", "REDIRECT_URI")}
    if os.getenv("POWER_NATIONAL_GRID_ENABLED", "").lower() != "true" or not all(values.values()):
        raise ConnectionError("National Grid automatic updates are not available yet. You can still upload your history.")
    if not values["CLIENT_ID"].startswith("NATIONALGRID_"):
        raise ConnectionError("National Grid connection settings need attention.")
    for key, suffix in (("AUTHORIZE_URL", "/oauth/authorize"), ("TOKEN_URL", "/oauth/token")):
        p = urlsplit(values[key])
        if p.scheme != "https" or p.netloc != "greenbuttonconnect.nationalgrid.com" or p.path != suffix or p.query or p.fragment:
            raise ConnectionError("National Grid connection settings need attention.")
    p = urlsplit(values["REDIRECT_URI"])
    public = urlsplit(os.getenv("POWER_PUBLIC_BASE_URL", ""))
    if p.scheme != "https" or not p.netloc or p.username or p.netloc != public.netloc or p.path != CALLBACK or p.query or p.fragment:
        raise ConnectionError("National Grid return address needs attention.")
    return values


def available():
    try:
        config()
        return True
    except ConnectionError:
        return False


def provider_matches(name):
    return "national grid" in str(name or "").lower()


def resource_url(value):
    p = urlsplit(str(value))
    if (p.scheme != "https" or p.netloc != "utilityapi.com" or p.query or p.fragment
            or not p.path.startswith("/DataCustodian/espi/1_1/resource/")
            or not re.fullmatch(r"[A-Za-z0-9_/-]+", p.path)
            or "//" in p.path):
        raise ConnectionError("National Grid returned an unexpected data address.")
    return str(value)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(url, *, token=None, form=None, client=None):
    headers = {"Accept": "application/json" if form else "application/atom+xml,application/xml"}
    if form:
        credentials = base64.b64encode((client["CLIENT_ID"] + ":" + client["CLIENT_SECRET"]).encode()).decode()
        headers.update({"Authorization": "Basic " + credentials, "Content-Type": "application/x-www-form-urlencoded"})
    else:
        resource_url(url)
        headers["Authorization"] = "Bearer " + token
    try:
        with build_opener(NoRedirect()).open(Request(url, data=urlencode(form).encode() if form else None, headers=headers), timeout=30) as response:
            data = response.read(LIMIT + 1)
    except HTTPError as exc:
        if exc.code in (401, 403) or (form and exc.code == 400):
            raise AuthorizationExpired("National Grid access has ended or needs renewal. Connect again.") from None
        raise ConnectionError("National Grid could not complete the request. Please try again later.") from None
    except (URLError, TimeoutError, OSError):
        raise ConnectionError("National Grid is unavailable. Please try again later.") from None
    if len(data) > LIMIT:
        raise ConnectionError("National Grid returned more data than can be processed safely.")
    return data


def exchange(form, previous=None):
    cfg = config()
    raw = transport(cfg["TOKEN_URL"], form=form, client=cfg)
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        payload = {**(previous or {}), **result}
        if (str(payload.get("token_type", "")).lower() != "bearer"
                or not isinstance(payload.get("access_token"), str)
                or not payload["access_token"] or not payload.get("refresh_token")):
            raise ValueError()
        lifetime = int(result["expires_in"])
        for name in ("access_token", "refresh_token"):
            if not isinstance(payload.get(name), str) or not re.fullmatch(r"[!-~]{1,8192}", payload[name]):
                raise ValueError()
        if not 0 < lifetime <= 86400 * 30:
            raise ValueError()
        for key, kind in (("resourceURI", "Subscription"), ("customerResourceURI", "RetailCustomer")):
            value = resource_url(payload[key])
            if not re.fullmatch(re.escape(RESOURCE) + kind + r"/[A-Za-z0-9_-]+", value):
                raise ValueError()
            if previous and value != previous[key]:
                raise ValueError()
        payload["expires_at"] = time.time() + lifetime
        return payload
    except (ValueError, KeyError, TypeError):
        raise ConnectionError("National Grid did not return a usable connection. Please connect again.") from None


def entries(data):
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
        if root.getroottree().docinfo.doctype:
            raise ValueError()
        if root.tag not in ("{" + NS["a"] + "}feed", "{" + NS["a"] + "}entry"):
            raise ValueError()
        if root.xpath("a:link[@rel='next']", namespaces=NS):
            raise ConnectionError("National Grid returned paginated data. No partial history was imported.")
        return [root] if root.tag.endswith("}entry") else root.findall("a:entry", NS)
    except (etree.XMLSyntaxError, ValueError):
        raise ConnectionError("National Grid returned unreadable or incomplete data.") from None


def obj(entry, name):
    found = entry.xpath("a:content/*[local-name()=$name]", namespaces=NS, name=name)
    return found[0] if len(found) == 1 else None


def field(element, path, default=""):
    current = element
    for name in path.split("/"):
        if current is None:
            return default
        matches = current.xpath("./*[local-name()=$name]", name=name)
        current = matches[0] if len(matches) == 1 else None
    return (current.text or "").strip() if current is not None else default


def self_url(entry):
    links = entry.xpath("a:link[@rel='self']/@href", namespaces=NS)
    if len(links) != 1:
        raise ConnectionError("National Grid returned an ambiguous record.")
    return resource_url(links[0])


def batch(url):
    resource_url(url)
    return RESOURCE + "Batch/" + url[len(RESOURCE):]


def account_key(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()


def matched_usage_points(payload, expected):
    """Follow account->agreement->meter relationships; never use the first meter."""
    retail = entries(transport(batch(payload["customerResourceURI"]), token=payload["access_token"]))
    accounts = [self_url(e) for e in retail if obj(e, "CustomerAccount") is not None
                and account_key(field(obj(e, "CustomerAccount"), "accountId")) == account_key(expected)]
    if not account_key(expected) or len(accounts) != 1:
        raise ConnectionError("National Grid did not return exactly one matching account. Check the number on your Account page.")
    if not accounts[0].startswith(payload["customerResourceURI"] + "/Customer/"):
        raise ConnectionError("National Grid returned an unexpected account relationship.")
    agreements = set()
    for e in retail:
        agreement = obj(e, "CustomerAgreement")
        if agreement is not None and self_url(e).startswith(accounts[0] + "/CustomerAgreement/"):
            agreements.add(self_url(e).rsplit("/", 1)[-1])
            if field(agreement, "agreementId"):
                agreements.add(field(agreement, "agreementId"))
    usage = entries(transport(payload["resourceURI"] + "/UsagePoint", token=payload["access_token"]))
    selected = []
    for e in usage:
        point = obj(e, "UsagePoint")
        if point is None or field(point, "ServiceCategory/kind") != "0":
            continue
        url = self_url(e)
        if not url.startswith(payload["resourceURI"] + "/UsagePoint/"):
            raise ConnectionError("National Grid returned an unexpected meter relationship.")
        if url.rsplit("/", 1)[-1] in agreements or field(point, "ServiceDeliveryPoint/customerAgreement") in agreements:
            selected.append(url)
    if not selected or len(set(selected)) != len(selected):
        raise ConnectionError("National Grid did not return a unique electric-meter mapping for this account.")
    return sorted(selected)


def meter_intervals(data, meter_url):
    feed = entries(data)
    types = {self_url(e): obj(e, "ReadingType") for e in feed if obj(e, "ReadingType") is not None}
    readings = {}
    for e in feed:
        if obj(e, "MeterReading") is None:
            continue
        key = self_url(e)
        if not key.startswith(meter_url + "/MeterReading/"):
            raise ConnectionError("National Grid mixed meters in one data response.")
        links = e.xpath("a:link[@rel='related']/@href", namespaces=NS)
        candidates = [types[link] for link in links if link in types]
        if len(candidates) != 1:
            raise ConnectionError("National Grid did not identify the units for a reading.")
        readings[key] = candidates[0]
    channels = {}
    for e in feed:
        block = obj(e, "IntervalBlock")
        if block is None:
            continue
        key = self_url(e).split("/IntervalBlock/")[0]
        if key not in readings:
            raise ConnectionError("National Grid returned intervals without their reading type.")
        rt = readings[key]
        if field(rt, "uom") != "72" or field(rt, "kind") != "12" or field(rt, "flowDirection") != "1":
            continue  # Only forward energy, never demand, gas or reverse generation.
        if field(rt, "accumulationBehaviour") != "4":
            raise ConnectionError("Cumulative National Grid readings cannot be treated as interval usage.")
        channel = channels.setdefault(key, {})
        try:
            multiplier = int(field(rt, "powerOfTenMultiplier", "0"))
            if not -12 <= multiplier <= 12:
                raise ValueError()
            for reading in block.findall("e:IntervalReading", NS):
                quality = [field(q, "quality", (q.text or "").strip()) for q in reading.findall("e:ReadingQuality", NS)]
                if any(q not in ("0", "14", "17", "18", "19") for q in quality or [field(rt, "defaultQuality", "0")]):
                    continue  # Leave estimates/questionable readings as gaps.
                start = int(field(reading, "timePeriod/start"))
                duration = int(field(reading, "timePeriod/duration", field(rt, "intervalLength")))
                wh = float(field(reading, "value")) * 10 ** multiplier
                if start < 0 or not 0 < duration <= 3600 or not math.isfinite(wh) or wh < 0:
                    raise ValueError()
                if start + duration > time.time():
                    continue
                interval = (start, duration)
                if interval in channel and channel[interval] != wh:
                    raise ValueError()
                channel[interval] = wh
        except (ValueError, OverflowError):
            raise ConnectionError("National Grid returned invalid or conflicting interval readings.") from None
    if len(channels) > 1:
        raise ConnectionError("National Grid returned multiple consumption channels. No ambiguous readings were imported.")
    result = next(iter(channels.values()), {})
    previous_end = -1
    for start, duration in sorted(result):
        if start < previous_end:
            raise ConnectionError("National Grid returned overlapping intervals.")
        previous_end = start + duration
    return result


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS national_grid_oauth_states (
        state_hash TEXT PRIMARY KEY, account_number TEXT NOT NULL,
        customer_id BIGINT NOT NULL, browser_hash TEXT NOT NULL,
        expires_at BIGINT NOT NULL, used INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS national_grid_sync_locks (
        connection_id BIGINT PRIMARY KEY, lease TEXT NOT NULL, expires_at BIGINT NOT NULL)""")


def save_connection(api, account, payload):
    sealed = api.seal_secret_value(json.dumps(payload))
    now = api.timestamp_now()
    with api.get_db_connection() as conn:
        # Lock the parent while creating/updating to avoid duplicate connections.
        conn.execute("UPDATE accounts SET account_number = account_number WHERE id = ?", (account["id"],))
        permitted = conn.execute("SELECT 1 FROM account_data_authorizations WHERE account_id = ? AND status = 'active'", (account["id"],)).fetchone()
        if not permitted:
            raise ConnectionError("Permission was withdrawn before the connection could be saved.")
        row = conn.execute("SELECT id FROM utility_connections WHERE account_id = ? AND access_method = ?", (account["id"], METHOD)).fetchone()
        if row:
            conn.execute("""UPDATE utility_connections SET secret_token = ?, status = ?, updated_at = ?,
                last_sync_error = NULL WHERE id = ?""", (sealed, "Ready to sync", now, row["id"]))
        else:
            conn.execute("""INSERT INTO utility_connections (account_id, provider_name, connection_label,
                access_method, secret_token, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (account["id"], account["energy_company"], "National Grid automatic updates", METHOD, sealed, "Ready to sync", now, now))
        conn.commit()


def sync(api, connection):
    lease = secrets.token_urlsafe(24)
    with api.get_db_connection() as conn:
        changed = conn.execute("""INSERT INTO national_grid_sync_locks (connection_id, lease, expires_at)
            VALUES (?, ?, ?) ON CONFLICT(connection_id) DO UPDATE SET lease = excluded.lease,
            expires_at = excluded.expires_at WHERE national_grid_sync_locks.expires_at < ?""",
            (connection["id"], lease, int(time.time()) + 600, int(time.time())))
        if changed.rowcount != 1:
            raise ConnectionError("National Grid is already syncing this account. Please wait for it to finish.")
        conn.commit()
    try:
        # Reload after obtaining the cross-worker lease; a preceding sync may have rotated credentials.
        connection = api.load_utility_connection_for_sync(connection["account_number"], connection["id"])
        return _sync(api, connection)
    finally:
        with api.get_db_connection() as conn:
            conn.execute("DELETE FROM national_grid_sync_locks WHERE connection_id = ? AND lease = ?", (connection["id"], lease))
            conn.commit()


def _sync(api, connection):
    config()  # Disabling the connector also stops scheduled collections.
    try:
        payload = json.loads(connection["access_secret"])
        if payload["expires_at"] <= time.time() + 60:
            payload = exchange({"grant_type": "refresh_token", "refresh_token": payload["refresh_token"]}, payload)
            with api.get_db_connection() as conn:
                row = conn.execute("SELECT secret_token FROM utility_connections WHERE id = ?", (connection["id"],)).fetchone()
                if not row or api.unseal_secret_value(row["secret_token"]) != connection["access_secret"]:
                    raise ConnectionError("National Grid connection changed during the refresh. Try again.")
                changed = conn.execute("""UPDATE utility_connections SET secret_token = ?, updated_at = ?
                    WHERE id = ? AND access_method = ? AND secret_token = ?""",
                    (api.seal_secret_value(json.dumps(payload)), api.timestamp_now(), connection["id"], METHOD, row["secret_token"]))
                if changed.rowcount != 1:
                    raise AuthorizationExpired("National Grid access was removed during the refresh.")
                conn.commit()
            connection["access_secret"] = json.dumps(payload)
        meters = matched_usage_points(payload, connection["account_number"])
        if meters != payload["meters"]:
            raise ConnectionError("National Grid's meter list changed. Connect again to confirm the account.")
        if len(meters) > 10:
            raise ConnectionError("This account has too many meters for one safe sync. Contact support.")
        values = [meter_intervals(transport(batch(meter), token=payload["access_token"]), meter) for meter in meters]
        # Sum only intervals supplied for every meter. Missing data stays missing.
        common = set.intersection(*(set(value) for value in values))
        rows = [{"start_epoch": k[0], "duration_s": k[1], "wh": sum(v[k] for v in values)} for k in sorted(common)]
        if not rows:
            raise ConnectionError("National Grid has no complete, measured hourly or sub-hourly readings available yet. Try syncing later.")
        if not api.account_has_active_data_authorization(connection["account_number"]):
            raise AuthorizationExpired("Permission was withdrawn during the refresh.")
        return api.import_interval_frame_to_db(pd.DataFrame(rows),
            source_path=f"national-grid://connection/{connection['id']}",
            account_number=connection["account_number"], adapter_id=METHOD,
            adapter_name="National Grid automatic connection",
            connection_guard=(int(connection["id"]), json.dumps(payload)))
    except AuthorizationExpired:
        with api.get_db_connection() as conn:
            row = conn.execute("SELECT secret_token FROM utility_connections WHERE id = ?", (connection["id"],)).fetchone()
            if row and api.unseal_secret_value(row["secret_token"]) == connection["access_secret"]:
                conn.execute("UPDATE utility_connections SET secret_token = NULL, status = ? WHERE id = ? AND access_method = ? AND secret_token = ?",
                             ("Reconnect National Grid", connection["id"], METHOD, row["secret_token"]))
            conn.commit()
        raise
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ConnectionError("The saved National Grid connection is incomplete. Connect again.") from None


def register_routes(app, api, current_customer_user):
    def manager(account_number):
        user = current_customer_user()
        if not user:
            raise ConnectionError("Sign in as an account manager before connecting National Grid.")
        access = api.get_customer_account_access(user["email"], account_number)
        if not access or access["access_level"] != "Manager":
            raise ConnectionError("Manager access is required to connect this account.")
        account = api.find_account(account_number)
        if not account or not provider_matches(account["energy_company"]):
            raise ConnectionError("Choose a National Grid account first.")
        if not api.account_has_active_data_authorization(account_number):
            raise ConnectionError("Give permission to use this account's data before connecting National Grid.")
        return user, account

    @app.post("/utility-connection/national-grid/start")
    def national_grid_start():
        account_number = request.form.get("account_number", "")
        try:
            cfg = config()
            user, account = manager(account_number)
            state = secrets.token_urlsafe(32)
            browser = secrets.token_urlsafe(32)
            session["national_grid_browser"] = browser
            with api.get_db_connection() as conn:
                conn.execute("DELETE FROM national_grid_oauth_states WHERE expires_at < ?", (int(time.time()),))
                conn.execute("""INSERT INTO national_grid_oauth_states
                    (state_hash, account_number, customer_id, browser_hash, expires_at) VALUES (?, ?, ?, ?, ?)""",
                    (hashlib.sha256(state.encode()).hexdigest(), account["account_number"], user["id"],
                     hashlib.sha256(browser.encode()).hexdigest(), int(time.time()) + 600))
                conn.commit()
            return redirect(cfg["AUTHORIZE_URL"] + "?" + urlencode({"response_type": "code", "client_id": cfg["CLIENT_ID"],
                "redirect_uri": cfg["REDIRECT_URI"], "state": state}))
        except ConnectionError as exc:
            flash(str(exc))
            return redirect(url_for("customer_utility_page", account_number=account_number))

    @app.get(CALLBACK)
    def national_grid_callback():
        account_number = ""
        try:
            cfg = config()
            state = request.args.get("state", "")
            if not 32 <= len(state) <= 256:
                raise ConnectionError("Start National Grid sign-in again; this return link has expired.")
            digest = hashlib.sha256(state.encode()).hexdigest()
            with api.get_db_connection() as conn:
                row = conn.execute("SELECT * FROM national_grid_oauth_states WHERE state_hash = ?", (digest,)).fetchone()
                if not row or row["used"] or row["expires_at"] <= time.time():
                    raise ConnectionError("Start National Grid sign-in again; this return link has expired.")
                user, account = manager(row["account_number"])
                browser = hashlib.sha256(session.get("national_grid_browser", "").encode()).hexdigest()
                if user["id"] != row["customer_id"] or not secrets.compare_digest(browser, row["browser_hash"]):
                    raise ConnectionError("Return using the same sign-in that started this connection.")
                changed = conn.execute("UPDATE national_grid_oauth_states SET used = 1 WHERE state_hash = ? AND used = 0", (digest,))
                if changed.rowcount != 1:
                    raise ConnectionError("This National Grid return link was already used.")
                conn.commit()
            account_number = account["account_number"]
            if request.args.get("error"):
                raise ConnectionError("National Grid sharing was not approved. Your existing history has not changed.")
            code = request.args.get("code", "")
            if not code or len(code) > 4096:
                raise ConnectionError("National Grid did not return a sign-in code. Please try again.")
            payload = exchange({"grant_type": "authorization_code", "code": code, "redirect_uri": cfg["REDIRECT_URI"]})
            payload["meters"] = matched_usage_points(payload, account_number)
            manager(account_number)  # Recheck after network requests.
            save_connection(api, account, payload)
            api.record_audit_event("utility.national_grid_connected", actor_type="customer", actor_id=user["id"],
                                   account_number=account_number, metadata={"meter_count": len(payload["meters"])})
            flash("National Grid is connected. Choose Sync now to import your history. Daily updates will follow.")
        except ConnectionError as exc:
            flash(str(exc))
        except Exception as exc:
            api.emit_application_log("utility.national_grid_connection_failed", error_type=type(exc).__name__)
            flash("Home Energy Watch could not save the National Grid connection. Please start again.")
        response = redirect(url_for("customer_utility_page", account_number=account_number))
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
