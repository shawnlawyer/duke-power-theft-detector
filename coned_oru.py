"""Con Edison and Orange & Rockland Green Button Connect My Data connector.

This connector is switched off in source. ``IMPLEMENTATION_READY`` stays False
until the utility has accepted Home Energy Watch, the asynchronous batch
lifecycle is finished, and activation is explicitly approved. Deployment
settings alone can never turn it on. Customer file uploads remain the supported
path for these utilities.

Credentials stay server-side. Every request is HTTPS to the configured token
endpoint's origin, never follows redirects, and is size-bounded. The customer
account is bound to the provider-returned subscription before anything is
saved, and the binding is re-verified on every sync.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from flask import flash, redirect, request, session, url_for

METHOD = "coned_oru_green_button"
# Deliberately not an environment flag. See the module docstring.
IMPLEMENTATION_READY = False
PROVIDERS = {
    "CONED": {"label": "Con Edison", "aliases": ("con edison", "coned", "con ed",
                                                 "consolidated edison", "con edison company of new york")},
    "ORU": {"label": "Orange & Rockland", "aliases": ("orange & rockland", "orange and rockland",
                                                      "o&r", "oru", "orange & rockland utilities")},
}
DEFAULT_SCOPE = ("FB=1_3_4_5_7_8_10_15_16_51_53_56_57_58_60;IntervalDuration=Monthly_3600_900_300;"
                 "BlockDuration=Monthly_Daily;HistoryLength=63072000")
TOKEN_LIMIT = 1024 * 1024
DATA_LIMIT = 200 * 1024 * 1024
STATE_MINUTES = 10


class ConnectionError(ValueError):
    pass


class AuthorizationExpired(ConnectionError):
    pass


def provider_key(name):
    normalized = re.sub(r"\s+", " ", str(name or "").strip().lower())
    for key, provider in PROVIDERS.items():
        if normalized in provider["aliases"]:
            return key
    return None


def provider_matches(name):
    return provider_key(name) is not None


def label(key):
    return PROVIDERS.get(str(key or "").upper(), {}).get("label", "your utility")


def callback_path(key):
    return f"/utility-connection/green-button/{key.lower()}/callback"


def config(key):
    key = str(key or "").upper()
    if key not in PROVIDERS:
        raise ConnectionError("Choose Con Edison or Orange & Rockland on the Account page first.")
    if not IMPLEMENTATION_READY:
        raise ConnectionError(f"{label(key)} automatic updates are not available yet. You can still upload your history.")
    prefix = f"POWER_{key}_GREEN_BUTTON_"
    values = {name.lower(): os.getenv(prefix + name, "").strip() for name in
              ("AUTHORIZE_URL", "TOKEN_URL", "CLIENT_ID", "CLIENT_SECRET", "BULK_ID", "REDIRECT_URI")}
    values["scope"] = os.getenv(prefix + "SCOPE", "").strip() or DEFAULT_SCOPE
    if os.getenv(prefix + "APPROVED", "").lower() != "true" or not all(values.values()):
        raise ConnectionError(f"{label(key)} automatic updates are waiting for utility approval. You can still upload your history.")
    token_origin = origin(values["token_url"])
    if origin(values["authorize_url"]) != token_origin:
        raise ConnectionError(f"{label(key)} connection settings need attention.")
    for name in ("authorize_url", "token_url"):
        if urlsplit(values[name]).query:
            raise ConnectionError(f"{label(key)} connection settings need attention.")
    p = urlsplit(values["redirect_uri"])
    public = urlsplit(os.getenv("POWER_PUBLIC_BASE_URL", ""))
    if (p.scheme != "https" or not p.netloc or p.username or p.netloc != public.netloc
            or p.path != callback_path(key) or p.query or p.fragment):
        raise ConnectionError(f"{label(key)} return address needs attention.")
    values["label"] = label(key)
    return values


def available(key):
    try:
        config(key)
        return True
    except ConnectionError:
        return False


def origin(url):
    try:
        parts = urlsplit(str(url))
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError()
        return parts.scheme, parts.hostname.lower(), parts.port or 443
    except ValueError:
        raise ConnectionError("The utility returned an unexpected resource address.") from None


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConnectionError("The utility returned an unexpected redirect.")


def opener():
    return build_opener(RejectRedirects())


def request_bytes(cfg, url, *, headers, data=None, limit=TOKEN_LIMIT):
    if origin(url) != origin(cfg["token_url"]):
        raise ConnectionError("The utility returned an unexpected resource address.")
    req = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with opener().open(req, timeout=60) as response:
            status = getattr(response, "status", 200)
            if status == 202:
                # Batch delivery is asynchronous. Until notification handling is
                # finished, a pending batch is an error, never an empty export.
                raise ConnectionError("The utility is preparing your data. Automatic batch delivery is not available yet.")
            if status != 200:
                raise ConnectionError("The utility did not return a completed response.")
            content = response.read(limit + 1)
    except HTTPError as exc:
        if exc.code in (401, 403) or (data is not None and exc.code == 400):
            raise AuthorizationExpired(f"{cfg['label']} access has ended or needs renewal. Connect again.") from None
        raise ConnectionError(f"{cfg['label']} could not complete the request. Please try again later.") from None
    except (URLError, TimeoutError, OSError):
        raise ConnectionError(f"{cfg['label']} is unavailable. Please try again later.") from None
    if len(content) > limit:
        raise ConnectionError("The utility response exceeded the supported size.")
    return content


def basic_auth(cfg):
    return "Basic " + base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()


def token_request(cfg, form):
    raw = request_bytes(cfg, cfg["token_url"], data=urlencode(form).encode(), headers={
        "Authorization": basic_auth(cfg), "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConnectionError("The utility did not return a usable connection. Connect again.") from None
    if not isinstance(result, dict):
        raise ConnectionError("The utility did not return a usable connection. Connect again.")
    return result


def serialize_secret(key, token_data, resource_url=None, *, binding=None):
    access_token = token_data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConnectionError("The utility did not provide an access token. Connect again.")
    resource_url = resource_url or str(token_data.get("resourceURI") or token_data.get("resource_uri") or "")
    if not resource_url:
        raise ConnectionError("The utility did not provide a customer data resource. Connect again.")
    try:
        expires_in = int(token_data.get("expires_in", 3600))
    except (TypeError, ValueError):
        raise ConnectionError("The utility returned an invalid token expiry.") from None
    if not 0 <= expires_in <= 86400 * 30:
        raise ConnectionError("The utility returned an invalid token expiry.")
    tokens = {name: token_data.get(name) for name in ("access_token", "refresh_token", "token_type", "scope")}
    tokens["expires_at"] = int(time.time()) + expires_in
    return json.dumps({"version": 1, "provider_key": key, "resource_url": resource_url,
                       "binding": binding, "tokens": tokens}, sort_keys=True, separators=(",", ":"))


def parse_secret(value):
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        raise ConnectionError("The saved utility connection could not be read. Connect again.") from None
    if (not isinstance(payload, dict) or payload.get("provider_key") not in PROVIDERS
            or not payload.get("resource_url") or not isinstance(payload.get("tokens"), dict)):
        raise ConnectionError("The saved utility connection is incomplete. Connect again.")
    return payload


def account_key(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def validate_customer_resource(key, token_data, expected_account):
    """Bind the account to the provider's AuthorizationModel.

    Only the explicit shape in the utility's published Swagger is accepted.
    Unknown wrappers are rejected rather than searched for a matching number.
    """
    cfg = config(key)
    resource = str(token_data.get("resourceURI") or "")
    customer = str(token_data.get("customerResourceURI") or "")
    authorization = str(token_data.get("authorizationURI") or "")
    access_token = str(token_data.get("access_token") or "")
    token_origin = origin(cfg["token_url"])
    for url in (resource, customer, authorization):
        if origin(url) != token_origin:
            raise ConnectionError("The utility returned an unexpected customer resource.")
    subscription = re.fullmatch(r"(.*/resource/)Subscription/([A-Za-z0-9_-]+)", urlsplit(resource).path)
    if (not subscription or not access_token
            or urlsplit(customer).path != f"{subscription[1]}Customer/{subscription[2]}"
            or not re.fullmatch(re.escape(subscription[1]) + r"Authorization/[A-Za-z0-9_-]+", urlsplit(authorization).path)
            or any(urlsplit(url).query for url in (resource, customer, authorization))):
        raise ConnectionError("The utility account and subscription could not be matched.")
    raw = request_bytes(cfg, authorization, headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"})
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ConnectionError("The utility account response could not be verified.") from None
    if not isinstance(document, dict):
        raise ConnectionError("The utility account response could not be verified.")
    subscriptions = document.get("subscriptions")
    if (not isinstance(document.get("AccountNumber"), str)
            or not account_key(expected_account)
            or account_key(document["AccountNumber"]) != account_key(expected_account)
            or document.get("ResourceURI") != resource
            or not document.get("RetailCustomerId")
            or not isinstance(subscriptions, list) or len(subscriptions) != 1
            or not isinstance(subscriptions[0], dict) or str(subscriptions[0].get("Id")) != subscription[2]
            or (document.get("AuthorizationUri") and document["AuthorizationUri"] != authorization)):
        raise ConnectionError("The utility did not return an unambiguous match for the selected account.")
    return {"account_number": str(expected_account).strip(), "subscription_id": subscription[2],
            "retail_customer_id": str(document["RetailCustomerId"]), "resource_uri": resource,
            "customer_resource_uri": customer, "authorization_uri": authorization}


def require_binding(payload, account_number):
    binding = payload.get("binding")
    if (not isinstance(binding, dict) or account_key(binding.get("account_number")) != account_key(account_number)
            or binding.get("resource_uri") != payload.get("resource_url")
            or not all(binding.get(name) for name in ("subscription_id", "retail_customer_id",
                                                       "customer_resource_uri", "authorization_uri"))):
        raise ConnectionError("The saved utility account mapping must be verified again. Connect again.")
    return binding


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS coned_oru_oauth_states (
        state_hash TEXT PRIMARY KEY, provider_key TEXT NOT NULL, account_number TEXT NOT NULL,
        customer_id BIGINT NOT NULL, browser_hash TEXT NOT NULL,
        expires_at BIGINT NOT NULL, used INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS coned_oru_sync_locks (
        connection_id BIGINT PRIMARY KEY, lease TEXT NOT NULL, expires_at BIGINT NOT NULL)""")


