"""Xcel Green Button connection; disabled until explicitly configured and approved.

Notifications are untrusted hints, never retrieval instructions. Customer-selected
electric UsagePoints are bound to one account; Xcel supplies no account number.
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
from urllib.parse import urlencode, urlsplit, parse_qsl
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import flash, redirect, request, session, url_for, render_template
from lxml import etree
import pandas as pd

METHOD = "xcel_oauth"
CALLBACK = "/utility-connection/xcel/callback"
NOTIFICATION = "/utility-connection/xcel/notifications"
BASE = "https://xe.planetecosystems.com/greenbutton-connect/gbc/espi/1_1/"
RESOURCE = BASE + "resource/"
SCOPE = "FB=1 3 4 5 10 13 15 31 37 39"
NS = {"a": "http://www.w3.org/2005/Atom", "e": "http://naesb.org/espi"}
LIMIT = 25 * 1024 * 1024
ID = r"[A-Za-z0-9_-]{1,128}"


class ConnectionError(ValueError):
    pass


class AuthorizationExpired(ConnectionError):
    pass


def config():
    values = {k: os.getenv("POWER_XCEL_" + k, "").strip() for k in
              ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI")}
    if os.getenv("POWER_XCEL_ENABLED", "").lower() != "true" or not all(values.values()):
        raise ConnectionError("Xcel automatic updates are not available yet. You can still upload your history.")
    public = urlsplit(os.getenv("POWER_PUBLIC_BASE_URL", ""))
    target = urlsplit(values["REDIRECT_URI"])
    if (target.scheme != "https" or not target.netloc or target.username or
            target.netloc != public.netloc or target.path != CALLBACK or target.query or target.fragment):
        raise ConnectionError("Xcel's return address needs attention.")
    return values


def available():
    try:
        config()
        return True
    except ConnectionError:
        return False


def provider_matches(name):
    return "xcel" in str(name or "").lower()


def resource_url(value):
    value = str(value)
    if (not value.startswith(RESOURCE) or not re.fullmatch(r"[A-Za-z0-9_/-]+", value[len(RESOURCE):])
            or "//" in value[len(RESOURCE):]):
        raise ConnectionError("Xcel returned an unexpected data address.")
    return value


def subscription_id(value):
    match = re.fullmatch(re.escape(RESOURCE) + r"(?:Batch/)?Subscription/(" + ID + ")", str(value))
    if not match:
        raise ConnectionError("Xcel returned an unexpected subscription.")
    return match[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(url, *, token=None, form=None, client=None):
    config()  # Every outbound path is guarded, including refresh and notifications.
    headers = {"Accept": "application/json" if form else "application/atom+xml,application/xml"}
    if form:
        if url != BASE + "oauth/token":
            raise ConnectionError("Unexpected Xcel token address.")
        credentials = base64.b64encode((client["CLIENT_ID"] + ":" + client["CLIENT_SECRET"]).encode()).decode()
        headers.update({"Authorization": "Basic " + credentials, "Content-Type": "application/x-www-form-urlencoded"})
    else:
        resource_url(url)
        headers["Authorization"] = "Bearer " + token
    try:
        with build_opener(NoRedirect()).open(Request(url, data=urlencode(form).encode() if form else None,
                                                     headers=headers), timeout=30) as response:
            data = response.read(LIMIT + 1)
    except HTTPError as exc:
        if exc.code in (401, 403, 410) or (form and exc.code == 400):
            raise AuthorizationExpired("Xcel access has ended or needs renewal. Connect again.") from None
        raise ConnectionError("Xcel could not complete the request. Try again later.") from None
    except (URLError, TimeoutError, OSError):
        raise ConnectionError("Xcel is unavailable. Try again later.") from None
    if len(data) > LIMIT:
        raise ConnectionError("Xcel returned more data than can be processed safely.")
    return data


def exchange(form, previous=None):
    cfg = config()
    raw = transport(BASE + "oauth/token", form=form, client=cfg)
    try:
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        payload = {**(previous or {}), **result}
        if str(payload.get("token_type", "")).lower() != "bearer":
            raise ValueError()
        for name in ("access_token", "refresh_token"):
            if not isinstance(payload.get(name), str) or not re.fullmatch(r"[!-~]{1,8192}", payload[name]):
                raise ValueError()
        lifetime = int(result["expires_in"])
        if not 0 < lifetime <= 86400:
            raise ValueError()
        sub = subscription_id(payload["resourceURI"])
        auth = RESOURCE + "Authorization/" + sub
        if payload.get("authorizationURI") != auth or (previous and sub != previous["subscription"]):
            raise ValueError()
        # Retain only the fields used by this connector, never arbitrary response fields.
        return {"access_token": payload["access_token"], "refresh_token": payload["refresh_token"],
                "token_type": "Bearer", "resourceURI": RESOURCE + "Batch/Subscription/" + sub,
                "authorizationURI": auth, "subscription": sub, "expires_at": time.time() + lifetime,
                "meters": (previous or {}).get("meters", [])}
    except (ValueError, KeyError, TypeError):
        raise ConnectionError("Xcel did not return a usable connection. Please connect again.") from None


def xml_root(data):
    if len(data) > LIMIT:
        raise ConnectionError("Xcel returned too much data.")
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
        if root.getroottree().docinfo.doctype:
            raise ValueError()
        return root
    except (etree.XMLSyntaxError, ValueError):
        raise ConnectionError("Xcel returned unreadable data.") from None


def entries(data):
    root = xml_root(data)
    if root.tag not in ("{" + NS["a"] + "}feed", "{" + NS["a"] + "}entry"):
        raise ConnectionError("Xcel returned an unexpected document.")
    if root.xpath(".//a:link[@rel='next']", namespaces=NS):
        raise ConnectionError("Xcel returned paginated data. No partial history was imported.")
    return [root] if root.tag.endswith("}entry") else root.findall("a:entry", NS)


def obj(entry, name):
    items = entry.findall("a:content/e:" + name, NS)
    return items[0] if len(items) == 1 else None


def field(element, path, default=""):
    if element is None:
        return default
    matches = element.findall("/".join("e:" + p for p in path.split("/")), NS)
    return (matches[0].text or "").strip() if len(matches) == 1 else default


def self_url(entry):
    values = entry.xpath("a:link[@rel='self']/@href", namespaces=NS)
    if len(values) != 1:
        raise ConnectionError("Xcel returned an ambiguous record.")
    return resource_url(values[0])


def usage_points(payload):
    prefix = RESOURCE + "Subscription/" + payload["subscription"] + "/UsagePoint/"
    items = entries(transport(prefix[:-1], token=payload["access_token"]))
    result = {}
    for item in items:
        point = obj(item, "UsagePoint")
        if point is None or field(point, "ServiceCategory/kind") != "0":
            continue
        url = self_url(item)
        if not re.fullmatch(re.escape(prefix) + ID, url) or url in result:
            raise ConnectionError("Xcel returned an unexpected meter relationship.")
        result[url] = (item.findtext("a:title", default="", namespaces=NS) or url.rsplit("/", 1)[-1])[:200]
    if not result or len(result) > 10:
        raise ConnectionError("Xcel did not return a supported list of electric meters.")
    return result


def meter_intervals(data, meter):
    feed = entries(data)
    types = {}
    readings = {}
    for item in feed:
        rt = obj(item, "ReadingType")
        if rt is not None:
            key = self_url(item)
            if key in types:
                raise ConnectionError("Xcel returned duplicate reading types.")
            types[key] = rt
    for item in feed:
        if obj(item, "MeterReading") is None:
            continue
        key = self_url(item)
        if not re.fullmatch(re.escape(meter) + "/MeterReading/" + ID, key) or key in readings:
            raise ConnectionError("Xcel mixed or repeated meters in one response.")
        links = item.xpath("a:link[@rel='related']/@href", namespaces=NS)
        candidates = [types[link] for link in links if link in types]
        if len(candidates) != 1:
            raise ConnectionError("Xcel did not identify the reading units.")
        readings[key] = candidates[0]
    channels = {}
    for item in feed:
        block = obj(item, "IntervalBlock")
        if block is None:
            continue
        url = self_url(item)
        key = url.split("/IntervalBlock/")[0]
        if key not in readings or not re.fullmatch(re.escape(key) + "/IntervalBlock/" + ID, url):
            raise ConnectionError("Xcel returned intervals without their meter and units.")
        rt = readings[key]
        if (field(rt, "uom"), field(rt, "kind"), field(rt, "flowDirection")) != ("72", "12", "1"):
            continue
        if field(rt, "accumulationBehaviour") != "4":
            raise ConnectionError("Cumulative readings cannot be treated as interval usage.")
        channel = channels.setdefault(key, {})
        try:
            multiplier = int(field(rt, "powerOfTenMultiplier", "0"))
            if not -12 <= multiplier <= 12:
                raise ValueError()
            for reading in block.findall("e:IntervalReading", NS):
                quality = [field(q, "quality", (q.text or "").strip()) for q in reading.findall("e:ReadingQuality", NS)]
                if any(q not in ("0", "14", "17", "18", "19") for q in quality or [field(rt, "defaultQuality", "0")]):
                    continue
                start = int(field(reading, "timePeriod/start"))
                duration = int(field(reading, "timePeriod/duration", field(rt, "intervalLength")))
                wh = float(field(reading, "value")) * 10 ** multiplier
                if start < 0 or not 0 < duration <= 3600 or not math.isfinite(wh) or wh < 0:
                    raise ValueError()
                if start + duration > time.time():
                    continue
                pair = (start, duration)
                if pair in channel and channel[pair] != wh:
                    raise ValueError()
                channel[pair] = wh
        except (ValueError, OverflowError):
            raise ConnectionError("Xcel returned invalid or conflicting readings.") from None
    if len(channels) > 1:
        raise ConnectionError("Xcel returned multiple consumption channels. No ambiguous readings were imported.")
    values = next(iter(channels.values()), {})
    previous_end = -1
    for start, duration in sorted(values):
        if start < previous_end:
            raise ConnectionError("Xcel returned overlapping intervals.")
        previous_end = start + duration
    return values


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS xcel_oauth_states (
        state_hash TEXT PRIMARY KEY, account_number TEXT NOT NULL,
        customer_id BIGINT NOT NULL, browser_hash TEXT NOT NULL,
        expires_at BIGINT NOT NULL, used INTEGER NOT NULL DEFAULT 0,
        pending_secret TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS xcel_sync_locks (
        connection_id BIGINT PRIMARY KEY, lease TEXT NOT NULL, expires_at BIGINT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS xcel_meter_bindings (
        meter_url TEXT PRIMARY KEY, account_id BIGINT NOT NULL, connection_id BIGINT NOT NULL)""")


def save_connection(api, account, payload):
    sealed = api.seal_secret_value(json.dumps(payload))
    now = api.timestamp_now()
    with api.get_db_connection() as conn:
        conn.execute("UPDATE accounts SET account_number = account_number WHERE id = ?", (account["id"],))
        if not conn.execute("SELECT 1 FROM account_data_authorizations WHERE account_id = ? AND status = 'active'", (account["id"],)).fetchone():
            raise ConnectionError("Permission was withdrawn before the connection could be saved.")
        row = conn.execute("SELECT id FROM utility_connections WHERE account_id = ? AND access_method = ?", (account["id"], METHOD)).fetchone()
        if row:
            connection_id = row["id"]
            conn.execute("UPDATE utility_connections SET secret_token = ?, status = ?, updated_at = ?, last_sync_error = NULL WHERE id = ?",
                         (sealed, "Ready to sync", now, connection_id))
        else:
            conn.execute("""INSERT INTO utility_connections (account_id, provider_name, connection_label,
                access_method, secret_token, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (account["id"], account["energy_company"], "Xcel automatic updates", METHOD, sealed, "Ready to sync", now, now))
            connection_id = conn.execute("SELECT id FROM utility_connections WHERE account_id = ? AND access_method = ?", (account["id"], METHOD)).fetchone()["id"]
        # Global unique meter binding prevents the same meter being linked to two accounts.
        for meter in payload["meters"]:
            changed = conn.execute("""INSERT INTO xcel_meter_bindings (meter_url, account_id, connection_id)
                VALUES (?, ?, ?) ON CONFLICT(meter_url) DO UPDATE SET connection_id = excluded.connection_id
                WHERE xcel_meter_bindings.account_id = excluded.account_id""", (meter, account["id"], connection_id))
            if changed.rowcount != 1:
                raise ConnectionError("This meter is already linked to another account. Contact support.")
        conn.commit()


def sync(api, connection):
    config()
    lease = secrets.token_urlsafe(24)
    with api.get_db_connection() as conn:
        changed = conn.execute("""INSERT INTO xcel_sync_locks (connection_id, lease, expires_at) VALUES (?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET lease = excluded.lease, expires_at = excluded.expires_at
            WHERE xcel_sync_locks.expires_at < ?""", (connection["id"], lease, int(time.time()) + 600, int(time.time())))
        if changed.rowcount != 1:
            raise ConnectionError("Xcel is already syncing this account.")
        conn.commit()
    try:
        connection = api.load_utility_connection_for_sync(connection["account_number"], connection["id"])
        payload = json.loads(connection["access_secret"])
        if payload["expires_at"] <= time.time() + 60:
            payload = exchange({"grant_type": "refresh_token", "refresh_token": payload["refresh_token"]}, payload)
            with api.get_db_connection() as conn:
                row = conn.execute("SELECT secret_token FROM utility_connections WHERE id = ?", (connection["id"],)).fetchone()
                if not row or api.unseal_secret_value(row["secret_token"]) != connection["access_secret"]:
                    raise ConnectionError("The Xcel connection changed during refresh.")
                changed = conn.execute("UPDATE utility_connections SET secret_token = ?, updated_at = ? WHERE id = ? AND secret_token = ?",
                    (api.seal_secret_value(json.dumps(payload)), api.timestamp_now(), connection["id"], row["secret_token"]))
                if changed.rowcount != 1:
                    raise AuthorizationExpired("Xcel access was removed during refresh.")
                conn.commit()
            connection["access_secret"] = json.dumps(payload)
        points = usage_points(payload)
        meters = payload["meters"]
        if not meters or len(set(meters)) != len(meters) or any(m not in points for m in meters):
            raise ConnectionError("Xcel's meter list changed. Connect again to confirm your meters.")
        values = [meter_intervals(transport(RESOURCE + "Batch/" + meter[len(RESOURCE):], token=payload["access_token"]), meter) for meter in meters]
        common = set.intersection(*(set(v) for v in values))
        rows = [{"start_epoch": k[0], "duration_s": k[1], "wh": sum(v[k] for v in values)} for k in sorted(common)]
        if not rows:
            raise ConnectionError("Xcel has no complete measured hourly or sub-hourly readings available yet.")
        config()  # Recheck the deployment gate immediately before committing data.
        if not api.account_has_active_data_authorization(connection["account_number"]):
            raise AuthorizationExpired("Permission was withdrawn during refresh.")
        return api.import_interval_frame_to_db(pd.DataFrame(rows), source_path=f"xcel://connection/{connection['id']}",
            account_number=connection["account_number"], adapter_id=METHOD, adapter_name="Xcel automatic connection",
            connection_guard=(int(connection["id"]), json.dumps(payload)))
    except AuthorizationExpired:
        with api.get_db_connection() as conn:
            row = conn.execute("SELECT secret_token FROM utility_connections WHERE id = ?", (connection["id"],)).fetchone()
            if row and api.unseal_secret_value(row["secret_token"]) == connection["access_secret"]:
                conn.execute("UPDATE utility_connections SET secret_token = NULL, status = ? WHERE id = ? AND secret_token = ?",
                             ("Reconnect Xcel", connection["id"], row["secret_token"]))
            conn.commit()
        raise
    except (KeyError, TypeError, json.JSONDecodeError):
        raise ConnectionError("The saved Xcel connection is incomplete. Connect again.") from None
    finally:
        with api.get_db_connection() as conn:
            conn.execute("DELETE FROM xcel_sync_locks WHERE connection_id = ? AND lease = ?", (connection["id"], lease))
            conn.commit()


def register_routes(app, api, current_customer_user):
    def manager(number):
        user = current_customer_user()
        if not user:
            raise ConnectionError("Sign in as an account manager before connecting Xcel.")
        access = api.get_customer_account_access(user["email"], number)
        account = api.find_account(number)
        if not access or access["access_level"] != "Manager" or not account or not provider_matches(account["energy_company"]):
            raise ConnectionError("Choose an Xcel account you manage first.")
        if not api.account_has_active_data_authorization(number):
            raise ConnectionError("Give permission to use this account's data before connecting Xcel.")
        return user, account

    def pending(conn, state, *, used):
        if not 32 <= len(state) <= 256:
            raise ConnectionError("Start Xcel sign-in again; this return link has expired.")
        digest = hashlib.sha256(state.encode()).hexdigest()
        row = conn.execute("SELECT * FROM xcel_oauth_states WHERE state_hash = ?", (digest,)).fetchone()
        if not row or row["used"] != used or row["expires_at"] <= time.time():
            raise ConnectionError("Start Xcel sign-in again; this return link has expired.")
        user, account = manager(row["account_number"])
        browser = hashlib.sha256(session.get("xcel_browser", "").encode()).hexdigest()
        if row["customer_id"] != user["id"] or not secrets.compare_digest(browser, row["browser_hash"]):
            raise ConnectionError("Return using the same sign-in that started this connection.")
        return digest, row, user, account

    @app.post("/utility-connection/xcel/start")
    def xcel_start():
        number = request.form.get("account_number", "")
        try:
            cfg = config()
            user, account = manager(number)
            state, browser = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            session["xcel_browser"] = browser
            with api.get_db_connection() as conn:
                conn.execute("DELETE FROM xcel_oauth_states WHERE expires_at < ?", (int(time.time()),))
                conn.execute("""INSERT INTO xcel_oauth_states (state_hash, account_number, customer_id, browser_hash, expires_at)
                    VALUES (?, ?, ?, ?, ?)""", (hashlib.sha256(state.encode()).hexdigest(), account["account_number"], user["id"],
                    hashlib.sha256(browser.encode()).hexdigest(), int(time.time()) + 600))
                conn.commit()
            return redirect(BASE + "oauth/authorize?" + urlencode({"response_type": "code", "client_id": cfg["CLIENT_ID"],
                "redirect_uri": cfg["REDIRECT_URI"], "scope": SCOPE, "state": state}))
        except ConnectionError as exc:
            flash(str(exc))
            return redirect(url_for("customer_utility_page", account_number=number))

    @app.get(CALLBACK)
    def xcel_callback():
        number = ""
        try:
            cfg = config()
            state = request.args.get("state", "")
            with api.get_db_connection() as conn:
                digest, row, user, account = pending(conn, state, used=0)
                changed = conn.execute("UPDATE xcel_oauth_states SET used = 1 WHERE state_hash = ? AND used = 0", (digest,))
                if changed.rowcount != 1:
                    raise ConnectionError("This return link was already used.")
                conn.commit()
            number = account["account_number"]
            if request.args.get("error"):
                raise ConnectionError("Xcel sharing was not approved. Your existing history has not changed.")
            code = request.args.get("code", "")
            if not code or len(code) > 4096:
                raise ConnectionError("Xcel did not return a sign-in code. Please try again.")
            payload = exchange({"grant_type": "authorization_code", "code": code, "redirect_uri": cfg["REDIRECT_URI"]})
            points = usage_points(payload)
            manager(number)
            with api.get_db_connection() as conn:
                conn.execute("UPDATE xcel_oauth_states SET pending_secret = ? WHERE state_hash = ? AND used = 1",
                    (api.seal_secret_value(json.dumps({"token": payload, "points": points})), digest))
                conn.commit()
            session["xcel_pending_state"] = state
            response = redirect(url_for("xcel_meters"))
        except ConnectionError as exc:
            flash(str(exc))
            response = redirect(url_for("customer_utility_page", account_number=number))
        except Exception as exc:
            api.emit_application_log("utility.xcel_connection_failed", error_type=type(exc).__name__)
            flash("Home Energy Watch could not save the Xcel connection. Please start again.")
            response = redirect(url_for("customer_utility_page", account_number=number))
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/utility-connection/xcel/meters", methods=["GET", "POST"])
    def xcel_meters():
        number = ""
        try:
            config()
            with api.get_db_connection() as conn:
                digest, row, user, account = pending(conn, session.get("xcel_pending_state", ""), used=1)
                number = account["account_number"]
                data = json.loads(api.unseal_secret_value(row["pending_secret"]))
                if request.method == "GET":
                    response = app.make_response(render_template("xcel_meters.html", account_number=number, points=data["points"]))
                    response.headers["Cache-Control"] = "no-store"
                    response.headers["Referrer-Policy"] = "no-referrer"
                    return response
                selected = request.form.getlist("meters")
                if not selected or len(set(selected)) != len(selected) or any(m not in data["points"] for m in selected) or request.form.get("confirmed") != "yes":
                    raise ConnectionError("Choose the electric meters for this account and confirm your selection.")
                changed = conn.execute("UPDATE xcel_oauth_states SET used = 2, pending_secret = NULL WHERE state_hash = ? AND used = 1", (digest,))
                if changed.rowcount != 1:
                    raise ConnectionError("This selection has already been used.")
                conn.commit()
            data["token"]["meters"] = sorted(selected)
            manager(number)
            save_connection(api, account, data["token"])
            session.pop("xcel_pending_state", None)
            api.record_audit_event("utility.xcel_connected", actor_type="customer", actor_id=user["id"], account_number=number,
                                   metadata={"meter_count": len(selected), "mapping": "manager_confirmed"})
            flash("Xcel is connected. Choose Sync now to import your history. Daily updates will follow.")
        except ConnectionError as exc:
            flash(str(exc))
        except Exception as exc:
            api.emit_application_log("utility.xcel_selection_failed", error_type=type(exc).__name__)
            flash("The Xcel meter selection could not be saved. Please start again.")
        return redirect(url_for("customer_utility_page", account_number=number))

    @app.post(NOTIFICATION)
    def xcel_notification():
        # Public protocol endpoint, deliberately no login/CSRF. It never mutates
        # connections or performs I/O; the scheduled job verifies current grants.
        if not available():
            return {"status": "disabled"}, 503
        if request.mimetype not in ("application/atom+xml", "application/xml"):
            return {"error": "Unsupported content type"}, 415
        if request.content_length is None or request.content_length > 65536:
            return {"error": "Payload too large or missing length"}, 413
        try:
            root = xml_root(request.get_data())
            if root.tag != "{" + NS["e"] + "}BatchList":
                raise ConnectionError("Invalid notification")
            urls = root.findall("e:resources", NS)
            if len(urls) != 1:
                raise ConnectionError("Invalid notification")
            parsed = urlsplit(urls[0].text or "")
            subscription_id(parsed._replace(query="", fragment="").geturl())
            if parsed.fragment or any(k not in ("published-min", "published-max") for k, _ in parse_qsl(parsed.query)):
                raise ConnectionError("Invalid notification")
        except ConnectionError:
            return {"error": "Invalid notification"}, 400
        # Uniform result for known/unknown subscriptions. Daily polling uses only
        # stored, approved bindings, never the supplied notification URL or range.
        return {"status": "accepted", "collection": "scheduled"}, 202