def save_connection(api, account, key, secret):
    payload = parse_secret(secret)
    require_binding(payload, account["account_number"])
    if payload["provider_key"] != key:
        raise ConnectionError("The utility connection does not match the verified account.")
    sealed = api.seal_secret_value(secret)
    now = api.timestamp_now()
    with api.get_db_connection() as conn:
        # Lock the parent while creating/updating to avoid duplicate connections.
        conn.execute("UPDATE accounts SET account_number = account_number WHERE id = ?", (account["id"],))
        permitted = conn.execute("SELECT 1 FROM account_data_authorizations WHERE account_id = ? AND status = 'active'",
                                 (account["id"],)).fetchone()
        if not permitted:
            raise ConnectionError("Permission was withdrawn before the connection could be saved.")
        row = conn.execute("SELECT id FROM utility_connections WHERE account_id = ? AND access_method = ?",
                           (account["id"], METHOD)).fetchone()
        if row:
            conn.execute("""UPDATE utility_connections SET provider_name = ?, connection_label = ?, secret_token = ?,
                status = ?, updated_at = ?, last_sync_error = NULL WHERE id = ?""",
                (label(key), f"{label(key)} automatic updates", sealed, "Ready to sync", now, row["id"]))
        else:
            conn.execute("""INSERT INTO utility_connections (account_id, provider_name, connection_label,
                access_method, secret_token, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (account["id"], label(key), f"{label(key)} automatic updates", METHOD, sealed, "Ready to sync", now, now))
        saved = conn.execute("SELECT id FROM utility_connections WHERE account_id = ? AND access_method = ?",
                             (account["id"], METHOD)).fetchone()
        conn.commit()
    return int(saved["id"])


def replace_secret(api, connection, new_secret):
    """Store refreshed tokens only if the connection is unchanged and still permitted."""
    with api.get_db_connection() as conn:
        row = conn.execute("""SELECT utility_connections.secret_token FROM utility_connections
            JOIN accounts ON accounts.id = utility_connections.account_id
            WHERE utility_connections.id = ? AND utility_connections.access_method = ?
              AND EXISTS (SELECT 1 FROM account_data_authorizations
                          WHERE account_id = accounts.id AND status = 'active')""",
            (connection["id"], METHOD)).fetchone()
        if not row or api.unseal_secret_value(row["secret_token"]) != connection["access_secret"]:
            raise ConnectionError("Utility access changed or permission was withdrawn. Start again.")
        changed = conn.execute("""UPDATE utility_connections SET secret_token = ?, updated_at = ?
            WHERE id = ? AND access_method = ? AND secret_token = ?""",
            (api.seal_secret_value(new_secret), api.timestamp_now(), connection["id"], METHOD, row["secret_token"]))
        if changed.rowcount != 1:
            raise ConnectionError("Utility access changed or permission was withdrawn. Start again.")
        conn.commit()
    connection["access_secret"] = new_secret


def refresh(api, cfg, connection, payload):
    tokens = payload["tokens"]
    if tokens.get("access_token") and float(tokens.get("expires_at") or 0) > time.time() + 60:
        return payload
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        raise AuthorizationExpired(f"{cfg['label']} access has expired. Connect again.")
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if tokens.get("scope"):
        # Keep the customer's granted scope. Never re-request the configured one.
        form["scope"] = str(tokens["scope"])
    refreshed = token_request(cfg, form)
    refreshed["refresh_token"] = refreshed.get("refresh_token") or refresh_token
    if "scope" not in refreshed:
        refreshed["scope"] = tokens.get("scope")
    new_secret = serialize_secret(payload["provider_key"], refreshed, payload["resource_url"], binding=payload["binding"])
    replace_secret(api, connection, new_secret)
    return parse_secret(new_secret)


def fetch_export(api, connection):
    payload = parse_secret(connection["access_secret"])
    key = payload["provider_key"]
    cfg = config(key)  # Disabling the connector also stops scheduled collections.
    binding = require_binding(payload, connection["account_number"])
    payload = refresh(api, cfg, connection, payload)
    token = str(payload["tokens"]["access_token"])
    current = validate_customer_resource(key, {"access_token": token, "resourceURI": binding["resource_uri"],
                                               "customerResourceURI": binding["customer_resource_uri"],
                                               "authorizationURI": binding["authorization_uri"]},
                                         connection["account_number"])
    if current != binding:
        raise ConnectionError("The utility account mapping changed. Connect again.")
    return request_bytes(cfg, payload["resource_url"], limit=DATA_LIMIT,
                         headers={"Accept": "application/atom+xml, application/xml", "Authorization": f"Bearer {token}"})


def sync(api, connection):
    lease = secrets.token_urlsafe(24)
    with api.get_db_connection() as conn:
        changed = conn.execute("""INSERT INTO coned_oru_sync_locks (connection_id, lease, expires_at)
            VALUES (?, ?, ?) ON CONFLICT(connection_id) DO UPDATE SET lease = excluded.lease,
            expires_at = excluded.expires_at WHERE coned_oru_sync_locks.expires_at < ?""",
            (connection["id"], lease, int(time.time()) + 600, int(time.time())))
        if changed.rowcount != 1:
            raise ConnectionError("This utility connection is already syncing. Please wait for it to finish.")
        conn.commit()
    try:
        # Reload after taking the lease; a preceding sync may have rotated credentials.
        connection = api.load_utility_connection_for_sync(connection["account_number"], connection["id"])
        return _sync(api, connection)
    finally:
        with api.get_db_connection() as conn:
            conn.execute("DELETE FROM coned_oru_sync_locks WHERE connection_id = ? AND lease = ?", (connection["id"], lease))
            conn.commit()


def _sync(api, connection):
    try:
        content = fetch_export(api, connection)
        if not api.account_has_active_data_authorization(connection["account_number"]):
            raise ConnectionError("Customer authorization was withdrawn during the download.")
        # Parse from a private temporary file that is always removed; nothing is
        # left in the import folder if the import is refused.
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "green-button.xml"
            path.write_bytes(content)
            parsed = api.parse_interval_file(path)
        payload = parse_secret(connection["access_secret"])
        return api.import_interval_frame_to_db(parsed.frame,
            source_path=f"green-button://{payload['provider_key'].lower()}/connection/{connection['id']}",
            account_number=connection["account_number"], adapter_id=METHOD,
            adapter_name=f"{label(payload['provider_key'])} automatic connection",
            connection_guard=(int(connection["id"]), connection["access_secret"]))
    except AuthorizationExpired:
        with api.get_db_connection() as conn:
            row = conn.execute("SELECT secret_token, provider_name FROM utility_connections WHERE id = ?",
                               (connection["id"],)).fetchone()
            if row and api.unseal_secret_value(row["secret_token"]) == connection["access_secret"]:
                conn.execute("""UPDATE utility_connections SET secret_token = NULL, status = ?
                    WHERE id = ? AND access_method = ? AND secret_token = ?""",
                    (f"Reconnect {row['provider_name']}", connection["id"], METHOD, row["secret_token"]))
            conn.commit()
        raise
    except (KeyError, TypeError):
        raise ConnectionError("The saved utility connection is incomplete. Connect again.") from None


def register_routes(app, api, current_customer_user):
    def manager(account_number, key):
        user = current_customer_user()
        if not user:
            raise ConnectionError(f"Sign in as an account manager before connecting {label(key)}.")
        access = api.get_customer_account_access(user["email"], account_number)
        if not access or access["access_level"] != "Manager":
            raise ConnectionError("Manager access is required to connect this account.")
        account = api.find_account(account_number)
        if not account or provider_key(account["energy_company"]) != key:
            raise ConnectionError(f"Choose {label(key)} on the Account page first.")
        if not api.account_has_active_data_authorization(account_number):
            raise ConnectionError(f"Give permission to use this account's data before connecting {label(key)}.")
        return user, account

    @app.post("/utility-connection/green-button/<key>/start")
    def coned_oru_start(key):
        key = key.upper()
        account_number = request.form.get("account_number", "")
        try:
            cfg = config(key)
            user, account = manager(account_number, key)
            state = secrets.token_urlsafe(32)
            browser = secrets.token_urlsafe(32)
            session["coned_oru_browser"] = browser
            with api.get_db_connection() as conn:
                conn.execute("DELETE FROM coned_oru_oauth_states WHERE expires_at < ?", (int(time.time()),))
                conn.execute("""INSERT INTO coned_oru_oauth_states
                    (state_hash, provider_key, account_number, customer_id, browser_hash, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (hashlib.sha256(state.encode()).hexdigest(), key, account["account_number"], user["id"],
                     hashlib.sha256(browser.encode()).hexdigest(), int(time.time()) + STATE_MINUTES * 60))
                conn.commit()
            scope = cfg["scope"] if ";BR=" in cfg["scope"] else f"{cfg['scope']};BR={cfg['bulk_id']}"
            return redirect(cfg["authorize_url"] + "?" + urlencode({"response_type": "code", "client_id": cfg["client_id"],
                            "redirect_uri": cfg["redirect_uri"], "state": state, "scope": scope}))
        except ConnectionError as exc:
            flash(str(exc))
            return redirect(url_for("customer_utility_page", account_number=account_number))

    @app.get("/utility-connection/green-button/<key>/callback")
    def coned_oru_callback(key):
        key = key.upper()
        account_number = ""
        try:
            cfg = config(key)
            state = request.args.get("state", "")
            if not 32 <= len(state) <= 256:
                raise ConnectionError(f"Start {label(key)} sign-in again; this return link has expired.")
            digest = hashlib.sha256(state.encode()).hexdigest()
            with api.get_db_connection() as conn:
                row = conn.execute("SELECT * FROM coned_oru_oauth_states WHERE state_hash = ?", (digest,)).fetchone()
                if not row or row["used"] or row["expires_at"] <= time.time() or row["provider_key"] != key:
                    raise ConnectionError(f"Start {label(key)} sign-in again; this return link has expired.")
                user, account = manager(row["account_number"], key)
                browser = hashlib.sha256(session.get("coned_oru_browser", "").encode()).hexdigest()
                if user["id"] != row["customer_id"] or not secrets.compare_digest(browser, row["browser_hash"]):
                    raise ConnectionError("Return using the same sign-in that started this connection.")
                changed = conn.execute("UPDATE coned_oru_oauth_states SET used = 1 WHERE state_hash = ? AND used = 0", (digest,))
                if changed.rowcount != 1:
                    raise ConnectionError("This return link was already used.")
                conn.commit()
            account_number = account["account_number"]
            if request.args.get("error"):
                raise ConnectionError(f"{label(key)} sharing was not approved. Your existing history has not changed.")
            code = request.args.get("code", "")
            if not code or len(code) > 4096:
                raise ConnectionError(f"{label(key)} did not return a sign-in code. Please try again.")
            tokens = token_request(cfg, {"grant_type": "authorization_code", "code": code, "redirect_uri": cfg["redirect_uri"]})
            binding = validate_customer_resource(key, tokens, account_number)
            secret = serialize_secret(key, tokens, binding["resource_uri"], binding=binding)
            manager(account_number, key)  # Recheck after network requests.
            save_connection(api, account, key, secret)
            api.record_audit_event("utility.green_button_connected", actor_type="customer", actor_id=user["id"],
                                   account_number=account_number, metadata={"provider": key})
            flash(f"{label(key)} is connected. Choose Sync now to import your history. Daily updates will follow.")
        except ConnectionError as exc:
            flash(str(exc))
        except Exception as exc:
            api.emit_application_log("utility.green_button_connection_failed", error_type=type(exc).__name__)
            flash(f"Home Energy Watch could not save the {label(key)} connection. Please start again.")
        response = redirect(url_for("customer_utility_page", account_number=account_number))
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response
