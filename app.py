#!/usr/bin/env python3
"""
Local utility interval analyzer with CLI and web UI.

CLI example:
    python app.py --input path/to/interval.xml --output report.csv

Web example:
    python app.py --serve
"""

from __future__ import annotations

import argparse
import base64
import csv
from dataclasses import dataclass
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import zipfile
from datetime import date as ddate, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

from dateutil import tz
from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, flash, g, has_request_context, jsonify, redirect, render_template, request, send_from_directory, session, url_for
import pandas as pd
from lxml import etree
import pyotp
import qrcode
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - SQLite-only runs do not need Postgres support.
    psycopg = None
    dict_row = None

try:
    import stripe
except ImportError:  # pragma: no cover - local tests can use a fake Stripe module.
    stripe = None

try:
    import boto3
except ImportError:  # pragma: no cover - local runs can keep email delivery disabled.
    boto3 = None

DEFAULT_TZ = os.getenv("POWER_TIMEZONE", "America/New_York")
DEFAULT_NIGHT_START = os.getenv("POWER_NIGHT_START", "02:00")
DEFAULT_NIGHT_END = os.getenv("POWER_NIGHT_END", "04:00")
DEFAULT_MIN_NIGHT_KW = float(os.getenv("POWER_MIN_NIGHT_KW", "1.0"))
DEFAULT_NIGHT_MULTIPLIER = float(os.getenv("POWER_NIGHT_MULTIPLIER", "2.0"))
DEFAULT_ALERT_WINDOW_START = os.getenv("POWER_ALERT_WINDOW_START", "00:00")
DEFAULT_ALERT_WINDOW_END = os.getenv("POWER_ALERT_WINDOW_END", "05:00")
DEFAULT_ALERT_MIN_KW = float(os.getenv("POWER_ALERT_MIN_KW", "1.2"))
DEFAULT_ALERT_MULTIPLIER = float(os.getenv("POWER_ALERT_MULTIPLIER", "1.5"))
DEFAULT_ALERT_JUMP_KW = float(os.getenv("POWER_ALERT_JUMP_KW", "0.75"))
DEFAULT_CLI_OUTPUT = "usage_report.csv"
DEFAULT_APP_SECRET = "local-power-data-only"
DEFAULT_INPUT_DIR = Path(os.getenv("POWER_INPUT_DIR", "/data/input"))
DEFAULT_OUTPUT_DIR = Path(os.getenv("POWER_OUTPUT_DIR", "/data/output"))
DEFAULT_DB_PATH = Path(os.getenv("POWER_DB_PATH", str(DEFAULT_OUTPUT_DIR / "power-history.db")))
DEFAULT_DATABASE_URL = (os.getenv("POWER_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
MAX_UPLOAD_BYTES = int(os.getenv("POWER_MAX_UPLOAD_MB", "25")) * 1024 * 1024
MAX_EXPORT_REPORT_BYTES = int(os.getenv("POWER_MAX_EXPORT_REPORT_MB", "25")) * 1024 * 1024
ALLOWED_SUFFIXES = {".xml", ".csv"}
STAFF_ROLES = ("Commissioner", "Analyst", "Investigator")
INVITE_EXPIRY_DAYS = 7
AUTH_RATE_LIMIT_WINDOW_SECONDS = 15 * 60
AUTH_RATE_LIMIT_MAX_ATTEMPTS = 5
AUTH_RATE_LIMIT_BLOCK_SECONDS = 15 * 60
EMAIL_VERIFICATION_EXPIRY_HOURS = 24
PASSWORD_RESET_EXPIRY_MINUTES = 30
MFA_ENROLLMENT_EXPIRY_MINUTES = 15
MFA_CHALLENGE_EXPIRY_MINUTES = 5
MFA_RECOVERY_CODE_COUNT = 10
MFA_RECOVERY_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MFA_ISSUER = "Home Energy Watch"
CUSTOMER_AUTH_TOKEN_PURPOSES = ("verify_email", "password_reset")
STAFF_AUTH_TOKEN_PURPOSES = ("password_reset",)
EMAIL_OUTBOX: list[dict[str, str]] = []
CUSTOMER_ACCESS_LEVELS = ("Viewer", "Manager")
ENERGY_COMPANY_GROUPS = (
    {
        "label": "Regulated electric companies",
        "companies": (
            "Duke Energy Carolinas, LLC",
            "Duke Energy Progress, LLC",
            "Dominion Energy North Carolina",
            "New River Light and Power Company",
            "Western Carolina University Power",
        ),
    },
    {
        "label": "Electric cooperatives",
        "companies": (
            "Haywood EMC",
            "PITT & GREENE EMC",
            "EDGECOMBE-MARTIN EMC",
            "FOUR COUNTY EMC",
            "BLUE RIDGE EMC",
            "RUTHERFORD EMC",
            "ROANOKE EMC",
            "MECKLENBURG EMC",
            "PIEDMONT EMC",
            "HALIFAX EMC",
            "BROAD RIVER EMC",
            "PEE DEE EMC",
            "RANDOLPH EMC",
            "UNION EMC",
            "BRUNSWICK EMC",
            "JONES-ONSLOW EMC",
            "FRENCH BROAD EMC",
            "WAKE EMC",
            "SURRY-YADKIN EMC",
            "TRI-COUNTY EMC",
            "LUMBEE RIVER EMC",
            "Mountain EMC",
            "SOUTH RIVER EMC",
            "CARTERET-CRAVEN EMC",
            "CENTRAL EMC",
            "Tri-State EMC",
            "TIDELAND EMC",
            "CAPE HATTERAS EMC",
            "ALBEMARLE EMC",
            "North Carolina EMC",
            "Blue Ridge Mountain EMC - Georgia",
            "ENERGYUNITED EMC",
        ),
    },
    {
        "label": "Public power providers",
        "companies": (
            "City of Albemarle",
            "Town of Apex",
            "Town of Ayden",
            "Town of Belhaven",
            "Town of Benson",
            "Town of Black Creek",
            "Town of Bostic",
            "City of Cherryville",
            "Town of Clayton",
            "City of Concord",
            "Town of Cornelius",
            "Dallas Electric Department",
            "Town of Drexel",
            "Town of Edenton",
            "City of Elizabeth City",
            "Town of Enfield",
            "Town of Farmville",
            "Fayetteville Public Works Commission",
            "Town of Forest City",
            "Town of Fountain",
            "Town of Fremont",
            "City of Gastonia",
            "Town of Granite Falls",
            "Greenville Utilities Commission",
            "Town of Hamilton",
            "Town of Hertford",
            "City of High Point",
            "Highlands Municipal Plant",
            "Town of Hobgood",
            "Town of Hookerton",
            "Town of Huntersville",
            "City of Kings Mountain",
            "City of Kinston",
            "Town of La Grange",
            "Town of Landis",
            "City of Laurinburg",
            "City of Lexington",
            "City of Lincolnton",
            "Town of Louisburg",
            "Town of Lucama",
            "City of Lumberton",
            "Town of Macclesfield",
            "Town of Maiden",
            "City of Monroe",
            "City of Morganton",
            "City of Murphy",
            "City of New Bern",
            "City of Newton",
            "Town of Oak City",
            "Town of Pikeville",
            "Town of Pinetops",
            "Pineville Electric Co.",
            "Town of Red Springs",
            "City of Robersonville",
            "City of Rocky Mount",
            "Town of Scotland Neck",
            "Town of Selma",
            "Town of Sharpsburg",
            "City of Shelby",
            "Town of Smithfield",
            "City of Southport",
            "Town of Stantonsburg",
            "City of Statesville",
            "Town of Tarboro",
            "Town of Wake Forest",
            "Town of Walstonburg",
            "City of Washington",
            "Town of Waynesville",
            "Wilson Energy",
            "Town of Windsor",
            "Town of Winterville",
            "University of North Carolina, Charlotte",
            "North Carolina State University",
        ),
    },
    {
        "label": "Other",
        "companies": (
            "Other North Carolina electric provider",
        ),
    },
)
ARCGIS_GEOCODER_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
NC_ELECTRIC_SERVICE_LAYERS = (
    {
        "url": "https://services1.arcgis.com/5xTVQvyyE1FPIlHH/arcgis/rest/services/Electric_Utilities/FeatureServer/0/query",
        "owner_field": "OWNER",
        "utility_id_field": "EIA_UTILIT",
    },
    {
        "url": "https://services1.arcgis.com/5xTVQvyyE1FPIlHH/arcgis/rest/services/Electric_Utilities/FeatureServer/1/query",
        "owner_field": "OWNER",
        "utility_id_field": "EIA_UTILIT",
    },
    {
        "url": "https://services1.arcgis.com/5xTVQvyyE1FPIlHH/arcgis/rest/services/Electric_Utilities/FeatureServer/2/query",
        "owner_field": "OWNER_1",
        "utility_id_field": "EIA_UTIL_1",
    },
)
ENERGY_COMPANY_BY_EIA_ID = {
    "19876": "Dominion Energy North Carolina",
    "5416": "Duke Energy Carolinas, LLC",
    "3046": "Duke Energy Progress, LLC",
    "13482": "New River Light and Power Company",
    "6235": "Fayetteville Public Works Commission",
    "7639": "Greenville Utilities Commission",
    "20785": "Wilson Energy",
}
DUKE_MY_ACCOUNT_URL = "https://www.duke-energy.com/my-account/sign-in"
GREEN_BUTTON_CONNECT_URL = "https://www.greenbuttonalliance.org/green-button-connect-my-data-cmd"
GREEN_BUTTON_DOWNLOAD_URL = "https://www.greenbuttonalliance.org/green-button-download-my-data-dmd"
NCUC_DATA_ACCESS_ORDER_URL = "https://starw1.ncuc.gov/NCUC/ViewFile.aspx?Id=b18eb0c3-6968-47d0-adbf-9f1b6ea8f680"
UTILITY_ACCESS_GUIDES = (
    {
        "id": "duke_download",
        "name": "Download your Duke history",
        "status": "Works now",
        "summary": "Sign in to Duke My Account, open your usage history, download the detailed interval file, then upload it here.",
        "action_label": "Go to Duke My Account",
        "action_url": DUKE_MY_ACCOUNT_URL,
        "secondary_label": "About Green Button files",
        "secondary_url": GREEN_BUTTON_DOWNLOAD_URL,
    },
    {
        "id": "green_button_connect",
        "name": "Green Button customer connection",
        "status": "Ready when Duke opens access",
        "summary": "This is the clean long-term path: the customer approves Home Energy Watch, the utility sends usage history, and the customer can revoke access.",
        "action_label": "See the Green Button connection standard",
        "action_url": GREEN_BUTTON_CONNECT_URL,
        "secondary_label": "",
        "secondary_url": "",
    },
    {
        "id": "ncuc_data_access",
        "name": "North Carolina data-access track",
        "status": "Watching the rule",
        "summary": "North Carolina has ordered a machine-readable customer data path. Home Energy Watch is built to use that path when the utility registration process is available.",
        "action_label": "Read the NCUC order",
        "action_url": NCUC_DATA_ACCESS_ORDER_URL,
        "secondary_label": "",
        "secondary_url": "",
    },
)

INPUT_DIR = DEFAULT_INPUT_DIR
OUTPUT_DIR = DEFAULT_OUTPUT_DIR
DB_PATH = DEFAULT_DB_PATH
DATABASE_URL = DEFAULT_DATABASE_URL
DEFAULT_ACCOUNT_NUMBER = "primary"
OPEN_METEO_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
SCHEMA_READY_TARGETS: set[str] = set()
POSTGRES_SCHEMA_LOCK_KEY = 104251906
POSTGRES_AUDIT_LOCK_KEY = 104251907
POSTGRES_STAFF_LOCK_KEY = 104251908
COMPARE_MAJOR_DELTA_KWH_PCT = 15.0
COMPARE_MAJOR_DELTA_BASELINE_KW = 0.25
COMPARE_MAJOR_DELTA_FLAGGED_NIGHTS = 2
WEATHER_HEAT_HIGH_F = float(os.getenv("POWER_WEATHER_HEAT_HIGH_F", "90"))
WEATHER_HEAT_APPARENT_F = float(os.getenv("POWER_WEATHER_HEAT_APPARENT_F", "95"))
WEATHER_HEAT_LOW_F = float(os.getenv("POWER_WEATHER_HEAT_LOW_F", "75"))
WEATHER_COLD_LOW_F = float(os.getenv("POWER_WEATHER_COLD_LOW_F", "32"))
WEATHER_COLD_HIGH_F = float(os.getenv("POWER_WEATHER_COLD_HIGH_F", "45"))
WEATHER_STORM_PRECIP_IN = float(os.getenv("POWER_WEATHER_STORM_PRECIP_IN", "0.5"))
WEATHER_STORM_WIND_MPH = float(os.getenv("POWER_WEATHER_STORM_WIND_MPH", "25"))
BILLING_PLAN_DEFINITIONS = (
    {
        "id": "home",
        "name": "Home Watch",
        "monthly_price_label": "Pricing being finalized",
        "account_limit": 1,
        "checkout_supported": True,
        "stripe_price_env": "STRIPE_PRICE_HOME",
        "summary": "For one household watching its own electric account.",
    },
    {
        "id": "review",
        "name": "Review Desk",
        "monthly_price_label": "Pricing being finalized",
        "account_limit": 20,
        "checkout_supported": True,
        "stripe_price_env": "STRIPE_PRICE_REVIEW",
        "summary": "For advocates and reviewers working across a small set of accounts.",
    },
    {
        "id": "agency",
        "name": "Agency Pilot",
        "monthly_price_label": "Talk with us",
        "account_limit": None,
        "checkout_supported": False,
        "stripe_price_env": "STRIPE_PRICE_AGENCY",
        "summary": "For a commission or agency review workspace.",
    },
)
DEFAULT_STRIPE_API_VERSION = "2026-02-25.clover"
DEFAULT_MARKETING_HOSTS = ("homeenergywatch.com", "www.homeenergywatch.com")
DEFAULT_APP_HOSTS = ("app.homeenergywatch.com",)
CURRENT_TERMS_VERSION = "2026-07-21"
CURRENT_PRIVACY_VERSION = "2026-07-21"
CURRENT_UTILITY_AUTHORIZATION_VERSION = "2026-07-21"
UTILITY_AUTHORIZATION_SCOPE = (
    "Import, store, analyze, compare, and report electricity usage for this account."
)


class DatabaseConnection:
    def __init__(self, raw_connection: Any, kind: str, target_label: str) -> None:
        self._raw_connection = raw_connection
        self.kind = kind
        self.target_label = target_label

    def __enter__(self) -> "DatabaseConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self._raw_connection.commit()
        else:
            self._raw_connection.rollback()
        self._raw_connection.close()
        return False

    def _translate_query(self, query: str) -> str:
        if self.kind == "postgres":
            return query.replace("?", "%s")
        return query

    def execute(self, query: str, params: tuple | list | None = None):
        cursor = self._raw_connection.cursor()
        cursor.execute(self._translate_query(query), params or ())
        return cursor

    def executemany(self, query: str, seq_of_params):
        cursor = self._raw_connection.cursor()
        cursor.executemany(self._translate_query(query), seq_of_params)
        return cursor

    def commit(self) -> None:
        self._raw_connection.commit()

    def rollback(self) -> None:
        self._raw_connection.rollback()

    def close(self) -> None:
        self._raw_connection.close()


def normalize_database_url(value: str | None) -> str:
    normalized = (value or "").strip()
    if normalized.startswith("postgres://"):
        return "postgresql://" + normalized[len("postgres://") :]
    return normalized


def sqlite_path_from_url(database_url: str) -> Path:
    parsed = urlsplit(database_url)
    path_text = unquote(parsed.path or "")
    if not path_text:
        raise ValueError("Use a file path when POWER_DATABASE_URL points at SQLite.")
    if parsed.netloc:
        path_text = f"//{parsed.netloc}{path_text}"
    return Path(path_text)


def redact_database_url(database_url: str) -> str:
    parsed = urlsplit(database_url)
    hostname = parsed.hostname or "localhost"
    port = f":{parsed.port}" if parsed.port else ""
    user = f"{parsed.username}@" if parsed.username else ""
    path = parsed.path or ""
    return f"{parsed.scheme}://{user}{hostname}{port}{path}"


def get_database_settings() -> dict[str, Any]:
    database_url = normalize_database_url(os.getenv("POWER_DATABASE_URL") or os.getenv("DATABASE_URL") or DATABASE_URL)
    if not database_url:
        return {
            "kind": "sqlite",
            "path": DB_PATH,
            "target_label": DB_PATH.as_posix(),
        }

    if database_url.startswith("sqlite:///"):
        sqlite_path = sqlite_path_from_url(database_url)
        return {
            "kind": "sqlite",
            "path": sqlite_path,
            "target_label": sqlite_path.as_posix(),
        }

    if database_url.startswith("postgresql://"):
        return {
            "kind": "postgres",
            "dsn": database_url,
            "target_label": redact_database_url(database_url),
        }

    raise ValueError("POWER_DATABASE_URL must use sqlite:/// or postgresql://.")


def build_database_status() -> dict[str, str]:
    settings = get_database_settings()
    return {
        "database_backend": str(settings["kind"]),
        "database_target": str(settings["target_label"]),
    }


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze Green Button or Duke-style interval XML for unusual overnight load."
    )
    parser.add_argument("--input", "-i", help="Path to a Green Button or Duke interval XML file")
    parser.add_argument(
        "--compare-to",
        help="Optional second Duke interval XML file to compare against --input",
    )
    parser.add_argument(
        "--output",
        "-o",
        default=DEFAULT_CLI_OUTPUT,
        help="Path to save the single-file CSV report or compare artifact",
    )
    parser.add_argument("--tz", default=DEFAULT_TZ, help="Timezone for interpreting timestamps")
    parser.add_argument(
        "--account-number",
        help="Saved account number to use when adding cached weather context to single-file exports",
    )
    parser.add_argument("--night-start", type=str, default=DEFAULT_NIGHT_START, help="Night window start (HH:MM)")
    parser.add_argument("--night-end", type=str, default=DEFAULT_NIGHT_END, help="Night window end (HH:MM)")
    parser.add_argument(
        "--min-night-kw",
        type=float,
        default=DEFAULT_MIN_NIGHT_KW,
        help="Minimum average kW at night to flag a day",
    )
    parser.add_argument(
        "--night-multiplier",
        type=float,
        default=DEFAULT_NIGHT_MULTIPLIER,
        help="Flag a day when night average kW exceeds baseline times this multiplier",
    )
    parser.add_argument("--serve", action="store_true", help="Run the local web app instead of a one-shot report")
    parser.add_argument(
        "--sync-utilities",
        action="store_true",
        help="Sync saved utility connections once and exit",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Web host to bind when using --serve")
    parser.add_argument("--port", type=int, default=8000, help="Web port to bind when using --serve")
    return parser


def ensure_data_dirs() -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def normalize_account_number(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized or DEFAULT_ACCOUNT_NUMBER


def normalize_optional_date(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        return ddate.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError("Choose a valid baseline date.") from exc


def timestamp_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_database() -> None:
    ensure_data_dirs()
    with get_db_connection(ensure_schema=False) as conn:
        ensure_schema_ready(conn)


def ensure_schema_ready(conn: DatabaseConnection) -> None:
    target_label = str(conn.target_label)
    if target_label in SCHEMA_READY_TARGETS:
        return
    try:
        if conn.kind == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(?)", (POSTGRES_SCHEMA_LOCK_KEY,))
        migrate_database(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    SCHEMA_READY_TARGETS.add(target_label)


def get_db_connection(*, ensure_schema: bool = True) -> DatabaseConnection:
    settings = get_database_settings()
    if settings["kind"] == "postgres":
        if psycopg is None or dict_row is None:
            raise RuntimeError("Install psycopg to use POWER_DATABASE_URL with Postgres.")
        conn = DatabaseConnection(
            psycopg.connect(settings["dsn"], row_factory=dict_row),
            kind="postgres",
            target_label=settings["target_label"],
        )
    else:
        sqlite_path = Path(settings["path"])
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        raw_connection = sqlite3.connect(sqlite_path)
        raw_connection.row_factory = sqlite3.Row
        conn = DatabaseConnection(raw_connection, kind="sqlite", target_label=settings["target_label"])
    if ensure_schema and str(settings["target_label"]) not in SCHEMA_READY_TARGETS:
        try:
            ensure_schema_ready(conn)
        except Exception:
            conn.close()
            raise
    return conn


def table_exists(conn: DatabaseConnection, name: str) -> bool:
    if conn.kind == "postgres":
        row = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = ?
            ) AS exists
            """,
            (name,),
        ).fetchone()
        return bool(row["exists"]) if row is not None else False
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def table_columns(conn: DatabaseConnection, name: str) -> set[str]:
    if not table_exists(conn, name):
        return set()
    if conn.kind == "postgres":
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            ORDER BY ordinal_position
            """,
            (name,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})").fetchall()}


def ensure_accounts_table(conn: DatabaseConnection) -> int:
    id_column = "BIGSERIAL PRIMARY KEY" if conn.kind == "postgres" else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS accounts (
            id {id_column},
            account_number TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            energy_company TEXT,
            baseline_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    account_columns = table_columns(conn, "accounts")
    if "energy_company" not in account_columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN energy_company TEXT")
    timestamp = timestamp_now()
    conn.execute(
        """
        INSERT INTO accounts (account_number, display_name, baseline_date, created_at, updated_at)
        VALUES (?, ?, NULL, ?, ?)
        ON CONFLICT(account_number) DO NOTHING
        """,
        (DEFAULT_ACCOUNT_NUMBER, "Primary account", timestamp, timestamp),
    )
    row = conn.execute(
        "SELECT id FROM accounts WHERE account_number = ?",
        (DEFAULT_ACCOUNT_NUMBER,),
    ).fetchone()
    return int(row["id"] if conn.kind == "postgres" else row[0])


def migrate_database_postgres(conn: DatabaseConnection) -> None:
    ensure_accounts_table(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS imported_files (
            account_id BIGINT NOT NULL,
            path TEXT NOT NULL,
            modified_time DOUBLE PRECISION NOT NULL,
            interval_count INTEGER NOT NULL,
            imported_at TEXT NOT NULL,
            service_point_id TEXT,
            PRIMARY KEY (account_id, path),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interval_readings (
            account_id BIGINT NOT NULL,
            start_epoch BIGINT NOT NULL,
            duration_s INTEGER NOT NULL,
            wh DOUBLE PRECISION NOT NULL,
            source_path TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            PRIMARY KEY (account_id, start_epoch, duration_s),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_interval_readings_account_start_epoch
        ON interval_readings (account_id, start_epoch)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_load_items (
            id BIGSERIAL PRIMARY KEY,
            account_id BIGINT NOT NULL,
            label TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            watts_each DOUBLE PRECISION NOT NULL,
            include_when_off INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_load_items_account_id
        ON account_load_items (account_id, id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS household_profiles (
            account_id BIGINT PRIMARY KEY,
            address TEXT,
            zip_code TEXT,
            occupant_count INTEGER,
            year_built INTEGER,
            square_footage INTEGER,
            heating_system TEXT,
            cooling_system TEXT,
            water_heater TEXT,
            notes TEXT,
            updated_at TEXT NOT NULL,
            latitude DOUBLE PRECISION,
            longitude DOUBLE PRECISION,
            weather_location TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute("ALTER TABLE household_profiles ADD COLUMN IF NOT EXISTS zip_code TEXT")
    conn.execute("ALTER TABLE household_profiles ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION")
    conn.execute("ALTER TABLE household_profiles ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION")
    conn.execute("ALTER TABLE household_profiles ADD COLUMN IF NOT EXISTS weather_location TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_daily_cache (
            account_id BIGINT NOT NULL,
            weather_date TEXT NOT NULL,
            latitude DOUBLE PRECISION NOT NULL,
            longitude DOUBLE PRECISION NOT NULL,
            timezone TEXT NOT NULL,
            location_name TEXT,
            data_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (account_id, weather_date),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_access_emails (
            id BIGSERIAL PRIMARY KEY,
            account_id BIGINT NOT NULL,
            email TEXT NOT NULL,
            full_name TEXT,
            access_level TEXT NOT NULL DEFAULT 'Viewer',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id, email),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_access_emails_account_id
        ON account_access_emails (account_id, email)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS utility_connections (
            id BIGSERIAL PRIMARY KEY,
            account_id BIGINT NOT NULL,
            provider_name TEXT NOT NULL,
            connection_label TEXT NOT NULL,
            access_method TEXT NOT NULL,
            access_identifier TEXT,
            secret_hash TEXT,
            secret_token TEXT,
            secret_last4 TEXT,
            status TEXT NOT NULL,
            last_sync_at TEXT,
            last_sync_status TEXT,
            last_sync_error TEXT,
            last_sync_attempt_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute("ALTER TABLE utility_connections ADD COLUMN IF NOT EXISTS secret_token TEXT")
    conn.execute("ALTER TABLE utility_connections ADD COLUMN IF NOT EXISTS last_sync_status TEXT")
    conn.execute("ALTER TABLE utility_connections ADD COLUMN IF NOT EXISTS last_sync_error TEXT")
    conn.execute("ALTER TABLE utility_connections ADD COLUMN IF NOT EXISTS last_sync_attempt_at TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_utility_connections_account_id
        ON utility_connections (account_id, provider_name)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_users (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL,
            password_hash TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            invite_token TEXT,
            invite_token_hash TEXT,
            invite_expires_at TEXT,
            auth_version INTEGER NOT NULL DEFAULT 1,
            mfa_secret_token TEXT,
            mfa_pending_secret_token TEXT,
            mfa_pending_created_at TEXT,
            mfa_enabled_at TEXT,
            mfa_last_counter BIGINT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        )
        """
    )
    conn.execute("ALTER TABLE staff_users ADD COLUMN IF NOT EXISTS invite_token_hash TEXT")
    conn.execute("ALTER TABLE staff_users ADD COLUMN IF NOT EXISTS auth_version INTEGER NOT NULL DEFAULT 1")
    conn.execute("ALTER TABLE staff_users ADD COLUMN IF NOT EXISTS mfa_secret_token TEXT")
    conn.execute("ALTER TABLE staff_users ADD COLUMN IF NOT EXISTS mfa_pending_secret_token TEXT")
    conn.execute("ALTER TABLE staff_users ADD COLUMN IF NOT EXISTS mfa_pending_created_at TEXT")
    conn.execute("ALTER TABLE staff_users ADD COLUMN IF NOT EXISTS mfa_enabled_at TEXT")
    conn.execute("ALTER TABLE staff_users ADD COLUMN IF NOT EXISTS mfa_last_counter BIGINT")
    legacy_invites = conn.execute(
        "SELECT id, invite_token FROM staff_users WHERE invite_token IS NOT NULL AND invite_token_hash IS NULL"
    ).fetchall()
    for legacy_invite in legacy_invites:
        conn.execute(
            "UPDATE staff_users SET invite_token = NULL, invite_token_hash = ? WHERE id = ?",
            (staff_invite_token_hash(str(legacy_invite["invite_token"])), int(legacy_invite["id"])),
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_auth_tokens (
            id BIGSERIAL PRIMARY KEY,
            staff_user_id BIGINT NOT NULL,
            purpose TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            requested_ip_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(staff_user_id) REFERENCES staff_users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_auth_tokens_user_purpose
        ON staff_auth_tokens (staff_user_id, purpose, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_mfa_recovery_codes (
            id BIGSERIAL PRIMARY KEY,
            staff_user_id BIGINT NOT NULL,
            code_hash TEXT NOT NULL UNIQUE,
            consumed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(staff_user_id) REFERENCES staff_users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_mfa_recovery_user
        ON staff_mfa_recovery_codes (staff_user_id, consumed_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_users (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            email_verified_at TEXT,
            auth_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        )
        """
    )
    customer_columns = table_columns(conn, "customer_users")
    if "email_verified_at" not in customer_columns:
        conn.execute("ALTER TABLE customer_users ADD COLUMN email_verified_at TEXT")
        conn.execute("UPDATE customer_users SET email_verified_at = created_at WHERE email_verified_at IS NULL")
    if "auth_version" not in customer_columns:
        conn.execute("ALTER TABLE customer_users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_policy_acceptances (
            id BIGSERIAL PRIMARY KEY,
            customer_user_id BIGINT NOT NULL,
            terms_version TEXT NOT NULL,
            privacy_version TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            remote_hash TEXT,
            user_agent_hash TEXT,
            UNIQUE(customer_user_id, terms_version, privacy_version),
            FOREIGN KEY(customer_user_id) REFERENCES customer_users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_data_authorizations (
            id BIGSERIAL PRIMARY KEY,
            account_id BIGINT NOT NULL,
            customer_user_id BIGINT NOT NULL,
            authorization_version TEXT NOT NULL,
            authorization_scope TEXT NOT NULL,
            status TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            revoked_at TEXT,
            remote_hash TEXT,
            user_agent_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY(customer_user_id) REFERENCES customer_users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_data_authorizations_account_status
        ON account_data_authorizations (account_id, status, updated_at)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_account_data_authorizations_active_user
        ON account_data_authorizations (account_id, customer_user_id)
        WHERE status = 'active'
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_auth_tokens (
            id BIGSERIAL PRIMARY KEY,
            customer_user_id BIGINT NOT NULL,
            purpose TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            requested_ip_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(customer_user_id) REFERENCES customer_users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_auth_tokens_user_purpose
        ON customer_auth_tokens (customer_user_id, purpose, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_rate_limits (
            scope TEXT NOT NULL,
            identity_hash TEXT NOT NULL,
            window_start_epoch BIGINT NOT NULL,
            attempt_count INTEGER NOT NULL,
            blocked_until_epoch BIGINT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, identity_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_artifacts (
            filename TEXT PRIMARY KEY,
            account_id BIGINT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_report_artifacts_account_id
        ON report_artifacts (account_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id BIGSERIAL PRIMARY KEY,
            occurred_at TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id BIGINT,
            action TEXT NOT NULL,
            account_id BIGINT,
            target_type TEXT,
            target_id TEXT,
            request_id TEXT NOT NULL,
            remote_hash TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            previous_hash TEXT,
            event_hash TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS previous_hash TEXT")
    conn.execute("ALTER TABLE audit_events ADD COLUMN IF NOT EXISTS event_hash TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_account_time
        ON audit_events (account_id, occurred_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_action_time
        ON audit_events (action, occurred_at)
        """
    )
    backfill_audit_event_hashes(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_billing (
            customer_user_id BIGINT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            subscription_status TEXT NOT NULL,
            checkout_session_id TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            stripe_payment_intent_id TEXT,
            stripe_receipt_url TEXT,
            payments_customer_id TEXT,
            payments_order_id TEXT,
            payments_checkout_session_id TEXT,
            payments_receipt_id TEXT,
            current_period_end TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(customer_user_id) REFERENCES customer_users(id)
        )
        """
    )
    conn.execute("ALTER TABLE customer_billing ADD COLUMN IF NOT EXISTS payments_customer_id TEXT")
    conn.execute("ALTER TABLE customer_billing ADD COLUMN IF NOT EXISTS payments_order_id TEXT")
    conn.execute("ALTER TABLE customer_billing ADD COLUMN IF NOT EXISTS payments_checkout_session_id TEXT")
    conn.execute("ALTER TABLE customer_billing ADD COLUMN IF NOT EXISTS payments_receipt_id TEXT")
    conn.execute("ALTER TABLE customer_billing ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT")
    conn.execute("ALTER TABLE customer_billing ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT")
    conn.execute("ALTER TABLE customer_billing ADD COLUMN IF NOT EXISTS stripe_payment_intent_id TEXT")
    conn.execute("ALTER TABLE customer_billing ADD COLUMN IF NOT EXISTS stripe_receipt_url TEXT")


def migrate_database(conn: DatabaseConnection) -> None:
    if conn.kind == "postgres":
        migrate_database_postgres(conn)
        return

    default_account_id = ensure_accounts_table(conn)
    imported_columns = table_columns(conn, "imported_files")
    needs_imported_migration = (
        not imported_columns
        or "account_id" not in imported_columns
        or "service_point_id" not in imported_columns
        or len(conn.execute("PRAGMA index_list(imported_files)").fetchall()) == 0
    )
    if needs_imported_migration:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_files_new (
                account_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                modified_time REAL NOT NULL,
                interval_count INTEGER NOT NULL,
                imported_at TEXT NOT NULL,
                service_point_id TEXT,
                PRIMARY KEY (account_id, path),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
            """
        )
        if imported_columns:
            conn.execute(
                """
                INSERT OR REPLACE INTO imported_files_new (
                    path, account_id, modified_time, interval_count, imported_at, service_point_id
                )
                SELECT path, ?, modified_time, interval_count, imported_at, NULL
                FROM imported_files
                """,
                (default_account_id,),
            )
            conn.execute("DROP TABLE imported_files")
        conn.execute("ALTER TABLE imported_files_new RENAME TO imported_files")
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_files (
                account_id INTEGER NOT NULL,
                path TEXT NOT NULL,
                modified_time REAL NOT NULL,
                interval_count INTEGER NOT NULL,
                imported_at TEXT NOT NULL,
                service_point_id TEXT,
                PRIMARY KEY (account_id, path),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
            """
        )

    interval_columns = table_columns(conn, "interval_readings")
    if not interval_columns or "account_id" not in interval_columns:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interval_readings_new (
                account_id INTEGER NOT NULL,
                start_epoch INTEGER NOT NULL,
                duration_s INTEGER NOT NULL,
                wh REAL NOT NULL,
                source_path TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (account_id, start_epoch, duration_s),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
            """
        )
        if interval_columns:
            conn.execute(
                """
                INSERT OR REPLACE INTO interval_readings_new (
                    account_id, start_epoch, duration_s, wh, source_path, imported_at
                )
                SELECT ?, start_epoch, duration_s, wh, source_path, imported_at
                FROM interval_readings
                """,
                (default_account_id,),
            )
            conn.execute("DROP TABLE interval_readings")
        conn.execute("ALTER TABLE interval_readings_new RENAME TO interval_readings")
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interval_readings (
                account_id INTEGER NOT NULL,
                start_epoch INTEGER NOT NULL,
                duration_s INTEGER NOT NULL,
                wh REAL NOT NULL,
                source_path TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY (account_id, start_epoch, duration_s),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
            """
        )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_interval_readings_account_start_epoch
        ON interval_readings (account_id, start_epoch)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_load_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            watts_each REAL NOT NULL,
            include_when_off INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_load_items_account_id
        ON account_load_items (account_id, id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS household_profiles (
            account_id INTEGER PRIMARY KEY,
            address TEXT,
            zip_code TEXT,
            occupant_count INTEGER,
            year_built INTEGER,
            square_footage INTEGER,
            heating_system TEXT,
            cooling_system TEXT,
            water_heater TEXT,
            notes TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    household_columns = table_columns(conn, "household_profiles")
    if "zip_code" not in household_columns:
        conn.execute("ALTER TABLE household_profiles ADD COLUMN zip_code TEXT")
    if "latitude" not in household_columns:
        conn.execute("ALTER TABLE household_profiles ADD COLUMN latitude REAL")
    if "longitude" not in household_columns:
        conn.execute("ALTER TABLE household_profiles ADD COLUMN longitude REAL")
    if "weather_location" not in household_columns:
        conn.execute("ALTER TABLE household_profiles ADD COLUMN weather_location TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS weather_daily_cache (
            account_id INTEGER NOT NULL,
            weather_date TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            timezone TEXT NOT NULL,
            location_name TEXT,
            data_json TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (account_id, weather_date),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_access_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            full_name TEXT,
            access_level TEXT NOT NULL DEFAULT 'Viewer',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(account_id, email),
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_access_emails_account_id
        ON account_access_emails (account_id, email)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS utility_connections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            provider_name TEXT NOT NULL,
            connection_label TEXT NOT NULL,
            access_method TEXT NOT NULL,
            access_identifier TEXT,
            secret_hash TEXT,
            secret_token TEXT,
            secret_last4 TEXT,
            status TEXT NOT NULL,
            last_sync_at TEXT,
            last_sync_status TEXT,
            last_sync_error TEXT,
            last_sync_attempt_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    utility_columns = table_columns(conn, "utility_connections")
    if "secret_token" not in utility_columns:
        conn.execute("ALTER TABLE utility_connections ADD COLUMN secret_token TEXT")
    if "last_sync_status" not in utility_columns:
        conn.execute("ALTER TABLE utility_connections ADD COLUMN last_sync_status TEXT")
    if "last_sync_error" not in utility_columns:
        conn.execute("ALTER TABLE utility_connections ADD COLUMN last_sync_error TEXT")
    if "last_sync_attempt_at" not in utility_columns:
        conn.execute("ALTER TABLE utility_connections ADD COLUMN last_sync_attempt_at TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_utility_connections_account_id
        ON utility_connections (account_id, provider_name)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            role TEXT NOT NULL,
            password_hash TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            invite_token TEXT,
            invite_token_hash TEXT,
            invite_expires_at TEXT,
            auth_version INTEGER NOT NULL DEFAULT 1,
            mfa_secret_token TEXT,
            mfa_pending_secret_token TEXT,
            mfa_pending_created_at TEXT,
            mfa_enabled_at TEXT,
            mfa_last_counter INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        )
        """
    )
    staff_columns = table_columns(conn, "staff_users")
    if "invite_token_hash" not in staff_columns:
        conn.execute("ALTER TABLE staff_users ADD COLUMN invite_token_hash TEXT")
    if "auth_version" not in staff_columns:
        conn.execute("ALTER TABLE staff_users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 1")
    if "mfa_secret_token" not in staff_columns:
        conn.execute("ALTER TABLE staff_users ADD COLUMN mfa_secret_token TEXT")
    if "mfa_pending_secret_token" not in staff_columns:
        conn.execute("ALTER TABLE staff_users ADD COLUMN mfa_pending_secret_token TEXT")
    if "mfa_pending_created_at" not in staff_columns:
        conn.execute("ALTER TABLE staff_users ADD COLUMN mfa_pending_created_at TEXT")
    if "mfa_enabled_at" not in staff_columns:
        conn.execute("ALTER TABLE staff_users ADD COLUMN mfa_enabled_at TEXT")
    if "mfa_last_counter" not in staff_columns:
        conn.execute("ALTER TABLE staff_users ADD COLUMN mfa_last_counter INTEGER")
    legacy_invites = conn.execute(
        "SELECT id, invite_token FROM staff_users WHERE invite_token IS NOT NULL AND invite_token_hash IS NULL"
    ).fetchall()
    for legacy_invite in legacy_invites:
        conn.execute(
            "UPDATE staff_users SET invite_token = NULL, invite_token_hash = ? WHERE id = ?",
            (staff_invite_token_hash(str(legacy_invite["invite_token"])), int(legacy_invite["id"])),
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_auth_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_user_id INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            requested_ip_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(staff_user_id) REFERENCES staff_users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_auth_tokens_user_purpose
        ON staff_auth_tokens (staff_user_id, purpose, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS staff_mfa_recovery_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_user_id INTEGER NOT NULL,
            code_hash TEXT NOT NULL UNIQUE,
            consumed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(staff_user_id) REFERENCES staff_users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_staff_mfa_recovery_user
        ON staff_mfa_recovery_codes (staff_user_id, consumed_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            email_verified_at TEXT,
            auth_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT
        )
        """
    )
    customer_columns = table_columns(conn, "customer_users")
    if "email_verified_at" not in customer_columns:
        conn.execute("ALTER TABLE customer_users ADD COLUMN email_verified_at TEXT")
        conn.execute("UPDATE customer_users SET email_verified_at = created_at WHERE email_verified_at IS NULL")
    if "auth_version" not in customer_columns:
        conn.execute("ALTER TABLE customer_users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_policy_acceptances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_user_id INTEGER NOT NULL,
            terms_version TEXT NOT NULL,
            privacy_version TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            remote_hash TEXT,
            user_agent_hash TEXT,
            UNIQUE(customer_user_id, terms_version, privacy_version),
            FOREIGN KEY(customer_user_id) REFERENCES customer_users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account_data_authorizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id INTEGER NOT NULL,
            customer_user_id INTEGER NOT NULL,
            authorization_version TEXT NOT NULL,
            authorization_scope TEXT NOT NULL,
            status TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            revoked_at TEXT,
            remote_hash TEXT,
            user_agent_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY(customer_user_id) REFERENCES customer_users(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_data_authorizations_account_status
        ON account_data_authorizations (account_id, status, updated_at)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_account_data_authorizations_active_user
        ON account_data_authorizations (account_id, customer_user_id)
        WHERE status = 'active'
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_auth_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_user_id INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            requested_ip_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(customer_user_id) REFERENCES customer_users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_customer_auth_tokens_user_purpose
        ON customer_auth_tokens (customer_user_id, purpose, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_rate_limits (
            scope TEXT NOT NULL,
            identity_hash TEXT NOT NULL,
            window_start_epoch INTEGER NOT NULL,
            attempt_count INTEGER NOT NULL,
            blocked_until_epoch INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, identity_hash)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_artifacts (
            filename TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_report_artifacts_account_id
        ON report_artifacts (account_id, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            actor_type TEXT NOT NULL,
            actor_id INTEGER,
            action TEXT NOT NULL,
            account_id INTEGER,
            target_type TEXT,
            target_id TEXT,
            request_id TEXT NOT NULL,
            remote_hash TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            previous_hash TEXT,
            event_hash TEXT,
            FOREIGN KEY(account_id) REFERENCES accounts(id)
        )
        """
    )
    audit_columns = table_columns(conn, "audit_events")
    if "previous_hash" not in audit_columns:
        conn.execute("ALTER TABLE audit_events ADD COLUMN previous_hash TEXT")
    if "event_hash" not in audit_columns:
        conn.execute("ALTER TABLE audit_events ADD COLUMN event_hash TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_account_time
        ON audit_events (account_id, occurred_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_action_time
        ON audit_events (action, occurred_at)
        """
    )
    backfill_audit_event_hashes(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_billing (
            customer_user_id INTEGER PRIMARY KEY,
            plan_id TEXT NOT NULL,
            subscription_status TEXT NOT NULL,
            checkout_session_id TEXT,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            stripe_payment_intent_id TEXT,
            stripe_receipt_url TEXT,
            payments_customer_id TEXT,
            payments_order_id TEXT,
            payments_checkout_session_id TEXT,
            payments_receipt_id TEXT,
            current_period_end TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(customer_user_id) REFERENCES customer_users(id)
        )
        """
    )
    billing_columns = table_columns(conn, "customer_billing")
    if "payments_customer_id" not in billing_columns:
        conn.execute("ALTER TABLE customer_billing ADD COLUMN payments_customer_id TEXT")
    if "payments_order_id" not in billing_columns:
        conn.execute("ALTER TABLE customer_billing ADD COLUMN payments_order_id TEXT")
    if "payments_checkout_session_id" not in billing_columns:
        conn.execute("ALTER TABLE customer_billing ADD COLUMN payments_checkout_session_id TEXT")
    if "payments_receipt_id" not in billing_columns:
        conn.execute("ALTER TABLE customer_billing ADD COLUMN payments_receipt_id TEXT")
    if "stripe_customer_id" not in billing_columns:
        conn.execute("ALTER TABLE customer_billing ADD COLUMN stripe_customer_id TEXT")
    if "stripe_subscription_id" not in billing_columns:
        conn.execute("ALTER TABLE customer_billing ADD COLUMN stripe_subscription_id TEXT")
    if "stripe_payment_intent_id" not in billing_columns:
        conn.execute("ALTER TABLE customer_billing ADD COLUMN stripe_payment_intent_id TEXT")
    if "stripe_receipt_url" not in billing_columns:
        conn.execute("ALTER TABLE customer_billing ADD COLUMN stripe_receipt_url TEXT")


def clean_email(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise ValueError("Enter a valid email address.")
    return normalized


def clean_password(value: str | None) -> str:
    password = value or ""
    if len(password) < 10:
        raise ValueError("Use at least 10 characters for the password.")
    return password


class EmailVerificationRequired(ValueError):
    def __init__(self, customer_user: dict[str, object]):
        super().__init__("Confirm your email address before signing in.")
        self.customer_user = customer_user


def runtime_environment() -> str:
    return (os.getenv("POWER_ENV") or "development").strip().lower()


def is_production_environment() -> bool:
    return runtime_environment() == "production"


def get_app_secret() -> str:
    return (os.getenv("POWER_APP_SECRET") or DEFAULT_APP_SECRET).strip()


def get_audit_signing_key() -> str:
    configured = (os.getenv("POWER_AUDIT_SIGNING_KEY") or "").strip()
    if configured:
        return configured
    if is_production_environment():
        raise RuntimeError("POWER_AUDIT_SIGNING_KEY is required in production.")
    return f"local-audit:{get_app_secret()}"


def email_delivery_backend() -> str:
    return (os.getenv("POWER_EMAIL_BACKEND") or "disabled").strip().lower()


def email_verification_required() -> bool:
    return email_delivery_backend() != "disabled"


def get_email_sender() -> str:
    return (os.getenv("POWER_EMAIL_FROM") or "").strip()


def get_email_reply_to() -> str:
    return (os.getenv("POWER_EMAIL_REPLY_TO") or "").strip()


def get_email_region() -> str:
    return (os.getenv("POWER_EMAIL_REGION") or "us-east-1").strip()


def send_transactional_email(recipient: str, subject: str, text_body: str) -> None:
    normalized_recipient = clean_email(recipient)
    backend = email_delivery_backend()
    if backend == "memory":
        if is_production_environment():
            raise RuntimeError("The in-memory email backend cannot run in production.")
        EMAIL_OUTBOX.append(
            {
                "recipient": normalized_recipient,
                "subject": subject,
                "text_body": text_body,
            }
        )
        return
    if backend != "ses":
        raise RuntimeError("Email delivery is not configured.")
    if boto3 is None:
        raise RuntimeError("Install boto3 to send account email through Amazon SES.")
    sender = get_email_sender()
    if not sender:
        raise RuntimeError("POWER_EMAIL_FROM is required for email delivery.")
    message: dict[str, object] = {
        "FromEmailAddress": sender,
        "Destination": {"ToAddresses": [normalized_recipient]},
        "Content": {
            "Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": text_body, "Charset": "UTF-8"}},
            }
        },
    }
    reply_to = get_email_reply_to()
    if reply_to:
        message["ReplyToAddresses"] = [reply_to]
    boto3.client("sesv2", region_name=get_email_region()).send_email(**message)


def get_data_encryption_cipher() -> Fernet:
    key = (os.getenv("POWER_DATA_ENCRYPTION_KEY") or "").strip()
    if not key:
        if is_production_environment():
            raise RuntimeError("POWER_DATA_ENCRYPTION_KEY is required in production.")
        digest = hashlib.sha256(f"home-energy-watch:{get_app_secret()}".encode("utf-8")).digest()
        key = base64.urlsafe_b64encode(digest).decode("ascii")
    try:
        return Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("POWER_DATA_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def validate_runtime_security() -> None:
    if not is_production_environment():
        return
    app_secret = get_app_secret()
    disallowed = {DEFAULT_APP_SECRET, "replace-with-a-long-random-secret", "change-me"}
    if app_secret in disallowed or len(app_secret) < 32:
        raise RuntimeError("POWER_APP_SECRET must be a unique value of at least 32 characters in production.")
    audit_signing_key = get_audit_signing_key()
    if len(audit_signing_key) < 32:
        raise RuntimeError("POWER_AUDIT_SIGNING_KEY must contain at least 32 characters in production.")
    public_base_url = (os.getenv("POWER_PUBLIC_BASE_URL") or "").strip()
    if not public_base_url.startswith("https://"):
        raise RuntimeError("POWER_PUBLIC_BASE_URL must use HTTPS in production.")
    database_url = (os.getenv("POWER_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    parsed_database_url = urlsplit(database_url)
    if parsed_database_url.scheme not in {"postgres", "postgresql"} or not parsed_database_url.hostname:
        raise RuntimeError("POWER_DATABASE_URL must point to Postgres in production.")
    sslmode = parse_qs(parsed_database_url.query).get("sslmode", [""])[0].lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise RuntimeError("POWER_DATABASE_URL must require TLS in production.")
    get_data_encryption_cipher()
    if email_delivery_backend() != "ses":
        raise RuntimeError("POWER_EMAIL_BACKEND must be ses in production.")
    sender = get_email_sender()
    if "@" not in sender:
        raise RuntimeError("POWER_EMAIL_FROM must be a valid sender in production.")
    if boto3 is None:
        raise RuntimeError("boto3 is required for production email delivery.")


def auth_rate_limit_identity(scope: str, email: str | None, remote_address: str | None) -> str:
    normalized_email = (email or "").strip().lower()
    identity = f"{scope}:{normalized_email}:{remote_address or 'unknown'}"
    return hmac.new(get_app_secret().encode("utf-8"), identity.encode("utf-8"), hashlib.sha256).hexdigest()


def auth_rate_limit_status(scope: str, identity_hash: str, now_epoch: int | None = None) -> dict[str, int | bool]:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT window_start_epoch, attempt_count, blocked_until_epoch
            FROM auth_rate_limits
            WHERE scope = ? AND identity_hash = ?
            """,
            (scope, identity_hash),
        ).fetchone()
    if row is None:
        return {"blocked": False, "retry_after": 0, "attempt_count": 0}
    blocked_until = int(row["blocked_until_epoch"] or 0)
    return {
        "blocked": blocked_until > now,
        "retry_after": max(0, blocked_until - now),
        "attempt_count": int(row["attempt_count"]),
    }


def record_auth_failure(scope: str, identity_hash: str, now_epoch: int | None = None) -> dict[str, int | bool]:
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT window_start_epoch, attempt_count, blocked_until_epoch
            FROM auth_rate_limits
            WHERE scope = ? AND identity_hash = ?
            """,
            (scope, identity_hash),
        ).fetchone()
        if row is None or now - int(row["window_start_epoch"]) >= AUTH_RATE_LIMIT_WINDOW_SECONDS:
            window_start = now
            attempt_count = 1
        else:
            window_start = int(row["window_start_epoch"])
            attempt_count = int(row["attempt_count"]) + 1
        blocked_until = now + AUTH_RATE_LIMIT_BLOCK_SECONDS if attempt_count >= AUTH_RATE_LIMIT_MAX_ATTEMPTS else None
        conn.execute(
            """
            INSERT INTO auth_rate_limits (
                scope, identity_hash, window_start_epoch, attempt_count, blocked_until_epoch, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope, identity_hash) DO UPDATE SET
                window_start_epoch = excluded.window_start_epoch,
                attempt_count = excluded.attempt_count,
                blocked_until_epoch = excluded.blocked_until_epoch,
                updated_at = excluded.updated_at
            """,
            (scope, identity_hash, window_start, attempt_count, blocked_until, timestamp_now()),
        )
        conn.commit()
    return {
        "blocked": blocked_until is not None,
        "retry_after": AUTH_RATE_LIMIT_BLOCK_SECONDS if blocked_until is not None else 0,
        "attempt_count": attempt_count,
    }


def clear_auth_failures(scope: str, identity_hash: str) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "DELETE FROM auth_rate_limits WHERE scope = ? AND identity_hash = ?",
            (scope, identity_hash),
        )
        conn.commit()


def clean_role(value: str | None) -> str:
    normalized = (value or "").strip()
    if normalized not in STAFF_ROLES:
        raise ValueError("Choose a valid access role.")
    return normalized


def staff_invite_token_hash(token: str | None) -> str:
    value = (token or "").strip()
    if not value:
        return ""
    key = f"{get_app_secret()}:staff-invite".encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def staff_auth_token_hash(token: str | None) -> str:
    value = (token or "").strip()
    if not value:
        return ""
    key = f"{get_app_secret()}:staff-auth".encode("utf-8")
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def serialize_staff_user_row(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    mapping = dict(row)
    invite_pending = bool(mapping.get("invite_token_hash") or mapping.get("invite_token")) and not bool(
        mapping.get("password_hash")
    )
    return {
        "id": int(mapping["id"]),
        "email": mapping["email"],
        "full_name": mapping["full_name"],
        "role": mapping["role"],
        "is_active": bool(mapping["is_active"]),
        "invite_pending": invite_pending,
        "invite_expires_at": mapping.get("invite_expires_at"),
        "auth_version": int(mapping.get("auth_version") or 1),
        "mfa_enabled": bool(mapping.get("mfa_enabled_at")),
        "mfa_enabled_at": mapping.get("mfa_enabled_at"),
        "last_login_at": mapping.get("last_login_at"),
    }


def count_staff_users() -> int:
    with get_db_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM staff_users").fetchone()
    return 0 if row is None else int(row["count"])


def list_staff_users() -> list[dict[str, object]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, email, full_name, role, is_active, invite_token, invite_token_hash,
                   invite_expires_at, password_hash, auth_version, mfa_enabled_at, last_login_at
            FROM staff_users
            ORDER BY CASE WHEN role = 'Commissioner' THEN 0 ELSE 1 END, full_name, email
            """
        ).fetchall()
    staff: list[dict[str, object]] = []
    for row in rows:
        serialized = serialize_staff_user_row(row)
        if serialized is not None:
            staff.append(serialized)
    return staff


def get_staff_user_by_id(staff_user_id: int | None) -> dict[str, object] | None:
    if staff_user_id is None:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, email, full_name, role, is_active, invite_token, invite_token_hash,
                   invite_expires_at, password_hash, auth_version, mfa_enabled_at, last_login_at
            FROM staff_users
            WHERE id = ?
            """,
            (int(staff_user_id),),
        ).fetchone()
    return serialize_staff_user_row(row)


def create_first_staff_user(email: str, full_name: str, password: str, role: str = "Commissioner") -> dict[str, object]:
    normalized_email = clean_email(email)
    normalized_name = (full_name or "").strip() or "Commission user"
    normalized_role = clean_role(role)
    normalized_password = clean_password(password)
    with get_db_connection() as conn:
        existing = conn.execute("SELECT id FROM staff_users LIMIT 1").fetchone()
        if existing is not None:
            raise ValueError("The commission workspace already has an admin account.")
        timestamp = timestamp_now()
        conn.execute(
            """
            INSERT INTO staff_users (
                email, full_name, role, password_hash, is_active, invite_token, invite_expires_at,
                created_at, updated_at, last_login_at
            )
            VALUES (?, ?, ?, ?, 1, NULL, NULL, ?, ?, NULL)
            """,
            (
                normalized_email,
                normalized_name,
                normalized_role,
                generate_password_hash(normalized_password),
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, email, full_name, role, is_active, invite_token, invite_token_hash,
                   invite_expires_at, password_hash, auth_version, mfa_enabled_at, last_login_at
            FROM staff_users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
    return serialize_staff_user_row(row) or {}


def authenticate_staff_user(email: str, password: str) -> dict[str, object]:
    normalized_email = clean_email(email)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, email, full_name, role, is_active, invite_token, invite_token_hash,
                   invite_expires_at, password_hash, auth_version, mfa_enabled_at, last_login_at
            FROM staff_users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
        if row is None or not row["password_hash"] or not bool(row["is_active"]):
            raise ValueError("That sign-in did not work.")
        if not check_password_hash(row["password_hash"], password or ""):
            raise ValueError("That sign-in did not work.")
    return get_staff_user_by_id(int(row["id"])) or {}


def mark_staff_login_succeeded(staff_user_id: int) -> None:
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE staff_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, int(staff_user_id)),
        )
        conn.commit()


def invite_staff_user(email: str, full_name: str, role: str, invited_by_id: int) -> dict[str, object]:
    normalized_email = clean_email(email)
    normalized_name = (full_name or "").strip() or normalized_email
    normalized_role = clean_role(role)
    token = secrets.token_urlsafe(32)
    token_hash = staff_invite_token_hash(token)
    expires_at = (datetime.now() + timedelta(days=INVITE_EXPIRY_DAYS)).isoformat(timespec="seconds")
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT id, password_hash FROM staff_users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO staff_users (
                    email, full_name, role, password_hash, is_active, invite_token, invite_token_hash,
                    invite_expires_at, auth_version, created_at, updated_at, last_login_at
                )
                VALUES (?, ?, ?, NULL, 1, NULL, ?, ?, 1, ?, ?, NULL)
                """,
                (
                    normalized_email,
                    normalized_name,
                    normalized_role,
                    token_hash,
                    expires_at,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE staff_users
                SET full_name = ?, role = ?, invite_token = NULL, invite_token_hash = ?,
                    invite_expires_at = ?, updated_at = ?, is_active = 1,
                    auth_version = auth_version + 1
                WHERE id = ?
                """,
                (
                    normalized_name,
                    normalized_role,
                    token_hash,
                    expires_at,
                    timestamp,
                    int(existing["id"]),
                ),
            )
        conn.commit()
    return {
        "email": normalized_email,
        "full_name": normalized_name,
        "role": normalized_role,
        "token": token,
        "expires_at": expires_at,
    }


def update_staff_user_access(
    staff_user_id: int,
    *,
    role: str,
    is_active: bool,
    acting_staff_user_id: int,
) -> dict[str, object]:
    target_id = int(staff_user_id)
    actor_id = int(acting_staff_user_id)
    normalized_role = clean_role(role)
    if target_id == actor_id:
        raise ValueError("Ask another commissioner to change your access.")
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        if conn.kind == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(?)", (POSTGRES_STAFF_LOCK_KEY,))
        else:
            conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            """
            SELECT id, email, full_name, role, is_active, invite_token, invite_token_hash,
                   invite_expires_at, password_hash, auth_version, mfa_enabled_at, last_login_at
            FROM staff_users
            WHERE id = ?
            """,
            (target_id,),
        ).fetchone()
        if target is None:
            conn.rollback()
            raise ValueError("That staff member is no longer available.")
        before = serialize_staff_user_row(target) or {}
        removing_active_commissioner = (
            bool(before["is_active"])
            and before["role"] == "Commissioner"
            and (not is_active or normalized_role != "Commissioner")
        )
        if removing_active_commissioner:
            commissioner_count = conn.execute(
                "SELECT COUNT(*) AS count FROM staff_users WHERE role = 'Commissioner' AND is_active = 1"
            ).fetchone()
            if commissioner_count is None or int(commissioner_count["count"]) <= 1:
                conn.rollback()
                raise ValueError("At least one active commissioner must remain.")
        conn.execute(
            """
            UPDATE staff_users
            SET role = ?, is_active = ?, auth_version = auth_version + 1,
                invite_token = CASE WHEN ? = 1 THEN invite_token ELSE NULL END,
                invite_token_hash = CASE WHEN ? = 1 THEN invite_token_hash ELSE NULL END,
                invite_expires_at = CASE WHEN ? = 1 THEN invite_expires_at ELSE NULL END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                normalized_role,
                1 if is_active else 0,
                1 if is_active else 0,
                1 if is_active else 0,
                1 if is_active else 0,
                timestamp,
                target_id,
            ),
        )
        conn.execute(
            """
            UPDATE staff_auth_tokens
            SET consumed_at = ?
            WHERE staff_user_id = ? AND consumed_at IS NULL
            """,
            (timestamp, target_id),
        )
        conn.commit()
    after = get_staff_user_by_id(target_id)
    if after is None:
        raise RuntimeError("The staff access change could not be confirmed.")
    return {"before": before, "after": after}


def load_invited_staff_user(token: str | None) -> dict[str, object] | None:
    if not token:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, email, full_name, role, is_active, invite_token, invite_token_hash,
                   invite_expires_at, password_hash, auth_version, mfa_enabled_at, last_login_at
            FROM staff_users
            WHERE invite_token_hash = ? OR invite_token = ?
            """,
            (staff_invite_token_hash(token), token),
        ).fetchone()
    return serialize_staff_user_row(row)


def accept_staff_invite(token: str, password: str, full_name: str | None = None) -> dict[str, object]:
    normalized_password = clean_password(password)
    invited_user = load_invited_staff_user(token)
    if invited_user is None:
        raise ValueError("That setup link is no longer available.")
    expires_at = invited_user.get("invite_expires_at")
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now():
        raise ValueError("That setup link has expired.")
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE staff_users
            SET full_name = ?, password_hash = ?, invite_token = NULL, invite_token_hash = NULL,
                invite_expires_at = NULL, auth_version = auth_version + 1, updated_at = ?
            WHERE id = ?
            """,
            (
                (full_name or "").strip() or invited_user["full_name"],
                generate_password_hash(normalized_password),
                timestamp,
                invited_user["id"],
            ),
        )
        conn.commit()
    return get_staff_user_by_id(int(invited_user["id"])) or {}


def get_staff_user_by_email(email: str | None) -> dict[str, object] | None:
    try:
        normalized_email = clean_email(email)
    except ValueError:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, email, full_name, role, is_active, invite_token, invite_token_hash,
                   invite_expires_at, password_hash, auth_version, mfa_enabled_at, last_login_at
            FROM staff_users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
    return serialize_staff_user_row(row)


def create_staff_auth_token(staff_user_id: int, purpose: str, lifetime: timedelta) -> str:
    if purpose not in STAFF_AUTH_TOKEN_PURPOSES:
        raise ValueError("Choose a valid staff account token purpose.")
    token = secrets.token_urlsafe(32)
    token_hash = staff_auth_token_hash(token)
    created_at = timestamp_now()
    expires_at = (datetime.now() + lifetime).isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE staff_auth_tokens
            SET consumed_at = ?
            WHERE staff_user_id = ? AND purpose = ? AND consumed_at IS NULL
            """,
            (created_at, int(staff_user_id), purpose),
        )
        conn.execute(
            """
            INSERT INTO staff_auth_tokens (
                staff_user_id, purpose, token_hash, expires_at, consumed_at,
                requested_ip_hash, created_at
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                int(staff_user_id),
                purpose,
                token_hash,
                expires_at,
                request_remote_hash(),
                created_at,
            ),
        )
        conn.commit()
    return token


def load_valid_staff_auth_token(token: str | None, purpose: str) -> dict[str, object] | None:
    token_hash = staff_auth_token_hash(token)
    if not token_hash or purpose not in STAFF_AUTH_TOKEN_PURPOSES:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT staff_auth_tokens.id AS token_id,
                   staff_auth_tokens.expires_at,
                   staff_users.id,
                   staff_users.email,
                   staff_users.full_name,
                   staff_users.role,
                   staff_users.is_active,
                   staff_users.invite_token,
                   staff_users.invite_token_hash,
                   staff_users.invite_expires_at,
                   staff_users.password_hash,
                   staff_users.auth_version,
                   staff_users.last_login_at
            FROM staff_auth_tokens
            JOIN staff_users ON staff_users.id = staff_auth_tokens.staff_user_id
            WHERE staff_auth_tokens.token_hash = ?
              AND staff_auth_tokens.purpose = ?
              AND staff_auth_tokens.consumed_at IS NULL
            """,
            (token_hash, purpose),
        ).fetchone()
    if row is None or not bool(row["is_active"]) or not row["password_hash"]:
        return None
    if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now():
        return None
    return {
        "token_id": int(row["token_id"]),
        "staff_user": serialize_staff_user_row(row) or {},
        "expires_at": str(row["expires_at"]),
    }


def reset_staff_password(token: str | None, password: str | None) -> dict[str, object]:
    normalized_password = clean_password(password)
    token_record = load_valid_staff_auth_token(token, "password_reset")
    if token_record is None:
        raise ValueError("That password link is no longer available.")
    staff_user = token_record["staff_user"]
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE staff_auth_tokens
            SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL
            """,
            (timestamp, int(token_record["token_id"])),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise ValueError("That password link is no longer available.")
        conn.execute(
            """
            UPDATE staff_auth_tokens
            SET consumed_at = ?
            WHERE staff_user_id = ? AND purpose = 'password_reset' AND consumed_at IS NULL
            """,
            (timestamp, int(staff_user["id"])),
        )
        conn.execute(
            """
            UPDATE staff_users
            SET password_hash = ?, auth_version = auth_version + 1, updated_at = ?
            WHERE id = ?
            """,
            (generate_password_hash(normalized_password), timestamp, int(staff_user["id"])),
        )
        conn.commit()
    return get_staff_user_by_id(int(staff_user["id"])) or {}


def send_staff_invitation_email(invite: dict[str, object], base_url: str) -> None:
    setup_url = build_absolute_url(base_url, f"/staff/setup/{invite['token']}")
    send_transactional_email(
        str(invite["email"]),
        "Your Home Energy Watch commission access",
        (
            f"Hello {invite['full_name']},\n\n"
            "You have been invited to the Home Energy Watch commission workspace. "
            "Use this one-time link to choose your password.\n\n"
            f"{setup_url}\n\n"
            f"This link expires in {INVITE_EXPIRY_DAYS} days. "
            "If you were not expecting this invitation, you can ignore this message.\n"
        ),
    )


def send_staff_password_reset_email(staff_user: dict[str, object], base_url: str) -> None:
    token = create_staff_auth_token(
        int(staff_user["id"]),
        "password_reset",
        timedelta(minutes=PASSWORD_RESET_EXPIRY_MINUTES),
    )
    reset_url = build_absolute_url(
        base_url,
        f"/staff/reset-password?{urlencode({'token': token})}",
    )
    send_transactional_email(
        str(staff_user["email"]),
        "Reset your Home Energy Watch commission password",
        (
            f"Hello {staff_user['full_name']},\n\n"
            "Use this link to choose a new password for the Home Energy Watch commission workspace.\n\n"
            f"{reset_url}\n\n"
            f"This link expires in {PASSWORD_RESET_EXPIRY_MINUTES} minutes and works once. "
            "If you did not request it, you can ignore this message.\n"
        ),
    )


def serialize_customer_user_row(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    mapping = dict(row)
    return {
        "id": int(mapping["id"]),
        "email": mapping["email"],
        "full_name": mapping["full_name"],
        "is_active": bool(mapping["is_active"]),
        "email_verified_at": mapping.get("email_verified_at"),
        "email_verified": bool(mapping.get("email_verified_at")),
        "auth_version": int(mapping.get("auth_version") or 1),
        "last_login_at": mapping.get("last_login_at"),
    }


def get_customer_user_by_id(customer_user_id: int | None) -> dict[str, object] | None:
    if customer_user_id is None:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, email, full_name, is_active, email_verified_at, auth_version, last_login_at
            FROM customer_users
            WHERE id = ?
            """,
            (int(customer_user_id),),
        ).fetchone()
    return serialize_customer_user_row(row)


def create_customer_user(email: str, full_name: str, password: str) -> dict[str, object]:
    normalized_email = clean_email(email)
    normalized_name = (full_name or "").strip() or normalized_email
    normalized_password = clean_password(password)
    timestamp = timestamp_now()
    email_verified_at = None if email_verification_required() else timestamp
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM customer_users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if existing is not None:
            raise ValueError("An account already uses that email address.")
        conn.execute(
            """
            INSERT INTO customer_users (
                email, full_name, password_hash, is_active, email_verified_at, auth_version,
                created_at, updated_at, last_login_at
            )
            VALUES (?, ?, ?, 1, ?, 1, ?, ?, NULL)
            """,
            (
                normalized_email,
                normalized_name,
                generate_password_hash(normalized_password),
                email_verified_at,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
        row = conn.execute(
            """
            SELECT id, email, full_name, is_active, email_verified_at, auth_version, last_login_at
            FROM customer_users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
    return serialize_customer_user_row(row) or {}


def authenticate_customer_user(email: str, password: str) -> dict[str, object]:
    normalized_email = clean_email(email)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, email, full_name, password_hash, is_active, email_verified_at, auth_version, last_login_at
            FROM customer_users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
        if row is None or not bool(row["is_active"]):
            raise ValueError("That sign-in did not work.")
        if not check_password_hash(row["password_hash"], password or ""):
            raise ValueError("That sign-in did not work.")
        customer_user = serialize_customer_user_row(row) or {}
        if email_verification_required() and not customer_user.get("email_verified"):
            raise EmailVerificationRequired(customer_user)
        conn.execute(
            "UPDATE customer_users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (timestamp_now(), timestamp_now(), int(row["id"])),
        )
        conn.commit()
    return get_customer_user_by_id(int(row["id"])) or {}


def get_customer_user_by_email(email: str | None) -> dict[str, object] | None:
    try:
        normalized_email = clean_email(email)
    except ValueError:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT id, email, full_name, is_active, email_verified_at, auth_version, last_login_at
            FROM customer_users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
    return serialize_customer_user_row(row)


def customer_auth_token_hash(token: str | None) -> str:
    value = (token or "").strip()
    if not value:
        return ""
    return hmac.new(get_app_secret().encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def request_remote_hash() -> str | None:
    if not has_request_context():
        return None
    remote_address = (request.remote_addr or "").strip()
    if not remote_address:
        return None
    return hmac.new(
        get_app_secret().encode("utf-8"),
        remote_address.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def request_user_agent_hash() -> str | None:
    if not has_request_context():
        return None
    user_agent = (request.headers.get("User-Agent") or "").strip()
    if not user_agent:
        return None
    return hmac.new(
        get_app_secret().encode("utf-8"),
        user_agent.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_customer_auth_token(
    customer_user_id: int,
    purpose: str,
    lifetime: timedelta,
) -> str:
    if purpose not in CUSTOMER_AUTH_TOKEN_PURPOSES:
        raise ValueError("Choose a valid account token purpose.")
    token = secrets.token_urlsafe(32)
    token_hash = customer_auth_token_hash(token)
    created_at = timestamp_now()
    expires_at = (datetime.now() + lifetime).isoformat(timespec="seconds")
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE customer_auth_tokens
            SET consumed_at = ?
            WHERE customer_user_id = ? AND purpose = ? AND consumed_at IS NULL
            """,
            (created_at, int(customer_user_id), purpose),
        )
        conn.execute(
            """
            INSERT INTO customer_auth_tokens (
                customer_user_id, purpose, token_hash, expires_at, consumed_at,
                requested_ip_hash, created_at
            )
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                int(customer_user_id),
                purpose,
                token_hash,
                expires_at,
                request_remote_hash(),
                created_at,
            ),
        )
        conn.commit()
    return token


def load_valid_customer_auth_token(token: str | None, purpose: str) -> dict[str, object] | None:
    token_hash = customer_auth_token_hash(token)
    if not token_hash or purpose not in CUSTOMER_AUTH_TOKEN_PURPOSES:
        return None
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT customer_auth_tokens.id AS token_id,
                   customer_users.id AS id,
                   customer_auth_tokens.expires_at,
                   customer_users.email,
                   customer_users.full_name,
                   customer_users.is_active,
                   customer_users.email_verified_at,
                   customer_users.auth_version,
                   customer_users.last_login_at
            FROM customer_auth_tokens
            JOIN customer_users ON customer_users.id = customer_auth_tokens.customer_user_id
            WHERE customer_auth_tokens.token_hash = ?
              AND customer_auth_tokens.purpose = ?
              AND customer_auth_tokens.consumed_at IS NULL
            """,
            (token_hash, purpose),
        ).fetchone()
    if row is None or not bool(row["is_active"]):
        return None
    if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now():
        return None
    return {
        "token_id": int(row["token_id"]),
        "customer_user": serialize_customer_user_row(row) or {},
        "expires_at": str(row["expires_at"]),
    }


def verify_customer_email(token: str | None) -> dict[str, object]:
    token_record = load_valid_customer_auth_token(token, "verify_email")
    if token_record is None:
        raise ValueError("That confirmation link is no longer available.")
    customer_user = token_record["customer_user"]
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE customer_auth_tokens
            SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL
            """,
            (timestamp, int(token_record["token_id"])),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise ValueError("That confirmation link is no longer available.")
        conn.execute(
            """
            UPDATE customer_users
            SET email_verified_at = COALESCE(email_verified_at, ?), updated_at = ?
            WHERE id = ?
            """,
            (timestamp, timestamp, int(customer_user["id"])),
        )
        conn.commit()
    return get_customer_user_by_id(int(customer_user["id"])) or {}


def reset_customer_password(token: str | None, password: str | None) -> dict[str, object]:
    normalized_password = clean_password(password)
    token_record = load_valid_customer_auth_token(token, "password_reset")
    if token_record is None:
        raise ValueError("That password link is no longer available.")
    customer_user = token_record["customer_user"]
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        cursor = conn.execute(
            """
            UPDATE customer_auth_tokens
            SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL
            """,
            (timestamp, int(token_record["token_id"])),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise ValueError("That password link is no longer available.")
        conn.execute(
            """
            UPDATE customer_auth_tokens
            SET consumed_at = ?
            WHERE customer_user_id = ? AND purpose = 'password_reset' AND consumed_at IS NULL
            """,
            (timestamp, int(customer_user["id"])),
        )
        conn.execute(
            """
            UPDATE customer_users
            SET password_hash = ?, auth_version = auth_version + 1, updated_at = ?
            WHERE id = ?
            """,
            (generate_password_hash(normalized_password), timestamp, int(customer_user["id"])),
        )
        conn.commit()
    return get_customer_user_by_id(int(customer_user["id"])) or {}


def mask_email_address(email: str | None) -> str:
    normalized = (email or "").strip()
    if "@" not in normalized:
        return "your email address"
    local, domain = normalized.rsplit("@", 1)
    visible = local[:1] if local else ""
    return f"{visible}{'*' * max(3, len(local) - 1)}@{domain}"


def send_customer_verification_email(customer_user: dict[str, object], base_url: str) -> None:
    token = create_customer_auth_token(
        int(customer_user["id"]),
        "verify_email",
        timedelta(hours=EMAIL_VERIFICATION_EXPIRY_HOURS),
    )
    confirmation_url = build_absolute_url(
        base_url,
        f"/customer/verify-email?{urlencode({'token': token})}",
    )
    send_transactional_email(
        str(customer_user["email"]),
        "Confirm your Home Energy Watch account",
        (
            f"Hello {customer_user['full_name']},\n\n"
            "Confirm your email address before opening your energy history or continuing to payment.\n\n"
            f"{confirmation_url}\n\n"
            f"This link expires in {EMAIL_VERIFICATION_EXPIRY_HOURS} hours. "
            "If you did not create this account, you can ignore this message.\n"
        ),
    )


def send_customer_password_reset_email(customer_user: dict[str, object], base_url: str) -> None:
    token = create_customer_auth_token(
        int(customer_user["id"]),
        "password_reset",
        timedelta(minutes=PASSWORD_RESET_EXPIRY_MINUTES),
    )
    reset_url = build_absolute_url(
        base_url,
        f"/customer/reset-password?{urlencode({'token': token})}",
    )
    send_transactional_email(
        str(customer_user["email"]),
        "Reset your Home Energy Watch password",
        (
            f"Hello {customer_user['full_name']},\n\n"
            "Use this link to choose a new password for Home Energy Watch.\n\n"
            f"{reset_url}\n\n"
            f"This link expires in {PASSWORD_RESET_EXPIRY_MINUTES} minutes and works once. "
            "If you did not request it, you can ignore this message.\n"
        ),
    )


def audit_event_hash(
    event: dict[str, object],
    previous_hash: str | None,
) -> str:
    payload = {
        "previous_hash": previous_hash or "",
        "occurred_at": str(event.get("occurred_at") or ""),
        "actor_type": str(event.get("actor_type") or ""),
        "actor_id": event.get("actor_id"),
        "action": str(event.get("action") or ""),
        "account_id": event.get("account_id"),
        "target_type": event.get("target_type"),
        "target_id": event.get("target_id"),
        "request_id": str(event.get("request_id") or ""),
        "remote_hash": event.get("remote_hash"),
        "metadata_json": str(event.get("metadata_json") or "{}"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(
        get_audit_signing_key().encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def audit_chain_status(conn: DatabaseConnection) -> dict[str, object]:
    rows = conn.execute(
        """
        SELECT id, occurred_at, actor_type, actor_id, action, account_id, target_type,
               target_id, request_id, remote_hash, metadata_json, previous_hash, event_hash
        FROM audit_events
        ORDER BY id ASC
        """
    ).fetchall()
    previous_hash = ""
    for row in rows:
        mapping = dict(row)
        saved_previous = str(mapping.get("previous_hash") or "")
        saved_hash = str(mapping.get("event_hash") or "")
        if not saved_hash or not hmac.compare_digest(saved_previous, previous_hash):
            return {
                "valid": False,
                "checked_events": len(rows),
                "failure_event_id": int(mapping["id"]),
            }
        expected_hash = audit_event_hash(mapping, previous_hash)
        if not hmac.compare_digest(saved_hash, expected_hash):
            return {
                "valid": False,
                "checked_events": len(rows),
                "failure_event_id": int(mapping["id"]),
            }
        previous_hash = saved_hash
    return {
        "valid": True,
        "checked_events": len(rows),
        "failure_event_id": None,
        "latest_hash": previous_hash,
    }


def backfill_audit_event_hashes(conn: DatabaseConnection) -> None:
    rows = conn.execute(
        """
        SELECT id, occurred_at, actor_type, actor_id, action, account_id, target_type,
               target_id, request_id, remote_hash, metadata_json, previous_hash, event_hash
        FROM audit_events
        ORDER BY id ASC
        """
    ).fetchall()
    if not rows:
        return
    saved_hashes = [str(dict(row).get("event_hash") or "") for row in rows]
    if all(not saved_hash for saved_hash in saved_hashes):
        previous_hash = ""
        for row in rows:
            mapping = dict(row)
            event_hash_value = audit_event_hash(mapping, previous_hash)
            conn.execute(
                "UPDATE audit_events SET previous_hash = ?, event_hash = ? WHERE id = ?",
                (previous_hash or None, event_hash_value, int(mapping["id"])),
            )
            previous_hash = event_hash_value
        return
    if any(not saved_hash for saved_hash in saved_hashes):
        raise RuntimeError("The audit record has an incomplete integrity chain.")
    status = audit_chain_status(conn)
    if not status["valid"]:
        raise RuntimeError("The audit record did not pass its integrity check.")


def verify_audit_chain() -> dict[str, object]:
    with get_db_connection() as conn:
        return audit_chain_status(conn)


def record_audit_event(
    action: str,
    *,
    actor_type: str = "system",
    actor_id: int | None = None,
    account_number: str | None = None,
    target_type: str | None = None,
    target_id: object | None = None,
    metadata: dict[str, object] | None = None,
) -> int:
    normalized_action = (action or "").strip()
    if not normalized_action:
        raise ValueError("Audit actions require a name.")
    request_id = str(getattr(g, "request_id", "") or uuid4().hex) if has_request_context() else uuid4().hex
    metadata_json = json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))
    if len(metadata_json) > 4000:
        metadata_json = json.dumps({"detail": "Audit metadata exceeded the storage limit."})
    with get_db_connection() as conn:
        if conn.kind == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(?)", (POSTGRES_AUDIT_LOCK_KEY,))
        else:
            conn.execute("BEGIN IMMEDIATE")
        account_id = None
        if account_number:
            account_row = conn.execute(
                "SELECT id FROM accounts WHERE account_number = ?",
                (normalize_account_number(account_number),),
            ).fetchone()
            account_id = None if account_row is None else int(account_row["id"])
        previous_row = conn.execute(
            "SELECT event_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "" if previous_row is None else str(previous_row["event_hash"] or "")
        if previous_row is not None and not previous_hash:
            conn.rollback()
            raise RuntimeError("The audit record has an incomplete integrity chain.")
        event = {
            "occurred_at": timestamp_now(),
            "actor_type": actor_type,
            "actor_id": actor_id,
            "action": normalized_action,
            "account_id": account_id,
            "target_type": clean_optional_text(target_type),
            "target_id": None if target_id is None else str(target_id),
            "request_id": request_id,
            "remote_hash": request_remote_hash(),
            "metadata_json": metadata_json,
        }
        event_hash_value = audit_event_hash(event, previous_hash)
        values = (
            event["occurred_at"],
            event["actor_type"],
            event["actor_id"],
            event["action"],
            event["account_id"],
            event["target_type"],
            event["target_id"],
            event["request_id"],
            event["remote_hash"],
            event["metadata_json"],
            previous_hash or None,
            event_hash_value,
        )
        insert_sql = """
            INSERT INTO audit_events (
                occurred_at, actor_type, actor_id, action, account_id, target_type,
                target_id, request_id, remote_hash, metadata_json, previous_hash, event_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if conn.kind == "postgres":
            row = conn.execute(f"{insert_sql} RETURNING id", values).fetchone()
            event_id = int(row["id"])
        else:
            cursor = conn.execute(insert_sql, values)
            event_id = int(cursor.lastrowid)
        conn.commit()
    return event_id


def list_audit_events(
    *,
    account_number: str | None = None,
    action: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict[str, object]:
    safe_page = max(1, int(page or 1))
    safe_per_page = min(100, max(25, int(per_page or 50)))
    clauses: list[str] = []
    params: list[object] = []
    if account_number:
        clauses.append("accounts.account_number = ?")
        params.append(normalize_account_number(account_number))
    normalized_action = (action or "").strip()
    if normalized_action:
        clauses.append("audit_events.action = ?")
        params.append(normalized_action)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db_connection() as conn:
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM audit_events
            LEFT JOIN accounts ON accounts.id = audit_events.account_id
            {where_clause}
            """,
            tuple(params),
        ).fetchone()
        total = 0 if total_row is None else int(total_row["count"])
        total_pages = max(1, (total + safe_per_page - 1) // safe_per_page)
        safe_page = min(safe_page, total_pages)
        rows = conn.execute(
            f"""
            SELECT audit_events.*, accounts.account_number,
                   staff_users.full_name AS staff_name,
                   customer_users.full_name AS customer_name
            FROM audit_events
            LEFT JOIN accounts ON accounts.id = audit_events.account_id
            LEFT JOIN staff_users
              ON audit_events.actor_type = 'staff' AND staff_users.id = audit_events.actor_id
            LEFT JOIN customer_users
              ON audit_events.actor_type = 'customer' AND customer_users.id = audit_events.actor_id
            {where_clause}
            ORDER BY audit_events.id DESC
            LIMIT ? OFFSET ?
            """,
            tuple([*params, safe_per_page, (safe_page - 1) * safe_per_page]),
        ).fetchall()
    events: list[dict[str, object]] = []
    for row in rows:
        mapping = dict(row)
        actor_name = mapping.get("staff_name") or mapping.get("customer_name") or mapping["actor_type"].title()
        try:
            event_metadata = json.loads(mapping.get("metadata_json") or "{}")
        except json.JSONDecodeError:
            event_metadata = {}
        events.append(
            {
                "id": int(mapping["id"]),
                "occurred_at": mapping["occurred_at"],
                "actor_type": mapping["actor_type"],
                "actor_name": actor_name,
                "action": mapping["action"],
                "account_number": mapping.get("account_number"),
                "target_type": mapping.get("target_type"),
                "target_id": mapping.get("target_id"),
                "request_id": mapping["request_id"],
                "previous_hash": mapping.get("previous_hash") or "",
                "event_hash": mapping.get("event_hash") or "",
                "metadata": event_metadata,
            }
        )
    return {
        "events": events,
        "account_number": account_number or "",
        "action": normalized_action,
        "page": safe_page,
        "per_page": safe_per_page,
        "total": total,
        "total_pages": total_pages,
        "has_previous": safe_page > 1,
        "has_next": safe_page < total_pages,
    }


def audit_csv_value(value: object | None) -> object:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def build_audit_export_csv(account_number: str | None = None, action: str | None = None) -> str:
    clauses: list[str] = []
    params: list[object] = []
    if account_number:
        clauses.append("accounts.account_number = ?")
        params.append(normalize_account_number(account_number))
    normalized_action = (action or "").strip()
    if normalized_action:
        clauses.append("audit_events.action = ?")
        params.append(normalized_action)
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with get_db_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT audit_events.*, accounts.account_number,
                   staff_users.full_name AS staff_name,
                   customer_users.full_name AS customer_name
            FROM audit_events
            LEFT JOIN accounts ON accounts.id = audit_events.account_id
            LEFT JOIN staff_users
              ON audit_events.actor_type = 'staff' AND staff_users.id = audit_events.actor_id
            LEFT JOIN customer_users
              ON audit_events.actor_type = 'customer' AND customer_users.id = audit_events.actor_id
            {where_clause}
            ORDER BY audit_events.id ASC
            """,
            tuple(params),
        ).fetchall()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        (
            "event_id",
            "occurred_at",
            "action",
            "actor_type",
            "actor_name",
            "account_number",
            "target_type",
            "target_id",
            "request_id",
            "previous_hash",
            "event_hash",
            "metadata_json",
        )
    )
    for row in rows:
        mapping = dict(row)
        actor_name = mapping.get("staff_name") or mapping.get("customer_name") or mapping["actor_type"].title()
        writer.writerow(
            tuple(
                audit_csv_value(value)
                for value in (
                    int(mapping["id"]),
                    mapping["occurred_at"],
                    mapping["action"],
                    mapping["actor_type"],
                    actor_name,
                    mapping.get("account_number"),
                    mapping.get("target_type"),
                    mapping.get("target_id"),
                    mapping["request_id"],
                    mapping.get("previous_hash"),
                    mapping.get("event_hash"),
                    mapping.get("metadata_json") or "{}",
                )
            )
        )
    return output.getvalue()


def customer_export_accounts(email: str) -> list[dict[str, object]]:
    normalized_email = clean_email(email)
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT accounts.id, accounts.account_number, accounts.display_name,
                   accounts.energy_company, accounts.baseline_date,
                   accounts.created_at, accounts.updated_at,
                   account_access_emails.access_level
            FROM account_access_emails
            JOIN accounts ON accounts.id = account_access_emails.account_id
            WHERE account_access_emails.email = ?
            ORDER BY accounts.account_number
            """,
            (normalized_email,),
        ).fetchall()
    return [dict(row) for row in rows]


def customer_export_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def build_customer_data_archive(customer_user: dict[str, object]) -> tuple[bytes, int]:
    customer_id = int(customer_user["id"])
    email = clean_email(str(customer_user["email"]))
    accounts = customer_export_accounts(email)
    archive_buffer = io.BytesIO()
    with get_db_connection() as conn, zipfile.ZipFile(
        archive_buffer,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        user_row = conn.execute(
            """
            SELECT email, full_name, email_verified_at, created_at, updated_at, last_login_at
            FROM customer_users
            WHERE id = ?
            """,
            (customer_id,),
        ).fetchone()
        billing_row = conn.execute(
            """
            SELECT plan_id, subscription_status, stripe_receipt_url, current_period_end,
                   created_at, updated_at
            FROM customer_billing
            WHERE customer_user_id = ?
            """,
            (customer_id,),
        ).fetchone()
        policy_rows = conn.execute(
            """
            SELECT terms_version, privacy_version, accepted_at
            FROM customer_policy_acceptances
            WHERE customer_user_id = ?
            ORDER BY accepted_at, id
            """,
            (customer_id,),
        ).fetchall()
        manifest = {
            "format_version": 2,
            "generated_at": timestamp_now(),
            "customer": {} if user_row is None else dict(user_row),
            "billing": {} if billing_row is None else dict(billing_row),
            "policy_acceptances": [dict(row) for row in policy_rows],
            "account_count": len(accounts),
        }
        archive.writestr("manifest.json", customer_export_json(manifest))

        for account in accounts:
            account_id = int(account["id"])
            account_number = str(account["account_number"])
            path_name = secure_filename(account_number) or f"account-{account_id}"
            prefix = f"accounts/{path_name}-{account_id}"
            household_row = conn.execute(
                """
                SELECT address, zip_code, occupant_count, year_built, square_footage,
                       heating_system, cooling_system, water_heater, notes,
                       latitude, longitude, weather_location, updated_at
                FROM household_profiles
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            inventory_rows = conn.execute(
                """
                SELECT label, quantity, watts_each, include_when_off, notes, created_at, updated_at
                FROM account_load_items
                WHERE account_id = ?
                ORDER BY id
                """,
                (account_id,),
            ).fetchall()
            connection_rows = conn.execute(
                """
                SELECT provider_name, connection_label, access_method, status,
                       last_sync_at, last_sync_status, last_sync_attempt_at,
                       created_at, updated_at
                FROM utility_connections
                WHERE account_id = ?
                ORDER BY provider_name, connection_label
                """,
                (account_id,),
            ).fetchall()
            authorization_rows = conn.execute(
                """
                SELECT authorization_version, authorization_scope, status,
                       granted_at, revoked_at
                FROM account_data_authorizations
                WHERE account_id = ? AND customer_user_id = ?
                ORDER BY created_at, id
                """,
                (account_id, customer_id),
            ).fetchall()
            import_rows = conn.execute(
                """
                SELECT path, interval_count, imported_at, service_point_id
                FROM imported_files
                WHERE account_id = ?
                ORDER BY imported_at, path
                """,
                (account_id,),
            ).fetchall()
            imports = [
                {
                    "source_file": Path(str(row["path"])).name,
                    "interval_count": int(row["interval_count"]),
                    "imported_at": row["imported_at"],
                    "service_point_id": row["service_point_id"],
                }
                for row in import_rows
            ]
            profile = {
                "account": {
                    "account_number": account_number,
                    "display_name": account["display_name"],
                    "energy_company": account["energy_company"],
                    "baseline_date": account["baseline_date"],
                    "created_at": account["created_at"],
                    "updated_at": account["updated_at"],
                },
                "access_level": account["access_level"],
                "household": {} if household_row is None else dict(household_row),
                "inventory": [dict(row) for row in inventory_rows],
                "utility_connections": [dict(row) for row in connection_rows],
                "data_authorizations": [dict(row) for row in authorization_rows],
                "imports": imports,
            }
            archive.writestr(f"{prefix}/profile.json", customer_export_json(profile))

            interval_rows = conn.execute(
                """
                SELECT start_epoch, duration_s, wh, source_path, imported_at
                FROM interval_readings
                WHERE account_id = ?
                ORDER BY start_epoch, duration_s
                """,
                (account_id,),
            ).fetchall()
            interval_output = io.StringIO(newline="")
            interval_writer = csv.writer(interval_output)
            interval_writer.writerow(
                ("start_epoch", "start_utc", "duration_seconds", "usage_wh", "usage_kwh", "source_file", "imported_at")
            )
            for row in interval_rows:
                start_epoch = int(row["start_epoch"])
                wh = float(row["wh"])
                interval_writer.writerow(
                    (
                        start_epoch,
                        datetime.fromtimestamp(start_epoch, tz=timezone.utc).isoformat(),
                        int(row["duration_s"]),
                        wh,
                        wh / 1000.0,
                        Path(str(row["source_path"])).name,
                        row["imported_at"],
                    )
                )
            archive.writestr(f"{prefix}/interval-readings.csv", interval_output.getvalue())

            weather_rows = conn.execute(
                """
                SELECT weather_date, timezone, location_name, data_json, fetched_at
                FROM weather_daily_cache
                WHERE account_id = ?
                ORDER BY weather_date
                """,
                (account_id,),
            ).fetchall()
            weather: list[dict[str, object]] = []
            for row in weather_rows:
                try:
                    weather_data = json.loads(row["data_json"] or "{}")
                except json.JSONDecodeError:
                    weather_data = {}
                weather.append(
                    {
                        "date": row["weather_date"],
                        "timezone": row["timezone"],
                        "location": row["location_name"],
                        "fetched_at": row["fetched_at"],
                        "weather": weather_data,
                    }
                )
            archive.writestr(f"{prefix}/weather.json", customer_export_json(weather))

            report_rows = conn.execute(
                """
                SELECT filename
                FROM report_artifacts
                WHERE account_id = ?
                ORDER BY created_at, filename
                """,
                (account_id,),
            ).fetchall()
            for report_row in report_rows:
                filename = normalize_report_filename(str(report_row["filename"]))
                report_path = OUTPUT_DIR / filename
                if report_path.is_file() and report_path.stat().st_size <= MAX_EXPORT_REPORT_BYTES:
                    archive.write(report_path, arcname=f"{prefix}/reports/{filename}")
    return archive_buffer.getvalue(), len(accounts)


def list_billing_plans() -> list[dict[str, object]]:
    plans: list[dict[str, object]] = []
    for plan in BILLING_PLAN_DEFINITIONS:
        plan_copy = dict(plan)
        plan_copy["payment_ready"] = bool(
            plan_copy.get("checkout_supported")
            and billing_checkout_enabled()
            and get_stripe_secret_key()
            and get_stripe_price_id(plan_copy)
        )
        plans.append(plan_copy)
    return plans


def list_energy_company_groups() -> list[dict[str, object]]:
    return [
        {"label": group["label"], "companies": list(group["companies"])}
        for group in ENERGY_COMPANY_GROUPS
    ]


def list_energy_companies() -> list[str]:
    companies: list[str] = []
    for group in ENERGY_COMPANY_GROUPS:
        companies.extend(str(company) for company in group["companies"])
    return companies


def list_utility_access_guides() -> list[dict[str, str]]:
    return [dict(guide) for guide in UTILITY_ACCESS_GUIDES]


def clean_energy_company(value: str | None) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    companies_by_lower = {company.lower(): company for company in list_energy_companies()}
    return companies_by_lower.get(normalized.lower(), normalized)


def clean_zip_code(value: str | None, *, required: bool = False) -> str:
    normalized = (value or "").strip()
    if not normalized:
        if required:
            raise ValueError("Enter the ZIP code for the service address.")
        return ""
    if not re.fullmatch(r"\d{5}(?:-\d{4})?", normalized):
        raise ValueError("Enter a five-digit ZIP code.")
    return normalized[:5]


def canonical_energy_company_name(raw_name: str, utility_id: object = None) -> str:
    normalized_id = ""
    if utility_id is not None:
        try:
            normalized_id = str(int(float(str(utility_id))))
        except (TypeError, ValueError):
            normalized_id = str(utility_id).strip()
    if normalized_id in ENERGY_COMPANY_BY_EIA_ID:
        return ENERGY_COMPANY_BY_EIA_ID[normalized_id]

    cleaned = re.sub(r"\s+-\s+\(NC\)$", "", (raw_name or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    known = clean_energy_company(cleaned)
    if known != cleaned or known in list_energy_companies():
        return known
    if cleaned.isupper():
        return cleaned.title().replace(" E M C", " EMC").replace(" Llc", " LLC")
    return cleaned


def get_billing_plan(plan_id: str | None) -> dict[str, object]:
    normalized_plan_id = (plan_id or "home").strip().lower()
    for plan in list_billing_plans():
        if plan["id"] == normalized_plan_id:
            return plan
    raise ValueError("Choose a valid plan.")


def serialize_customer_billing_row(row: sqlite3.Row | None, customer_user_id: int) -> dict[str, object]:
    if row is None:
        plan = get_billing_plan("home")
        return {
            "customer_user_id": int(customer_user_id),
            "plan_id": plan["id"],
            "plan_name": plan["name"],
            "monthly_price_label": plan["monthly_price_label"],
            "payment_ready": plan["payment_ready"],
            "status": "not_started",
            "status_label": "Not started",
            "checkout_session_id": "",
            "stripe_customer_id": "",
            "stripe_subscription_id": "",
            "stripe_payment_intent_id": "",
            "stripe_receipt_url": "",
            "payments_customer_id": "",
            "payments_order_id": "",
            "payments_checkout_session_id": "",
            "payments_receipt_id": "",
            "current_period_end": "",
        }
    mapping = dict(row)
    plan = get_billing_plan(str(mapping.get("plan_id") or "home"))
    status = mapping.get("subscription_status") or "not_started"
    status_labels = {
        "not_started": "Not started",
        "checkout_started": "Checkout started",
        "active": "Active",
        "trialing": "Trialing",
        "past_due": "Past due",
        "canceled": "Canceled",
        "incomplete": "Incomplete",
    }
    return {
        "customer_user_id": int(mapping["customer_user_id"]),
        "plan_id": plan["id"],
        "plan_name": plan["name"],
        "monthly_price_label": plan["monthly_price_label"],
        "payment_ready": plan["payment_ready"],
        "status": status,
        "status_label": status_labels.get(str(status), str(status).replace("_", " ").title()),
        "checkout_session_id": mapping.get("checkout_session_id") or "",
        "stripe_customer_id": mapping.get("stripe_customer_id") or "",
        "stripe_subscription_id": mapping.get("stripe_subscription_id") or "",
        "stripe_payment_intent_id": mapping.get("stripe_payment_intent_id") or "",
        "stripe_receipt_url": mapping.get("stripe_receipt_url") or "",
        "payments_customer_id": mapping.get("payments_customer_id") or "",
        "payments_order_id": mapping.get("payments_order_id") or "",
        "payments_checkout_session_id": mapping.get("payments_checkout_session_id") or "",
        "payments_receipt_id": mapping.get("payments_receipt_id") or "",
        "current_period_end": mapping.get("current_period_end") or "",
    }


def load_customer_billing(customer_user_id: int) -> dict[str, object]:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT customer_user_id, plan_id, subscription_status, checkout_session_id,
                   stripe_customer_id, stripe_subscription_id, stripe_payment_intent_id,
                   stripe_receipt_url,
                   payments_customer_id, payments_order_id, payments_checkout_session_id,
                   payments_receipt_id, current_period_end
            FROM customer_billing
            WHERE customer_user_id = ?
            """,
            (int(customer_user_id),),
        ).fetchone()
    return serialize_customer_billing_row(row, int(customer_user_id))


def upsert_customer_billing(
    customer_user_id: int,
    plan_id: str,
    status: str,
    checkout_session_id: str | None = None,
    stripe_customer_id: str | None = None,
    stripe_subscription_id: str | None = None,
    stripe_payment_intent_id: str | None = None,
    stripe_receipt_url: str | None = None,
    payments_customer_id: str | None = None,
    payments_order_id: str | None = None,
    payments_checkout_session_id: str | None = None,
    payments_receipt_id: str | None = None,
    current_period_end: str | None = None,
) -> dict[str, object]:
    plan = get_billing_plan(plan_id)
    existing = load_customer_billing(int(customer_user_id))
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO customer_billing (
                customer_user_id, plan_id, subscription_status, checkout_session_id,
                stripe_customer_id, stripe_subscription_id, stripe_payment_intent_id,
                stripe_receipt_url,
                payments_customer_id, payments_order_id,
                payments_checkout_session_id, payments_receipt_id, current_period_end, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_user_id) DO UPDATE SET
                plan_id = excluded.plan_id,
                subscription_status = excluded.subscription_status,
                checkout_session_id = excluded.checkout_session_id,
                stripe_customer_id = excluded.stripe_customer_id,
                stripe_subscription_id = excluded.stripe_subscription_id,
                stripe_payment_intent_id = excluded.stripe_payment_intent_id,
                stripe_receipt_url = excluded.stripe_receipt_url,
                payments_customer_id = excluded.payments_customer_id,
                payments_order_id = excluded.payments_order_id,
                payments_checkout_session_id = excluded.payments_checkout_session_id,
                payments_receipt_id = excluded.payments_receipt_id,
                current_period_end = excluded.current_period_end,
                updated_at = excluded.updated_at
            """,
            (
                int(customer_user_id),
                plan["id"],
                status,
                checkout_session_id if checkout_session_id is not None else existing["checkout_session_id"],
                stripe_customer_id if stripe_customer_id is not None else existing["stripe_customer_id"],
                stripe_subscription_id if stripe_subscription_id is not None else existing["stripe_subscription_id"],
                stripe_payment_intent_id
                if stripe_payment_intent_id is not None
                else existing["stripe_payment_intent_id"],
                stripe_receipt_url if stripe_receipt_url is not None else existing["stripe_receipt_url"],
                payments_customer_id if payments_customer_id is not None else existing["payments_customer_id"],
                payments_order_id if payments_order_id is not None else existing["payments_order_id"],
                payments_checkout_session_id
                if payments_checkout_session_id is not None
                else existing["payments_checkout_session_id"],
                payments_receipt_id if payments_receipt_id is not None else existing["payments_receipt_id"],
                current_period_end if current_period_end is not None else existing["current_period_end"],
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return load_customer_billing(int(customer_user_id))


def record_customer_plan_selection(customer_user_id: int, plan_id: str | None) -> dict[str, object]:
    plan = get_billing_plan(plan_id)
    billing = load_customer_billing(int(customer_user_id))
    status = str(billing["status"] or "not_started")
    if status == "active" and plan["id"] != billing["plan_id"]:
        status = "checkout_started"
    return upsert_customer_billing(int(customer_user_id), str(plan["id"]), status)


def normalize_host(value: str | None) -> str:
    host = (value or "").strip().lower()
    if not host:
        return ""
    if host.startswith("[") and "]" in host:
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def parse_host_list(env_name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    configured = tuple(
        normalized
        for normalized in (normalize_host(part) for part in (os.getenv(env_name, "") or "").split(","))
        if normalized
    )
    return configured or defaults


def get_marketing_hosts() -> tuple[str, ...]:
    return parse_host_list("POWER_MARKETING_HOSTS", DEFAULT_MARKETING_HOSTS)


def get_app_hosts() -> tuple[str, ...]:
    return parse_host_list("POWER_APP_HOSTS", DEFAULT_APP_HOSTS)


def is_marketing_host(host: str | None) -> bool:
    return normalize_host(host) in set(get_marketing_hosts())


def is_app_host(host: str | None) -> bool:
    return normalize_host(host) in set(get_app_hosts())


def current_request_host() -> str:
    if not has_request_context():
        return ""
    forwarded_host = request.headers.get("X-Forwarded-Host")
    if forwarded_host:
        return normalize_host(forwarded_host.split(",", 1)[0])
    return normalize_host(request.host)


def infer_request_scheme() -> str:
    if not has_request_context():
        return "https"
    forwarded_proto = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip()
    if forwarded_proto:
        return forwarded_proto
    scheme = (request.scheme or "").strip()
    return scheme or "https"


def build_base_url_for_host(host: str | None, fallback_url_root: str | None = None) -> str:
    normalized_host = normalize_host(host)
    if not normalized_host:
        return (fallback_url_root or "").strip().rstrip("/")

    if fallback_url_root:
        parsed = urlsplit(fallback_url_root)
        scheme = parsed.scheme or infer_request_scheme()
    else:
        scheme = infer_request_scheme()
    return f"{scheme}://{normalized_host}"


def build_marketing_base_url(fallback_url_root: str | None = None) -> str:
    configured = (os.getenv("POWER_MARKETING_BASE_URL") or "").strip()
    if configured:
        return configured.rstrip("/")

    current_host = current_request_host()
    if current_host and not is_app_host(current_host):
        return build_base_url_for_host(current_host, fallback_url_root)

    marketing_hosts = get_marketing_hosts()
    if marketing_hosts:
        return build_base_url_for_host(marketing_hosts[0], fallback_url_root)
    return (fallback_url_root or "").strip().rstrip("/")


def build_public_base_url(fallback_url_root: str | None = None) -> str:
    configured = (os.getenv("POWER_PUBLIC_BASE_URL") or "").strip()
    if configured:
        return configured.rstrip("/")

    current_host = current_request_host()
    if current_host and not is_marketing_host(current_host):
        return build_base_url_for_host(current_host, fallback_url_root)

    app_hosts = get_app_hosts()
    if app_hosts:
        return build_base_url_for_host(app_hosts[0], fallback_url_root)
    return (fallback_url_root or "").strip().rstrip("/")


def build_absolute_url(base_url: str, path: str) -> str:
    clean_base = (base_url or "").rstrip("/")
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"{clean_base}{clean_path}"


def get_stripe_secret_key() -> str:
    return (os.getenv("STRIPE_SECRET_KEY") or "").strip()


def get_stripe_webhook_secret() -> str:
    return (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()


def get_stripe_api_version() -> str:
    return (os.getenv("STRIPE_API_VERSION") or DEFAULT_STRIPE_API_VERSION).strip()


def get_stripe_account_id() -> str:
    return (os.getenv("STRIPE_ACCOUNT_ID") or "").strip()


def billing_checkout_enabled() -> bool:
    return (os.getenv("POWER_BILLING_ENABLED") or "false").strip().lower() in {"1", "true", "yes"}


def get_stripe_price_id(plan: dict[str, object]) -> str:
    env_name = str(plan.get("stripe_price_env") or "")
    return (os.getenv(env_name) or "").strip()


def extract_mapping_value(obj: object, key: str) -> object:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def normalize_stripe_id(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    candidate = extract_mapping_value(value, "id")
    return str(candidate or "")


def normalize_stripe_timestamp(value: object) -> str:
    if value in {None, ""}:
        return ""
    try:
        return datetime.fromtimestamp(int(value), tz=tz.UTC).isoformat()
    except Exception:
        return str(value)


def configure_stripe() -> None:
    if stripe is None:
        raise ValueError("Payment is not available in this build.")
    secret_key = get_stripe_secret_key()
    if not secret_key:
        raise ValueError("Payment is not connected yet.")
    stripe.api_key = secret_key
    stripe.api_version = get_stripe_api_version()


def build_stripe_metadata(customer_user: dict[str, object], plan: dict[str, object]) -> dict[str, str]:
    customer_user_id = str(customer_user["id"])
    return {
        "project": "home-energy-watch",
        "project_name": "Home Energy Watch",
        "project_domain": "app.homeenergywatch.com",
        "customer_user_id": customer_user_id,
        "plan_id": str(plan["id"]),
        "reference": f"customer_billing:{customer_user_id}:{plan['id']}",
    }


def create_customer_checkout_session(
    customer_user: dict[str, object],
    plan_id: str | None,
    base_url: str,
) -> dict[str, object]:
    if not billing_checkout_enabled():
        raise ValueError("Online payment is not open yet.")
    configure_stripe()
    plan = get_billing_plan(plan_id)
    price_id = get_stripe_price_id(plan)
    if not price_id:
        raise ValueError("Payment is not connected for that plan yet.")
    metadata = build_stripe_metadata(customer_user, plan)
    session_obj = stripe.checkout.Session.create(
        mode="subscription",
        customer_email=str(customer_user["email"]),
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base_url}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/billing/cancel",
        client_reference_id=str(customer_user["id"]),
        metadata=metadata,
        subscription_data={"metadata": metadata},
        allow_promotion_codes=True,
    )
    session_id = normalize_stripe_id(session_obj)
    session_url = str(extract_mapping_value(session_obj, "url") or "")
    if not session_id or not session_url:
        raise ValueError("Payment could not start. Please try again.")
    upsert_customer_billing(
        int(customer_user["id"]),
        str(plan["id"]),
        "checkout_started",
        checkout_session_id=session_id,
        stripe_customer_id=normalize_stripe_id(extract_mapping_value(session_obj, "customer")),
        stripe_subscription_id=normalize_stripe_id(extract_mapping_value(session_obj, "subscription")),
        stripe_payment_intent_id=normalize_stripe_id(extract_mapping_value(session_obj, "payment_intent")),
    )
    return {"id": session_id, "url": session_url}


def create_customer_portal_session(customer_user: dict[str, object], base_url: str) -> dict[str, object]:
    configure_stripe()
    billing = load_customer_billing(int(customer_user["id"]))
    stripe_customer_id = str(billing["stripe_customer_id"] or "")
    if not stripe_customer_id:
        raise ValueError("Billing is not active yet.")
    session_obj = stripe.billing_portal.Session.create(
        customer=stripe_customer_id,
        return_url=f"{base_url}/customer/billing",
    )
    session_url = str(extract_mapping_value(session_obj, "url") or "")
    if not session_url:
        raise ValueError("Billing portal could not start. Please try again.")
    return {"url": session_url}


def stripe_subscription_status_to_billing_status(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized in {"active", "trialing"}:
        return normalized
    if normalized in {"past_due", "unpaid", "incomplete", "incomplete_expired"}:
        return "past_due" if normalized in {"past_due", "unpaid"} else "incomplete"
    if normalized in {"canceled", "cancelled"}:
        return "canceled"
    return normalized or "checkout_started"


def read_checkout_metadata(session_obj: object) -> dict[str, object]:
    metadata = extract_mapping_value(session_obj, "metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def read_subscription_metadata(subscription_obj: object) -> dict[str, object]:
    metadata = extract_mapping_value(subscription_obj, "metadata") or {}
    return metadata if isinstance(metadata, dict) else {}


def extract_stripe_receipt_url(session_obj: object) -> str:
    invoice_obj = extract_mapping_value(session_obj, "invoice")
    hosted_invoice_url = extract_mapping_value(invoice_obj, "hosted_invoice_url")
    if hosted_invoice_url:
        return str(hosted_invoice_url)
    payment_intent = extract_mapping_value(session_obj, "payment_intent")
    charges = extract_mapping_value(payment_intent, "charges")
    data = extract_mapping_value(charges, "data")
    if isinstance(data, list) and data:
        receipt_url = extract_mapping_value(data[0], "receipt_url")
        if receipt_url:
            return str(receipt_url)
    return ""


def refresh_customer_billing_from_stripe(customer_user: dict[str, object], session_id: str | None = None) -> dict[str, object]:
    configure_stripe()
    billing = load_customer_billing(int(customer_user["id"]))
    target_session_id = (session_id or str(billing.get("checkout_session_id") or "")).strip()
    if not target_session_id:
        return billing
    session_obj = stripe.checkout.Session.retrieve(
        target_session_id,
        expand=["subscription", "payment_intent.charges", "invoice"],
    )
    metadata = read_checkout_metadata(session_obj)
    plan_id = str(metadata.get("plan_id") or billing["plan_id"])
    subscription_obj = extract_mapping_value(session_obj, "subscription")
    subscription_id = normalize_stripe_id(subscription_obj)
    subscription_status = str(extract_mapping_value(subscription_obj, "status") or "")
    payment_status = str(extract_mapping_value(session_obj, "payment_status") or "")
    billing_status = stripe_subscription_status_to_billing_status(subscription_status)
    if not subscription_status and payment_status == "paid":
        billing_status = "active"
    current_period_end = normalize_stripe_timestamp(extract_mapping_value(subscription_obj, "current_period_end"))
    return upsert_customer_billing(
        int(customer_user["id"]),
        plan_id,
        billing_status,
        checkout_session_id=target_session_id,
        stripe_customer_id=normalize_stripe_id(extract_mapping_value(session_obj, "customer")),
        stripe_subscription_id=subscription_id,
        stripe_payment_intent_id=normalize_stripe_id(extract_mapping_value(session_obj, "payment_intent")),
        stripe_receipt_url=extract_stripe_receipt_url(session_obj),
        current_period_end=current_period_end,
    )


def handle_stripe_event(event: object) -> None:
    event_type = str(extract_mapping_value(event, "type") or "")
    data = extract_mapping_value(event, "data") or {}
    event_object = extract_mapping_value(data, "object") if not isinstance(data, dict) else data.get("object")
    if event_object is None:
        return

    if event_type == "checkout.session.completed":
        metadata = read_checkout_metadata(event_object)
        customer_user_id = metadata.get("customer_user_id")
        if not customer_user_id:
            return
        plan_id = str(metadata.get("plan_id") or "home")
        payment_status = str(extract_mapping_value(event_object, "payment_status") or "")
        status = "active" if payment_status in {"paid", "no_payment_required"} else "checkout_started"
        upsert_customer_billing(
            int(customer_user_id),
            plan_id,
            status,
            checkout_session_id=normalize_stripe_id(event_object),
            stripe_customer_id=normalize_stripe_id(extract_mapping_value(event_object, "customer")),
            stripe_subscription_id=normalize_stripe_id(extract_mapping_value(event_object, "subscription")),
            stripe_payment_intent_id=normalize_stripe_id(extract_mapping_value(event_object, "payment_intent")),
        )
        return

    if event_type in {"customer.subscription.updated", "customer.subscription.deleted"}:
        metadata = read_subscription_metadata(event_object)
        customer_user_id = metadata.get("customer_user_id")
        if not customer_user_id:
            return
        plan_id = str(metadata.get("plan_id") or "home")
        status = "canceled" if event_type == "customer.subscription.deleted" else stripe_subscription_status_to_billing_status(
            str(extract_mapping_value(event_object, "status") or "")
        )
        upsert_customer_billing(
            int(customer_user_id),
            plan_id,
            status,
            stripe_customer_id=normalize_stripe_id(extract_mapping_value(event_object, "customer")),
            stripe_subscription_id=normalize_stripe_id(event_object),
            current_period_end=normalize_stripe_timestamp(extract_mapping_value(event_object, "current_period_end")),
        )


def serialize_account_row(row: sqlite3.Row | tuple | None) -> dict[str, object] | None:
    if row is None:
        return None
    mapping = dict(row)
    return {
        "id": int(mapping["id"]),
        "account_number": mapping["account_number"],
        "display_name": mapping["display_name"],
        "energy_company": mapping.get("energy_company") or "",
        "baseline_date": mapping["baseline_date"],
        "address": mapping.get("address") or "",
    }


def get_or_create_account(
    conn: sqlite3.Connection,
    account_number: str | None,
    display_name: str | None = None,
    energy_company: str | None = None,
    baseline_date: str | None = None,
) -> dict[str, object]:
    normalized_number = normalize_account_number(account_number)
    normalized_date = normalize_optional_date(baseline_date) if baseline_date is not None else None
    normalized_energy_company = clean_energy_company(energy_company)
    existing = conn.execute(
        """
        SELECT id, account_number, display_name, energy_company, baseline_date
        FROM accounts
        WHERE account_number = ?
        """,
        (normalized_number,),
    ).fetchone()
    if existing is not None:
        current = dict(existing)
        updates: list[str] = []
        values: list[object] = []
        normalized_name = (display_name or "").strip()
        if normalized_name and normalized_name != current["display_name"]:
            updates.append("display_name = ?")
            values.append(normalized_name)
        if energy_company is not None and normalized_energy_company != (current.get("energy_company") or ""):
            updates.append("energy_company = ?")
            values.append(normalized_energy_company)
        if baseline_date is not None and normalized_date != current["baseline_date"]:
            updates.append("baseline_date = ?")
            values.append(normalized_date)
        if updates:
            updates.append("updated_at = ?")
            values.append(timestamp_now())
            values.append(normalized_number)
            conn.execute(
                f"UPDATE accounts SET {', '.join(updates)} WHERE account_number = ?",
                values,
            )
            existing = conn.execute(
                """
                SELECT id, account_number, display_name, energy_company, baseline_date
                FROM accounts
                WHERE account_number = ?
                """,
                (normalized_number,),
            ).fetchone()
        return serialize_account_row(existing) or {}

    timestamp = timestamp_now()
    account_label = (display_name or "").strip() or normalized_energy_company or normalized_number
    conn.execute(
        """
        INSERT INTO accounts (account_number, display_name, energy_company, baseline_date, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            normalized_number,
            account_label,
            normalized_energy_company,
            normalized_date,
            timestamp,
            timestamp,
        ),
    )
    created = conn.execute(
        """
        SELECT id, account_number, display_name, energy_company, baseline_date
        FROM accounts
        WHERE account_number = ?
        """,
        (normalized_number,),
    ).fetchone()
    return serialize_account_row(created) or {}


def list_accounts() -> list[dict[str, object]]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT accounts.id, accounts.account_number, accounts.display_name, accounts.energy_company, accounts.baseline_date,
                   household_profiles.address
            FROM accounts
            LEFT JOIN household_profiles ON household_profiles.account_id = accounts.id
            ORDER BY CASE WHEN account_number = ? THEN 0 ELSE 1 END, display_name, account_number
            """,
            (DEFAULT_ACCOUNT_NUMBER,),
        ).fetchall()
    accounts: list[dict[str, object]] = []
    for row in rows:
        serialized = serialize_account_row(row)
        if serialized is not None:
            accounts.append(serialized)
    return accounts


def clean_search_text(value: str | None) -> str:
    return (value or "").strip()


def list_account_page(search: str | None = None, page: int = 1, per_page: int = 10) -> dict[str, object]:
    normalized_search = clean_search_text(search)
    safe_page = max(1, int(page or 1))
    safe_per_page = min(20, max(10, int(per_page or 10)))
    where_clause = ""
    params: list[object] = []
    if normalized_search:
        like_value = f"%{normalized_search.lower()}%"
        where_clause = """
            WHERE LOWER(accounts.display_name) LIKE ?
               OR LOWER(accounts.account_number) LIKE ?
               OR LOWER(COALESCE(accounts.energy_company, '')) LIKE ?
               OR LOWER(COALESCE(household_profiles.address, '')) LIKE ?
        """
        params.extend([like_value, like_value, like_value, like_value])

    with get_db_connection() as conn:
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM accounts
            LEFT JOIN household_profiles ON household_profiles.account_id = accounts.id
            {where_clause}
            """,
            tuple(params),
        ).fetchone()
        total = 0 if total_row is None else int(total_row["count"])
        total_pages = max(1, (total + safe_per_page - 1) // safe_per_page)
        safe_page = min(safe_page, total_pages)
        offset = (safe_page - 1) * safe_per_page
        rows = conn.execute(
            f"""
            SELECT accounts.id, accounts.account_number, accounts.display_name, accounts.energy_company, accounts.baseline_date,
                   household_profiles.address
            FROM accounts
            LEFT JOIN household_profiles ON household_profiles.account_id = accounts.id
            {where_clause}
            ORDER BY CASE WHEN accounts.account_number = ? THEN 0 ELSE 1 END,
                     accounts.display_name, accounts.account_number
            LIMIT ? OFFSET ?
            """,
            tuple([*params, DEFAULT_ACCOUNT_NUMBER, safe_per_page, offset]),
        ).fetchall()

    accounts: list[dict[str, object]] = []
    for row in rows:
        serialized = serialize_account_row(row)
        if serialized is not None:
            accounts.append(serialized)
    return {
        "accounts": accounts,
        "search": normalized_search,
        "page": safe_page,
        "per_page": safe_per_page,
        "total": total,
        "total_pages": total_pages,
        "has_previous": safe_page > 1,
        "has_next": safe_page < total_pages,
        "previous_page": safe_page - 1 if safe_page > 1 else None,
        "next_page": safe_page + 1 if safe_page < total_pages else None,
    }


def find_account(account_number: str | None) -> dict[str, object] | None:
    normalized = normalize_account_number(account_number)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT accounts.id, accounts.account_number, accounts.display_name, accounts.energy_company, accounts.baseline_date,
                   household_profiles.address
            FROM accounts
            LEFT JOIN household_profiles ON household_profiles.account_id = accounts.id
            WHERE accounts.account_number = ?
            """,
            (normalized,),
        ).fetchone()
    return serialize_account_row(row)


def list_customer_account_page(
    email: str,
    search: str | None = None,
    page: int = 1,
    per_page: int = 10,
) -> dict[str, object]:
    normalized_email = clean_email(email)
    normalized_search = clean_search_text(search)
    safe_page = max(1, int(page or 1))
    safe_per_page = min(20, max(10, int(per_page or 10)))
    search_clause = ""
    params: list[object] = [normalized_email]
    if normalized_search:
        like_value = f"%{normalized_search.lower()}%"
        search_clause = """
            AND (
                LOWER(accounts.display_name) LIKE ?
                OR LOWER(accounts.account_number) LIKE ?
                OR LOWER(COALESCE(accounts.energy_company, '')) LIKE ?
                OR LOWER(COALESCE(household_profiles.address, '')) LIKE ?
            )
        """
        params.extend([like_value, like_value, like_value, like_value])

    with get_db_connection() as conn:
        total_row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM account_access_emails
            JOIN accounts ON accounts.id = account_access_emails.account_id
            LEFT JOIN household_profiles ON household_profiles.account_id = accounts.id
            WHERE account_access_emails.email = ?
            {search_clause}
            """,
            tuple(params),
        ).fetchone()
        total = 0 if total_row is None else int(total_row["count"])
        total_pages = max(1, (total + safe_per_page - 1) // safe_per_page)
        safe_page = min(safe_page, total_pages)
        offset = (safe_page - 1) * safe_per_page
        rows = conn.execute(
            f"""
            SELECT accounts.id, accounts.account_number, accounts.display_name, accounts.energy_company, accounts.baseline_date,
                   household_profiles.address
            FROM account_access_emails
            JOIN accounts ON accounts.id = account_access_emails.account_id
            LEFT JOIN household_profiles ON household_profiles.account_id = accounts.id
            WHERE account_access_emails.email = ?
            {search_clause}
            ORDER BY accounts.display_name, accounts.account_number
            LIMIT ? OFFSET ?
            """,
            tuple([*params, safe_per_page, offset]),
        ).fetchall()

    accounts: list[dict[str, object]] = []
    for row in rows:
        serialized = serialize_account_row(row)
        if serialized is not None:
            accounts.append(serialized)
    return {
        "accounts": accounts,
        "search": normalized_search,
        "page": safe_page,
        "per_page": safe_per_page,
        "total": total,
        "total_pages": total_pages,
        "has_previous": safe_page > 1,
        "has_next": safe_page < total_pages,
        "previous_page": safe_page - 1 if safe_page > 1 else None,
        "next_page": safe_page + 1 if safe_page < total_pages else None,
    }


def customer_has_account_access(email: str, account_number: str | None) -> bool:
    return get_customer_account_access(email, account_number) is not None


def get_customer_account_access(email: str, account_number: str | None) -> dict[str, object] | None:
    normalized_email = clean_email(email)
    normalized_account = normalize_account_number(account_number)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT account_access_emails.id, account_access_emails.access_level
            FROM account_access_emails
            JOIN accounts ON accounts.id = account_access_emails.account_id
            WHERE account_access_emails.email = ? AND accounts.account_number = ?
            """,
            (normalized_email, normalized_account),
        ).fetchone()
    if row is None:
        return None
    return {"id": int(row["id"]), "access_level": str(row["access_level"])}


def choose_customer_account_number(email: str, requested_account_number: str | None = None) -> str | None:
    if requested_account_number and customer_has_account_access(email, requested_account_number):
        return normalize_account_number(requested_account_number)
    account_page = list_customer_account_page(email, page=1, per_page=10)
    if not account_page["accounts"]:
        return None
    return str(account_page["accounts"][0]["account_number"])


def load_account(account_number: str | None = None) -> dict[str, object]:
    normalized = normalize_account_number(account_number)
    with get_db_connection() as conn:
        account = get_or_create_account(conn, normalized)
        conn.commit()
    return account


def save_account_profile(
    account_number: str | None,
    display_name: str | None = None,
    energy_company: str | None = None,
    baseline_date: str | None = None,
) -> dict[str, object]:
    with get_db_connection() as conn:
        account = get_or_create_account(
            conn,
            account_number,
            display_name=display_name,
            energy_company=energy_company,
            baseline_date=baseline_date,
        )
        conn.commit()
    return account


def serialize_account_access_row(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    mapping = dict(row)
    return {
        "id": int(mapping["id"]),
        "email": mapping["email"],
        "full_name": mapping.get("full_name") or "",
        "access_level": mapping.get("access_level") or "Viewer",
    }


def list_account_access_emails(account_number: str | None) -> list[dict[str, object]]:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        rows = conn.execute(
            """
            SELECT id, email, full_name, access_level
            FROM account_access_emails
            WHERE account_id = ?
            ORDER BY email
            """,
            (account["id"],),
        ).fetchall()
    access: list[dict[str, object]] = []
    for row in rows:
        serialized = serialize_account_access_row(row)
        if serialized is not None:
            access.append(serialized)
    return access


def add_account_access_email(
    account_number: str | None,
    email: str,
    full_name: str | None = None,
    access_level: str = "Viewer",
) -> dict[str, object]:
    normalized_email = clean_email(email)
    normalized_name = clean_optional_text(full_name)
    normalized_access = clean_optional_text(access_level) or "Viewer"
    if normalized_access not in CUSTOMER_ACCESS_LEVELS:
        raise ValueError("Choose a valid account access level.")
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        conn.execute(
            """
            INSERT INTO account_access_emails (
                account_id, email, full_name, access_level, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, email) DO UPDATE SET
                full_name = excluded.full_name,
                access_level = excluded.access_level,
                updated_at = excluded.updated_at
            """,
            (
                account["id"],
                normalized_email,
                normalized_name,
                normalized_access,
                timestamp,
                timestamp,
            ),
        )
        conn.commit()
    return next(item for item in list_account_access_emails(account_number) if item["email"] == normalized_email)


def delete_account_access_email(account_number: str | None, access_id: int) -> dict[str, object]:
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        access_row = conn.execute(
            """
            SELECT account_access_emails.email, customer_users.id AS customer_user_id
            FROM account_access_emails
            LEFT JOIN customer_users ON customer_users.email = account_access_emails.email
            WHERE account_access_emails.account_id = ? AND account_access_emails.id = ?
            """,
            (account["id"], int(access_id)),
        ).fetchone()
        if access_row is None:
            raise ValueError("That account access record could not be found.")
        customer_user_id = access_row["customer_user_id"]
        revoked_ids: list[int] = []
        if customer_user_id is not None:
            active_rows = conn.execute(
                """
                SELECT id
                FROM account_data_authorizations
                WHERE account_id = ? AND customer_user_id = ? AND status = 'active'
                """,
                (account["id"], int(customer_user_id)),
            ).fetchall()
            revoked_ids = [int(row["id"]) for row in active_rows]
            if revoked_ids:
                conn.execute(
                    """
                    UPDATE account_data_authorizations
                    SET status = 'revoked_access_removed', revoked_at = ?, updated_at = ?
                    WHERE account_id = ? AND customer_user_id = ? AND status = 'active'
                    """,
                    (timestamp, timestamp, account["id"], int(customer_user_id)),
                )
        conn.execute(
            "DELETE FROM account_access_emails WHERE account_id = ? AND id = ?",
            (account["id"], int(access_id)),
        )
        credentials_cleared = clear_utility_access_if_unauthorized(
            conn,
            account_id=int(account["id"]),
            timestamp=timestamp,
            reason="The authorizing customer's account access was removed.",
        )
    return {
        "email": str(access_row["email"]),
        "authorization_ids": revoked_ids,
        "credentials_cleared": credentials_cleared,
    }


def record_customer_policy_acceptance(
    conn: DatabaseConnection,
    customer_user_id: int,
    *,
    accepted_at: str,
    remote_hash: str | None,
    user_agent_hash: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO customer_policy_acceptances (
            customer_user_id, terms_version, privacy_version, accepted_at,
            remote_hash, user_agent_hash
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_user_id, terms_version, privacy_version) DO UPDATE SET
            accepted_at = excluded.accepted_at,
            remote_hash = excluded.remote_hash,
            user_agent_hash = excluded.user_agent_hash
        """,
        (
            int(customer_user_id),
            CURRENT_TERMS_VERSION,
            CURRENT_PRIVACY_VERSION,
            accepted_at,
            remote_hash,
            user_agent_hash,
        ),
    )


def create_account_data_authorization(
    conn: DatabaseConnection,
    *,
    account_id: int,
    customer_user_id: int,
    granted_at: str,
    remote_hash: str | None,
    user_agent_hash: str | None,
) -> None:
    conn.execute(
        """
        UPDATE account_data_authorizations
        SET status = 'superseded', revoked_at = ?, updated_at = ?
        WHERE account_id = ? AND customer_user_id = ? AND status = 'active'
        """,
        (granted_at, granted_at, int(account_id), int(customer_user_id)),
    )
    conn.execute(
        """
        INSERT INTO account_data_authorizations (
            account_id, customer_user_id, authorization_version, authorization_scope,
            status, granted_at, revoked_at, remote_hash, user_agent_hash, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'active', ?, NULL, ?, ?, ?, ?)
        """,
        (
            int(account_id),
            int(customer_user_id),
            CURRENT_UTILITY_AUTHORIZATION_VERSION,
            UTILITY_AUTHORIZATION_SCOPE,
            granted_at,
            remote_hash,
            user_agent_hash,
            granted_at,
            granted_at,
        ),
    )


def serialize_account_data_authorization(row) -> dict[str, object] | None:
    if row is None:
        return None
    mapping = dict(row)
    return {
        "id": int(mapping["id"]),
        "customer_user_id": int(mapping["customer_user_id"]),
        "customer_email": mapping.get("customer_email") or "",
        "customer_name": mapping.get("customer_name") or "",
        "authorization_version": mapping["authorization_version"],
        "authorization_scope": mapping["authorization_scope"],
        "status": mapping["status"],
        "active": mapping["status"] == "active",
        "granted_at": mapping["granted_at"],
        "revoked_at": mapping.get("revoked_at"),
    }


def list_account_data_authorizations(account_number: str | None) -> list[dict[str, object]]:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        rows = conn.execute(
            """
            SELECT account_data_authorizations.id,
                   account_data_authorizations.customer_user_id,
                   customer_users.email AS customer_email,
                   customer_users.full_name AS customer_name,
                   account_data_authorizations.authorization_version,
                   account_data_authorizations.authorization_scope,
                   account_data_authorizations.status,
                   account_data_authorizations.granted_at,
                   account_data_authorizations.revoked_at
            FROM account_data_authorizations
            JOIN customer_users ON customer_users.id = account_data_authorizations.customer_user_id
            WHERE account_data_authorizations.account_id = ?
            ORDER BY account_data_authorizations.updated_at DESC, account_data_authorizations.id DESC
            """,
            (account["id"],),
        ).fetchall()
    return [
        authorization
        for row in rows
        if (authorization := serialize_account_data_authorization(row)) is not None
    ]


def account_has_active_data_authorization(account_number: str | None) -> bool:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        row = conn.execute(
            """
            SELECT id
            FROM account_data_authorizations
            WHERE account_id = ? AND status = 'active'
            LIMIT 1
            """,
            (account["id"],),
        ).fetchone()
    return row is not None


def get_customer_account_data_authorization(
    account_number: str | None,
    customer_user_id: int,
) -> dict[str, object] | None:
    return next(
        (
            authorization
            for authorization in list_account_data_authorizations(account_number)
            if int(authorization["customer_user_id"]) == int(customer_user_id)
        ),
        None,
    )


def ensure_customer_manages_account(
    conn: DatabaseConnection,
    *,
    account_id: int,
    customer_user_id: int,
) -> None:
    row = conn.execute(
        """
        SELECT account_access_emails.id
        FROM account_access_emails
        JOIN customer_users ON customer_users.email = account_access_emails.email
        WHERE account_access_emails.account_id = ?
          AND customer_users.id = ?
          AND account_access_emails.access_level = 'Manager'
        """,
        (int(account_id), int(customer_user_id)),
    ).fetchone()
    if row is None:
        raise ValueError("Manager access is required to change data permission for this account.")


def clear_utility_access_if_unauthorized(
    conn: DatabaseConnection,
    *,
    account_id: int,
    timestamp: str,
    reason: str,
) -> bool:
    remaining = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM account_data_authorizations
        WHERE account_id = ? AND status = 'active'
        """,
        (int(account_id),),
    ).fetchone()
    if int(remaining["count"] if remaining is not None else 0) > 0:
        return False
    cursor = conn.execute(
        """
        UPDATE utility_connections
        SET access_identifier = NULL, secret_hash = NULL, secret_token = NULL,
            secret_last4 = NULL, status = 'Authorization withdrawn',
            last_sync_status = 'authorization_revoked',
            last_sync_error = ?, updated_at = ?
        WHERE account_id = ?
        """,
        (reason, timestamp, int(account_id)),
    )
    return int(cursor.rowcount or 0) > 0


def grant_account_data_authorization(
    account_number: str | None,
    customer_user_id: int,
    *,
    remote_hash: str | None = None,
    user_agent_hash: str | None = None,
) -> dict[str, object]:
    granted_at = timestamp_now()
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        ensure_customer_manages_account(
            conn,
            account_id=int(account["id"]),
            customer_user_id=int(customer_user_id),
        )
        create_account_data_authorization(
            conn,
            account_id=int(account["id"]),
            customer_user_id=int(customer_user_id),
            granted_at=granted_at,
            remote_hash=remote_hash,
            user_agent_hash=user_agent_hash,
        )
    authorization = get_customer_account_data_authorization(account_number, customer_user_id)
    if authorization is None:
        raise RuntimeError("The data permission could not be saved.")
    return authorization


def revoke_account_data_authorization(
    account_number: str | None,
    customer_user_id: int,
) -> dict[str, object]:
    revoked_at = timestamp_now()
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        ensure_customer_manages_account(
            conn,
            account_id=int(account["id"]),
            customer_user_id=int(customer_user_id),
        )
        active = conn.execute(
            """
            SELECT id
            FROM account_data_authorizations
            WHERE account_id = ? AND customer_user_id = ? AND status = 'active'
            """,
            (account["id"], int(customer_user_id)),
        ).fetchall()
        if not active:
            raise ValueError("Data permission is already withdrawn for this account.")
        conn.execute(
            """
            UPDATE account_data_authorizations
            SET status = 'revoked', revoked_at = ?, updated_at = ?
            WHERE account_id = ? AND customer_user_id = ? AND status = 'active'
            """,
            (revoked_at, revoked_at, account["id"], int(customer_user_id)),
        )
        clear_utility_access_if_unauthorized(
            conn,
            account_id=int(account["id"]),
            timestamp=revoked_at,
            reason="Customer authorization was withdrawn.",
        )
    authorization = get_customer_account_data_authorization(account_number, customer_user_id)
    if authorization is None:
        raise RuntimeError("The data permission could not be updated.")
    return authorization


def build_secret_hash(value: str | None) -> str | None:
    secret_value = (value or "").strip()
    if not secret_value:
        return None
    return hmac.new(
        get_app_secret().encode("utf-8"),
        secret_value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_legacy_secret_key_stream(length: int) -> bytes:
    salt = get_app_secret()
    digest = hashlib.sha256(salt.encode("utf-8")).digest()
    repeats = (length // len(digest)) + 1
    return (digest * repeats)[:length]


def seal_secret_value(value: str | None) -> str | None:
    secret_value = (value or "").strip()
    if not secret_value:
        return None
    sealed = get_data_encryption_cipher().encrypt(secret_value.encode("utf-8")).decode("ascii")
    return f"fernet:v1:{sealed}"


def unseal_secret_value(value: str | None) -> str | None:
    token = (value or "").strip()
    if not token:
        return None
    try:
        if token.startswith("fernet:v1:"):
            sealed_token = token.removeprefix("fernet:v1:")
            return get_data_encryption_cipher().decrypt(sealed_token.encode("ascii")).decode("utf-8")
        sealed = base64.urlsafe_b64decode(token.encode("ascii"))
        key_stream = build_legacy_secret_key_stream(len(sealed))
        payload = bytes(byte ^ key_stream[index] for index, byte in enumerate(sealed))
        return payload.decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError("The saved access key could not be read. Save the connection again.") from exc


def staff_mfa_required() -> bool:
    return (os.getenv("POWER_STAFF_MFA_REQUIRED") or "false").strip().lower() in {"1", "true", "yes"}


def normalize_mfa_code(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def staff_mfa_recovery_code_hash(code: str | None) -> str:
    normalized = normalize_mfa_code(code)
    if not normalized:
        return ""
    key = f"{get_app_secret()}:staff-mfa-recovery".encode("utf-8")
    return hmac.new(key, normalized.encode("ascii"), hashlib.sha256).hexdigest()


def generate_staff_mfa_recovery_codes() -> list[str]:
    codes: list[str] = []
    while len(codes) < MFA_RECOVERY_CODE_COUNT:
        raw = "".join(secrets.choice(MFA_RECOVERY_CODE_ALPHABET) for _ in range(12))
        formatted = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
        if formatted not in codes:
            codes.append(formatted)
    return codes


def save_staff_mfa_recovery_codes(
    conn: DatabaseConnection,
    staff_user_id: int,
    codes: list[str],
) -> None:
    conn.execute("DELETE FROM staff_mfa_recovery_codes WHERE staff_user_id = ?", (int(staff_user_id),))
    created_at = timestamp_now()
    for code in codes:
        conn.execute(
            """
            INSERT INTO staff_mfa_recovery_codes (
                staff_user_id, code_hash, consumed_at, created_at
            )
            VALUES (?, ?, NULL, ?)
            """,
            (int(staff_user_id), staff_mfa_recovery_code_hash(code), created_at),
        )


def count_staff_mfa_recovery_codes(staff_user_id: int) -> int:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM staff_mfa_recovery_codes
            WHERE staff_user_id = ? AND consumed_at IS NULL
            """,
            (int(staff_user_id),),
        ).fetchone()
    return 0 if row is None else int(row["count"])


def build_staff_mfa_qr_data_uri(provisioning_uri: str) -> str:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=6,
        border=2,
    )
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#111111", back_color="#ffffff")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(output.getvalue()).decode('ascii')}"


def begin_staff_mfa_enrollment(staff_user_id: int) -> dict[str, object]:
    staff_user = get_staff_user_by_id(int(staff_user_id))
    if staff_user is None or not staff_user.get("is_active"):
        raise ValueError("That staff account is no longer available.")
    if staff_user.get("mfa_enabled"):
        raise ValueError("Authenticator protection is already on.")
    secret = pyotp.random_base32()
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE staff_users
            SET mfa_pending_secret_token = ?, mfa_pending_created_at = ?, updated_at = ?
            WHERE id = ? AND is_active = 1 AND mfa_enabled_at IS NULL
            """,
            (seal_secret_value(secret), timestamp, timestamp, int(staff_user_id)),
        )
        conn.commit()
    return load_staff_mfa_enrollment(int(staff_user_id)) or {}


def load_staff_mfa_enrollment(staff_user_id: int) -> dict[str, object] | None:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT email, mfa_pending_secret_token, mfa_pending_created_at, mfa_enabled_at
            FROM staff_users
            WHERE id = ? AND is_active = 1
            """,
            (int(staff_user_id),),
        ).fetchone()
    if row is None or row["mfa_enabled_at"] or not row["mfa_pending_secret_token"]:
        return None
    created_at = datetime.fromisoformat(str(row["mfa_pending_created_at"]))
    if created_at + timedelta(minutes=MFA_ENROLLMENT_EXPIRY_MINUTES) <= datetime.now():
        cancel_staff_mfa_enrollment(int(staff_user_id))
        return None
    try:
        secret = unseal_secret_value(str(row["mfa_pending_secret_token"])) or ""
    except ValueError as exc:
        raise ValueError("The authenticator setup could not be read. Start again.") from exc
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(
        name=str(row["email"]),
        issuer_name=MFA_ISSUER,
    )
    return {
        "secret": secret,
        "display_secret": " ".join(secret[index : index + 4] for index in range(0, len(secret), 4)),
        "qr_data_uri": build_staff_mfa_qr_data_uri(provisioning_uri),
        "expires_at": (created_at + timedelta(minutes=MFA_ENROLLMENT_EXPIRY_MINUTES)).isoformat(timespec="seconds"),
    }


def cancel_staff_mfa_enrollment(staff_user_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE staff_users
            SET mfa_pending_secret_token = NULL, mfa_pending_created_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (timestamp_now(), int(staff_user_id)),
        )
        conn.commit()


def matching_totp_counter(secret: str, code: str | None, last_counter: int | None = None) -> int | None:
    normalized = normalize_mfa_code(code)
    if len(normalized) != 6 or not normalized.isdigit():
        return None
    totp = pyotp.TOTP(secret)
    current_counter = int(time.time()) // int(totp.interval)
    for counter in range(current_counter - 1, current_counter + 2):
        if last_counter is not None and counter <= int(last_counter):
            continue
        if hmac.compare_digest(totp.generate_otp(counter), normalized):
            return counter
    return None


def confirm_staff_mfa_enrollment(staff_user_id: int, code: str | None) -> dict[str, object]:
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        if conn.kind == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(?)", (POSTGRES_STAFF_LOCK_KEY,))
        else:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT mfa_pending_secret_token, mfa_pending_created_at, mfa_enabled_at
            FROM staff_users
            WHERE id = ? AND is_active = 1
            """,
            (int(staff_user_id),),
        ).fetchone()
        if row is None or row["mfa_enabled_at"] or not row["mfa_pending_secret_token"]:
            conn.rollback()
            raise ValueError("Start authenticator setup again.")
        created_at = datetime.fromisoformat(str(row["mfa_pending_created_at"]))
        if created_at + timedelta(minutes=MFA_ENROLLMENT_EXPIRY_MINUTES) <= datetime.now():
            conn.rollback()
            cancel_staff_mfa_enrollment(int(staff_user_id))
            raise ValueError("Authenticator setup expired. Start again.")
        try:
            secret = unseal_secret_value(str(row["mfa_pending_secret_token"])) or ""
        except ValueError as exc:
            conn.rollback()
            raise ValueError("The authenticator setup could not be read. Start again.") from exc
        counter = matching_totp_counter(secret, code)
        if counter is None:
            conn.rollback()
            raise ValueError("That authenticator code did not work.")
        recovery_codes = generate_staff_mfa_recovery_codes()
        conn.execute(
            """
            UPDATE staff_users
            SET mfa_secret_token = mfa_pending_secret_token,
                mfa_pending_secret_token = NULL,
                mfa_pending_created_at = NULL,
                mfa_enabled_at = ?,
                mfa_last_counter = ?,
                auth_version = auth_version + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (timestamp, counter, timestamp, int(staff_user_id)),
        )
        save_staff_mfa_recovery_codes(conn, int(staff_user_id), recovery_codes)
        conn.commit()
    return {
        "staff_user": get_staff_user_by_id(int(staff_user_id)) or {},
        "recovery_codes": recovery_codes,
    }


def verify_staff_mfa_code(staff_user_id: int, code: str | None) -> str | None:
    normalized = normalize_mfa_code(code)
    if not normalized:
        return None
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        if conn.kind == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(?)", (POSTGRES_STAFF_LOCK_KEY,))
        else:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT mfa_secret_token, mfa_enabled_at, mfa_last_counter
            FROM staff_users
            WHERE id = ? AND is_active = 1
            """,
            (int(staff_user_id),),
        ).fetchone()
        if row is None or not row["mfa_enabled_at"] or not row["mfa_secret_token"]:
            conn.rollback()
            return None
        try:
            secret = unseal_secret_value(str(row["mfa_secret_token"])) or ""
        except ValueError:
            conn.rollback()
            return None
        counter = matching_totp_counter(secret, normalized, row["mfa_last_counter"])
        if counter is not None:
            conn.execute(
                "UPDATE staff_users SET mfa_last_counter = ?, updated_at = ? WHERE id = ?",
                (counter, timestamp, int(staff_user_id)),
            )
            conn.commit()
            return "authenticator"
        recovery_hash = staff_mfa_recovery_code_hash(normalized)
        recovery_row = conn.execute(
            """
            SELECT id
            FROM staff_mfa_recovery_codes
            WHERE staff_user_id = ? AND code_hash = ? AND consumed_at IS NULL
            """,
            (int(staff_user_id), recovery_hash),
        ).fetchone()
        if recovery_row is None:
            conn.rollback()
            return None
        cursor = conn.execute(
            """
            UPDATE staff_mfa_recovery_codes
            SET consumed_at = ?
            WHERE id = ? AND consumed_at IS NULL
            """,
            (timestamp, int(recovery_row["id"])),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return "recovery_code"


def verify_staff_password(staff_user_id: int, password: str | None) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT password_hash FROM staff_users WHERE id = ? AND is_active = 1",
            (int(staff_user_id),),
        ).fetchone()
    return bool(row is not None and row["password_hash"] and check_password_hash(row["password_hash"], password or ""))


def disable_staff_mfa(staff_user_id: int, password: str | None, code: str | None) -> dict[str, object]:
    if not verify_staff_password(int(staff_user_id), password):
        raise ValueError("That password did not work.")
    if verify_staff_mfa_code(int(staff_user_id), code) is None:
        raise ValueError("That authenticator or recovery code did not work.")
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE staff_users
            SET mfa_secret_token = NULL, mfa_pending_secret_token = NULL,
                mfa_pending_created_at = NULL, mfa_enabled_at = NULL,
                mfa_last_counter = NULL, auth_version = auth_version + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (timestamp, int(staff_user_id)),
        )
        conn.execute("DELETE FROM staff_mfa_recovery_codes WHERE staff_user_id = ?", (int(staff_user_id),))
        conn.commit()
    return get_staff_user_by_id(int(staff_user_id)) or {}


def regenerate_staff_mfa_recovery_codes(staff_user_id: int, code: str | None) -> dict[str, object]:
    if verify_staff_mfa_code(int(staff_user_id), code) is None:
        raise ValueError("That authenticator or recovery code did not work.")
    recovery_codes = generate_staff_mfa_recovery_codes()
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        save_staff_mfa_recovery_codes(conn, int(staff_user_id), recovery_codes)
        conn.execute(
            "UPDATE staff_users SET auth_version = auth_version + 1, updated_at = ? WHERE id = ?",
            (timestamp, int(staff_user_id)),
        )
        conn.commit()
    return {
        "staff_user": get_staff_user_by_id(int(staff_user_id)) or {},
        "recovery_codes": recovery_codes,
    }


def reset_staff_mfa_by_commissioner(
    staff_user_id: int,
    *,
    acting_staff_user_id: int,
) -> dict[str, object]:
    target_id = int(staff_user_id)
    actor_id = int(acting_staff_user_id)
    if target_id == actor_id:
        raise ValueError("Another commissioner must reset your authenticator.")
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        if conn.kind == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(?)", (POSTGRES_STAFF_LOCK_KEY,))
        else:
            conn.execute("BEGIN IMMEDIATE")
        actor = conn.execute(
            "SELECT role, is_active FROM staff_users WHERE id = ?",
            (actor_id,),
        ).fetchone()
        target = conn.execute(
            """
            SELECT id, email, full_name, role, is_active, invite_token, invite_token_hash,
                   invite_expires_at, password_hash, auth_version, mfa_enabled_at, last_login_at
            FROM staff_users
            WHERE id = ?
            """,
            (target_id,),
        ).fetchone()
        if actor is None or not bool(actor["is_active"]) or actor["role"] != "Commissioner":
            conn.rollback()
            raise ValueError("Commissioner access is required for that action.")
        if target is None:
            conn.rollback()
            raise ValueError("That staff member is no longer available.")
        before = serialize_staff_user_row(target) or {}
        if not before.get("mfa_enabled"):
            conn.rollback()
            raise ValueError("Authenticator protection is already off for that staff member.")
        conn.execute(
            """
            UPDATE staff_users
            SET mfa_secret_token = NULL, mfa_pending_secret_token = NULL,
                mfa_pending_created_at = NULL, mfa_enabled_at = NULL,
                mfa_last_counter = NULL, auth_version = auth_version + 1,
                updated_at = ?
            WHERE id = ?
            """,
            (timestamp, target_id),
        )
        conn.execute("DELETE FROM staff_mfa_recovery_codes WHERE staff_user_id = ?", (target_id,))
        conn.commit()
    after = get_staff_user_by_id(target_id)
    if after is None:
        raise RuntimeError("The authenticator reset could not be confirmed.")
    return {"before": before, "after": after}


def build_secret_last4(value: str | None) -> str | None:
    secret_value = (value or "").strip()
    if not secret_value:
        return None
    return secret_value[-4:]


def serialize_utility_connection_row(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    mapping = dict(row)
    return {
        "id": int(mapping["id"]),
        "provider_name": mapping["provider_name"],
        "connection_label": mapping["connection_label"],
        "access_method": mapping["access_method"],
        "access_identifier": mapping.get("access_identifier") or "",
        "secret_last4": mapping.get("secret_last4"),
        "status": mapping.get("status") or "Not connected",
        "last_sync_at": mapping.get("last_sync_at"),
        "last_sync_status": mapping.get("last_sync_status"),
        "last_sync_error": mapping.get("last_sync_error"),
        "last_sync_attempt_at": mapping.get("last_sync_attempt_at"),
    }


def list_utility_connections(account_number: str | None) -> list[dict[str, object]]:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        rows = conn.execute(
            """
            SELECT id, provider_name, connection_label, access_method, access_identifier,
                   secret_last4, status, last_sync_at, last_sync_status,
                   last_sync_error, last_sync_attempt_at
            FROM utility_connections
            WHERE account_id = ?
            ORDER BY provider_name, connection_label
            """,
            (account["id"],),
        ).fetchall()
    connections: list[dict[str, object]] = []
    for row in rows:
        serialized = serialize_utility_connection_row(row)
        if serialized is not None:
            connections.append(serialized)
    return connections


def list_saved_utility_connections_for_sync(account_number: str | None = None) -> list[dict[str, object]]:
    with get_db_connection() as conn:
        params: list[object] = []
        where_clause = ""
        if account_number:
            where_clause = "WHERE accounts.account_number = ?"
            params.append(normalize_account_number(account_number))
        rows = conn.execute(
            f"""
            SELECT utility_connections.id, accounts.account_number,
                   utility_connections.provider_name, utility_connections.connection_label,
                   utility_connections.access_method, utility_connections.access_identifier,
                   utility_connections.secret_last4, utility_connections.status,
                   utility_connections.last_sync_at, utility_connections.last_sync_status,
                   utility_connections.last_sync_error, utility_connections.last_sync_attempt_at
            FROM utility_connections
            JOIN accounts ON accounts.id = utility_connections.account_id
            {where_clause}
            ORDER BY accounts.account_number, utility_connections.provider_name, utility_connections.connection_label
            """,
            params,
        ).fetchall()
    connections: list[dict[str, object]] = []
    for row in rows:
        serialized = serialize_utility_connection_row(row)
        if serialized is None:
            continue
        serialized["account_number"] = dict(row)["account_number"]
        connections.append(serialized)
    return connections


def save_utility_connection(account_number: str | None, form_like) -> dict[str, object]:
    provider_name = clean_optional_text(form_like.get("provider_name")) or "Duke Energy"
    connection_label = clean_optional_text(form_like.get("connection_label")) or provider_name
    access_method = clean_optional_text(form_like.get("access_method")) or "customer_api_key"
    access_identifier = clean_optional_text(form_like.get("access_identifier"))
    access_secret = form_like.get("access_secret")
    secret_hash = build_secret_hash(access_secret)
    secret_token = seal_secret_value(access_secret)
    secret_last4 = build_secret_last4(access_secret)
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        existing = conn.execute(
            """
            SELECT id, secret_hash, secret_token, secret_last4
            FROM utility_connections
            WHERE account_id = ? AND provider_name = ? AND connection_label = ?
            """,
            (account["id"], provider_name, connection_label),
        ).fetchone()
        connection_id = None if existing is None else int(existing["id"])
        if existing is not None and secret_hash is None:
            secret_hash = existing["secret_hash"]
            secret_token = existing["secret_token"]
            secret_last4 = existing["secret_last4"]
        if connection_id is None:
            conn.execute(
                """
                INSERT INTO utility_connections (
                    account_id, provider_name, connection_label, access_method, access_identifier,
                    secret_hash, secret_token, secret_last4, status, last_sync_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    account["id"],
                    provider_name,
                    connection_label,
                    access_method,
                    access_identifier,
                    secret_hash,
                    secret_token,
                    secret_last4,
                    "Ready to sync" if secret_hash else "Needs access",
                    timestamp,
                    timestamp,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE utility_connections
                SET access_method = ?, access_identifier = ?, secret_hash = ?, secret_token = ?, secret_last4 = ?,
                    status = ?, updated_at = ?
                WHERE id = ? AND account_id = ?
                """,
                (
                    access_method,
                    access_identifier,
                    secret_hash,
                    secret_token,
                    secret_last4,
                    "Ready to sync" if secret_hash else "Needs access",
                    timestamp,
                    connection_id,
                    account["id"],
                ),
            )
        conn.commit()
    return list_utility_connections(account_number)[0]


def load_utility_connection_for_sync(account_number: str | None, connection_id: int) -> dict[str, object]:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        row = conn.execute(
            """
            SELECT id, provider_name, connection_label, access_method, access_identifier,
                   secret_token, secret_last4, status, last_sync_at
            FROM utility_connections
            WHERE account_id = ? AND id = ?
            """,
            (account["id"], int(connection_id)),
        ).fetchone()
    if row is None:
        raise ValueError("That utility connection could not be found.")
    mapping = dict(row)
    access_secret = unseal_secret_value(mapping.get("secret_token"))
    if not access_secret:
        raise ValueError("Save the customer-approved access key before syncing.")
    return {
        "id": int(mapping["id"]),
        "account_number": account["account_number"],
        "provider_name": mapping["provider_name"],
        "connection_label": mapping["connection_label"],
        "access_method": mapping["access_method"],
        "access_identifier": mapping.get("access_identifier") or "",
        "access_secret": access_secret,
        "secret_last4": mapping.get("secret_last4"),
        "status": mapping.get("status") or "Not connected",
        "last_sync_at": mapping.get("last_sync_at"),
    }


def fetch_utility_connection_export(connection: dict[str, object]) -> dict[str, object]:
    access_identifier = str(connection.get("access_identifier") or "").strip()
    access_secret = str(connection.get("access_secret") or "").strip()
    if not access_identifier.lower().startswith(("http://", "https://")):
        raise ValueError("Add the utility export URL before syncing this connection.")
    request_headers = {
        "Accept": "application/xml,text/xml,text/csv,*/*",
        "Authorization": f"Bearer {access_secret}",
        "X-API-Key": access_secret,
    }
    request_obj = Request(access_identifier, headers=request_headers)
    with urlopen(request_obj, timeout=30) as response:
        content = response.read()
    filename = Path(unquote(urlsplit(access_identifier).path)).name or "utility-history.xml"
    return {"filename": filename, "content": content}


def clean_sync_error(error: Exception | str, max_length: int = 240) -> str:
    message = str(error).strip()
    if not message and isinstance(error, Exception):
        message = error.__class__.__name__
    if len(message) <= max_length:
        return message
    return f"{message[: max_length - 1].rstrip()}..."


def record_utility_connection_sync_success(account_number: str | None, connection_id: int) -> str:
    sync_time = timestamp_now()
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        conn.execute(
            """
            UPDATE utility_connections
            SET status = ?, last_sync_at = ?, last_sync_status = ?, last_sync_error = ?,
                last_sync_attempt_at = ?, updated_at = ?
            WHERE account_id = ? AND id = ?
            """,
            ("Synced", sync_time, "success", None, sync_time, sync_time, account["id"], int(connection_id)),
        )
        conn.commit()
    return sync_time


def record_utility_connection_sync_failure(account_number: str | None, connection_id: int, error: Exception | str) -> str:
    sync_time = timestamp_now()
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        conn.execute(
            """
            UPDATE utility_connections
            SET status = ?, last_sync_status = ?, last_sync_error = ?,
                last_sync_attempt_at = ?, updated_at = ?
            WHERE account_id = ? AND id = ?
            """,
            ("Sync failed", "failed", clean_sync_error(error), sync_time, sync_time, account["id"], int(connection_id)),
        )
        conn.commit()
    return sync_time


def sync_utility_connection(account_number: str | None, connection_id: int) -> dict[str, object]:
    ensure_data_dirs()
    if not account_has_active_data_authorization(account_number):
        raise ValueError("Customer authorization is required before utility data can be synced.")
    connection = load_utility_connection_for_sync(account_number, connection_id)
    exported = fetch_utility_connection_export(connection)
    filename = secure_filename(str(exported.get("filename") or "utility-history.xml")) or "utility-history.xml"
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        filename = f"{Path(filename).stem or 'utility-history'}.xml"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    destination = INPUT_DIR / f"utility-sync-{connection_id}-{timestamp}-{filename}"
    content = exported.get("content") or b""
    if isinstance(content, str):
        content = content.encode("utf-8")
    destination.write_bytes(content)
    imported = import_interval_file_to_db(
        destination,
        account_number=connection["account_number"],
    )
    sync_time = record_utility_connection_sync_success(connection["account_number"], connection_id)
    return {**imported, "status": "Synced", "last_sync_at": sync_time}


def run_scheduled_utility_sync(account_number: str | None = None) -> dict[str, object]:
    ensure_database()
    connections = list_saved_utility_connections_for_sync(account_number=account_number)
    results: list[dict[str, object]] = []
    for connection in connections:
        connection_id = int(connection["id"])
        connection_account = str(connection["account_number"])
        result: dict[str, object] = {
            "id": connection_id,
            "account_number": connection_account,
            "provider_name": connection["provider_name"],
            "connection_label": connection["connection_label"],
        }
        try:
            synced = sync_utility_connection(connection_account, connection_id)
        except Exception as exc:
            attempt_at = record_utility_connection_sync_failure(connection_account, connection_id, exc)
            results.append(
                {
                    **result,
                    "success": False,
                    "error": clean_sync_error(exc),
                    "last_sync_attempt_at": attempt_at,
                }
            )
            continue
        results.append(
            {
                **result,
                "success": True,
                "interval_count": synced.get("interval_count", 0),
                "last_sync_at": synced.get("last_sync_at"),
                "last_sync_attempt_at": synced.get("last_sync_at"),
            }
        )
    succeeded = sum(1 for result in results if result["success"])
    failed = len(results) - succeeded
    return {
        "account_number": normalize_account_number(account_number) if account_number else None,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "connections": results,
    }


def delete_utility_connection(account_number: str | None, connection_id: int) -> None:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        conn.execute(
            "DELETE FROM utility_connections WHERE account_id = ? AND id = ?",
            (account["id"], int(connection_id)),
        )
        conn.commit()


def clean_optional_text(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def form_checkbox_checked(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def clean_optional_int(value: str | int | None, field_label: str) -> int | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise ValueError(f"Choose a valid {field_label}.") from exc


def clean_household_profile_values(
    form_like,
    *,
    require_address: bool = False,
    require_zip: bool = False,
) -> dict[str, object]:
    address = clean_optional_text(form_like.get("address"))
    if require_address and not address:
        raise ValueError("Enter the electric service address.")
    zip_was_provided = "zip_code" in form_like
    zip_code = clean_zip_code(form_like.get("zip_code"), required=require_zip) if zip_was_provided else ""
    if require_zip and not zip_was_provided:
        raise ValueError("Enter the ZIP code for the service address.")
    occupant_count = clean_optional_int(form_like.get("occupant_count"), "occupancy")
    year_built = clean_optional_int(form_like.get("year_built"), "year built")
    square_footage = clean_optional_int(form_like.get("square_footage"), "square footage")
    if occupant_count is not None and occupant_count < 0:
        raise ValueError("Occupancy cannot be negative.")
    if year_built is not None and not 1700 <= year_built <= datetime.now().year + 1:
        raise ValueError("Choose a valid year built.")
    if square_footage is not None and square_footage < 0:
        raise ValueError("Square footage cannot be negative.")
    return {
        "address": address,
        "zip_was_provided": zip_was_provided,
        "zip_code": zip_code,
        "occupant_count": occupant_count,
        "year_built": year_built,
        "square_footage": square_footage,
        "heating_system": clean_optional_text(form_like.get("heating_system")),
        "cooling_system": clean_optional_text(form_like.get("cooling_system")),
        "water_heater": clean_optional_text(form_like.get("water_heater")),
        "notes": clean_optional_text(form_like.get("notes")),
    }


def serialize_household_profile_row(row: sqlite3.Row | None) -> dict[str, object]:
    if row is None:
        return {
            "address": "",
            "zip_code": "",
            "occupant_count": None,
            "year_built": None,
            "square_footage": None,
            "heating_system": "",
            "cooling_system": "",
            "water_heater": "",
            "notes": "",
            "latitude": None,
            "longitude": None,
            "weather_location": "",
        }

    mapping = dict(row)
    return {
        "address": mapping.get("address") or "",
        "zip_code": mapping.get("zip_code") or "",
        "occupant_count": mapping.get("occupant_count"),
        "year_built": mapping.get("year_built"),
        "square_footage": mapping.get("square_footage"),
        "heating_system": mapping.get("heating_system") or "",
        "cooling_system": mapping.get("cooling_system") or "",
        "water_heater": mapping.get("water_heater") or "",
        "notes": mapping.get("notes") or "",
        "latitude": mapping.get("latitude"),
        "longitude": mapping.get("longitude"),
        "weather_location": mapping.get("weather_location") or "",
    }


def load_household_profile(account_number: str | None = None) -> dict[str, object]:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        row = conn.execute(
            """
            SELECT
                address,
                zip_code,
                occupant_count,
                year_built,
                square_footage,
                heating_system,
                cooling_system,
                water_heater,
                notes,
                latitude,
                longitude,
                weather_location
            FROM household_profiles
            WHERE account_id = ?
            """,
            (account["id"],),
        ).fetchone()
    return serialize_household_profile_row(row)


def save_household_profile(account_number: str | None, form_like) -> dict[str, object]:
    values = clean_household_profile_values(form_like)
    address = values["address"]
    zip_was_provided = bool(values["zip_was_provided"])
    zip_code = str(values["zip_code"])

    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        existing = conn.execute(
            """
            SELECT address, zip_code, latitude, longitude, weather_location
            FROM household_profiles
            WHERE account_id = ?
            """,
            (account["id"],),
        ).fetchone()
        existing_address = None if existing is None else (existing["address"] or None)
        if not zip_was_provided and existing is not None:
            zip_code = existing["zip_code"] or ""
        latitude = None if existing is None else existing["latitude"]
        longitude = None if existing is None else existing["longitude"]
        weather_location = None if existing is None else existing["weather_location"]
        if existing_address != address:
            latitude = None
            longitude = None
            weather_location = None
        conn.execute(
            """
            INSERT INTO household_profiles (
                account_id, address, zip_code, occupant_count, year_built, square_footage,
                heating_system, cooling_system, water_heater, notes, latitude, longitude, weather_location, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                address = excluded.address,
                zip_code = excluded.zip_code,
                occupant_count = excluded.occupant_count,
                year_built = excluded.year_built,
                square_footage = excluded.square_footage,
                heating_system = excluded.heating_system,
                cooling_system = excluded.cooling_system,
                water_heater = excluded.water_heater,
                notes = excluded.notes,
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                weather_location = excluded.weather_location,
                updated_at = excluded.updated_at
            """,
            (
                account["id"],
                address,
                zip_code,
                values["occupant_count"],
                values["year_built"],
                values["square_footage"],
                values["heating_system"],
                values["cooling_system"],
                values["water_heater"],
                values["notes"],
                latitude,
                longitude,
                weather_location,
                timestamp_now(),
            ),
        )
        conn.commit()

    return load_household_profile(account_number)


def create_customer_signup(
    *,
    email: str,
    full_name: str,
    password: str,
    account_number: str | None,
    energy_company: str,
    plan_id: str | None,
    household_form,
    accept_policies: bool,
    confirm_account_authority: bool,
    evidence_remote_hash: str | None = None,
    evidence_user_agent_hash: str | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    normalized_email = clean_email(email)
    normalized_name = (full_name or "").strip() or normalized_email
    normalized_password = clean_password(password)
    if not accept_policies:
        raise ValueError("Agree to the Terms and Privacy Notice to create an account.")
    if not confirm_account_authority:
        raise ValueError("Confirm that you are allowed to manage this electric account.")
    normalized_account_number = (account_number or "").strip()
    if not normalized_account_number:
        raise ValueError("Enter the electric account number.")
    if len(normalized_account_number) > 100:
        raise ValueError("The electric account number is too long.")
    normalized_energy_company = clean_energy_company(energy_company)
    if not normalized_energy_company:
        raise ValueError("Enter the service address so we can identify the energy company.")
    plan = get_billing_plan(plan_id)
    profile = clean_household_profile_values(
        household_form,
        require_address=True,
        require_zip=True,
    )
    timestamp = timestamp_now()
    email_verified_at = None if email_verification_required() else timestamp

    with get_db_connection() as conn:
        existing_user = conn.execute(
            "SELECT id FROM customer_users WHERE email = ?",
            (normalized_email,),
        ).fetchone()
        if existing_user is not None:
            raise ValueError("An account already uses that email address.")
        existing_account = conn.execute(
            "SELECT id FROM accounts WHERE account_number = ?",
            (normalized_account_number,),
        ).fetchone()
        if existing_account is not None:
            raise ValueError("That electric account is already registered. Sign in or contact support for access.")

        conn.execute(
            """
            INSERT INTO customer_users (
                email, full_name, password_hash, is_active, email_verified_at, auth_version,
                created_at, updated_at, last_login_at
            )
            VALUES (?, ?, ?, 1, ?, 1, ?, ?, NULL)
            """,
            (
                normalized_email,
                normalized_name,
                generate_password_hash(normalized_password),
                email_verified_at,
                timestamp,
                timestamp,
            ),
        )
        customer_row = conn.execute(
            """
            SELECT id, email, full_name, is_active, email_verified_at, auth_version, last_login_at
            FROM customer_users
            WHERE email = ?
            """,
            (normalized_email,),
        ).fetchone()
        customer_user = serialize_customer_user_row(customer_row) or {}
        account = get_or_create_account(
            conn,
            normalized_account_number,
            display_name=normalized_name,
            energy_company=normalized_energy_company,
        )
        conn.execute(
            """
            INSERT INTO household_profiles (
                account_id, address, zip_code, occupant_count, year_built, square_footage,
                heating_system, cooling_system, water_heater, notes,
                latitude, longitude, weather_location, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
            """,
            (
                account["id"],
                profile["address"],
                profile["zip_code"],
                profile["occupant_count"],
                profile["year_built"],
                profile["square_footage"],
                profile["heating_system"],
                profile["cooling_system"],
                profile["water_heater"],
                profile["notes"],
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO account_access_emails (
                account_id, email, full_name, access_level, created_at, updated_at
            )
            VALUES (?, ?, ?, 'Manager', ?, ?)
            """,
            (account["id"], normalized_email, normalized_name, timestamp, timestamp),
        )
        record_customer_policy_acceptance(
            conn,
            int(customer_user["id"]),
            accepted_at=timestamp,
            remote_hash=evidence_remote_hash,
            user_agent_hash=evidence_user_agent_hash,
        )
        create_account_data_authorization(
            conn,
            account_id=int(account["id"]),
            customer_user_id=int(customer_user["id"]),
            granted_at=timestamp,
            remote_hash=evidence_remote_hash,
            user_agent_hash=evidence_user_agent_hash,
        )
        conn.execute(
            """
            INSERT INTO customer_billing (
                customer_user_id, plan_id, subscription_status, created_at, updated_at
            )
            VALUES (?, ?, 'not_started', ?, ?)
            """,
            (customer_user["id"], plan["id"], timestamp, timestamp),
        )
        billing_row = conn.execute(
            "SELECT * FROM customer_billing WHERE customer_user_id = ?",
            (customer_user["id"],),
        ).fetchone()

    return customer_user, account, serialize_customer_billing_row(billing_row, int(customer_user["id"]))


def fetch_json(url: str) -> dict[str, object]:
    with urlopen(url, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def geocode_electric_service_location(zip_code: str, address: str | None = None) -> dict[str, object] | None:
    normalized_zip = clean_zip_code(zip_code, required=True)
    address_text = clean_optional_text(address)
    search_text = ", ".join(part for part in (address_text, normalized_zip, "North Carolina", "USA") if part)
    params = urlencode(
        {
            "SingleLine": search_text,
            "f": "json",
            "outFields": "Match_addr,Addr_type,Postal,Region,Country",
            "maxLocations": 5,
            "countryCode": "USA",
        }
    )
    payload = fetch_json(f"{ARCGIS_GEOCODER_URL}?{params}")
    for candidate in payload.get("candidates") or []:
        if float(candidate.get("score") or 0) < 80:
            continue
        attributes = candidate.get("attributes") or {}
        region = str(attributes.get("Region") or "").strip().lower()
        match_address = str(candidate.get("address") or attributes.get("Match_addr") or "")
        if region not in {"nc", "north carolina"} and "north carolina" not in match_address.lower():
            continue
        postal = str(attributes.get("Postal") or "").strip()
        if postal and not postal.startswith(normalized_zip):
            continue
        location = candidate.get("location") or {}
        if location.get("x") is None or location.get("y") is None:
            continue
        return {
            "longitude": float(location["x"]),
            "latitude": float(location["y"]),
            "match_address": match_address,
        }
    return None


def lookup_energy_company_by_zip(zip_code: str, address: str | None = None) -> dict[str, object]:
    normalized_zip = clean_zip_code(zip_code, required=True)
    location = geocode_electric_service_location(normalized_zip, address)
    if location is None:
        raise ValueError("We could not locate that North Carolina service address. Check the address and ZIP code.")

    matches: dict[tuple[str, str], dict[str, object]] = {}
    for layer in NC_ELECTRIC_SERVICE_LAYERS:
        params = urlencode(
            {
                "geometry": f"{location['longitude']},{location['latitude']}",
                "geometryType": "esriGeometryPoint",
                "inSR": 4326,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "returnGeometry": "false",
                "f": "json",
            }
        )
        payload = fetch_json(f"{layer['url']}?{params}")
        if payload.get("error"):
            raise ValueError("The electric company lookup is unavailable right now. Please try again.")
        for feature in payload.get("features") or []:
            attributes = feature.get("attributes") or {}
            raw_name = str(attributes.get(layer["owner_field"]) or "").strip()
            utility_id = attributes.get(layer["utility_id_field"])
            if not raw_name:
                continue
            company_name = canonical_energy_company_name(raw_name, utility_id)
            normalized_id = ""
            if utility_id is not None:
                try:
                    normalized_id = str(int(float(str(utility_id))))
                except (TypeError, ValueError):
                    normalized_id = str(utility_id).strip()
            matches[(normalized_id, company_name.lower())] = {
                "energy_company": company_name,
                "eia_utility_id": normalized_id,
            }

    if not matches:
        raise ValueError("We could not identify the electric company for that service address.")
    if len(matches) > 1:
        if not clean_optional_text(address):
            raise ValueError("That ZIP code crosses electric service areas. Add the street address so we can identify the right company.")
        raise ValueError("That address is near an electric service boundary. Check the service address and ZIP code.")

    result = next(iter(matches.values()))
    return {
        **result,
        "zip_code": normalized_zip,
        "match_address": location["match_address"],
        "match_basis": "service address" if clean_optional_text(address) else "ZIP code",
    }


def resolve_energy_company_for_form(
    form_like,
    *,
    existing_account: dict[str, object] | None = None,
    existing_profile: dict[str, object] | None = None,
    require_zip: bool = False,
) -> str:
    zip_code = clean_zip_code(form_like.get("zip_code"), required=require_zip)
    if not zip_code:
        if existing_account and existing_account.get("energy_company"):
            return str(existing_account["energy_company"])
        return clean_energy_company(form_like.get("energy_company"))

    address = clean_optional_text(form_like.get("address"))
    if (
        existing_account
        and existing_account.get("energy_company")
        and existing_profile
        and zip_code == existing_profile.get("zip_code")
        and (address or "") == (existing_profile.get("address") or "")
    ):
        return str(existing_account["energy_company"])
    return str(lookup_energy_company_by_zip(zip_code, address).get("energy_company") or "")


def geocode_address(address: str) -> dict[str, object] | None:
    if not address.strip():
        return None
    params = urlencode(
        {
            "name": address,
            "count": 1,
            "language": "en",
            "format": "json",
        }
    )
    payload = fetch_json(f"{OPEN_METEO_GEOCODE_URL}?{params}")
    results = payload.get("results") or []
    if not results:
        return None

    top = results[0]
    name_parts: list[str] = []
    for key in ("name", "admin1", "country"):
        value = top.get(key)
        if value and value not in name_parts:
            name_parts.append(str(value))
    return {
        "latitude": float(top["latitude"]),
        "longitude": float(top["longitude"]),
        "weather_location": ", ".join(name_parts),
    }


def save_household_weather_location(
    account_number: str | None,
    latitude: float,
    longitude: float,
    weather_location: str | None,
) -> dict[str, object]:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        conn.execute(
            """
            UPDATE household_profiles
            SET latitude = ?, longitude = ?, weather_location = ?, updated_at = ?
            WHERE account_id = ?
            """,
            (
                float(latitude),
                float(longitude),
                weather_location,
                timestamp_now(),
                account["id"],
            ),
        )
        conn.commit()
    return load_household_profile(account_number)


def resolve_household_weather_location(account_number: str | None) -> dict[str, object] | None:
    profile = load_household_profile(account_number)
    if profile["latitude"] is not None and profile["longitude"] is not None:
        return profile
    if not profile["address"]:
        return None

    resolved = geocode_address(profile["address"])
    if resolved is None:
        return None
    return save_household_weather_location(
        account_number,
        latitude=float(resolved["latitude"]),
        longitude=float(resolved["longitude"]),
        weather_location=str(resolved["weather_location"]),
    )


def describe_weather_code(code: int | None) -> str:
    mapping = {
        0: "Clear",
        1: "Mostly clear",
        2: "Partly cloudy",
        3: "Cloudy",
        45: "Fog",
        48: "Rime fog",
        51: "Light drizzle",
        53: "Drizzle",
        55: "Heavy drizzle",
        56: "Light freezing drizzle",
        57: "Freezing drizzle",
        61: "Light rain",
        63: "Rain",
        65: "Heavy rain",
        66: "Light freezing rain",
        67: "Freezing rain",
        71: "Light snow",
        73: "Snow",
        75: "Heavy snow",
        77: "Snow grains",
        80: "Rain showers",
        81: "Heavy rain showers",
        82: "Violent rain showers",
        85: "Snow showers",
        86: "Heavy snow showers",
        95: "Thunderstorm",
        96: "Thunderstorm with hail",
        99: "Severe thunderstorm with hail",
    }
    return mapping.get(code or 0, "Weather")


def build_weather_payload(hourly: dict[str, list[object]], location_name: str, weather_date: str) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    times = hourly.get("time", [])
    for index, time_value in enumerate(times):
        temp = hourly.get("temperature_2m", [None] * len(times))[index]
        apparent = hourly.get("apparent_temperature", [None] * len(times))[index]
        precipitation = hourly.get("precipitation", [None] * len(times))[index]
        wind_speed = hourly.get("wind_speed_10m", [None] * len(times))[index]
        cloud_cover = hourly.get("cloud_cover", [None] * len(times))[index]
        weather_code = hourly.get("weather_code", [None] * len(times))[index]
        hour_label = time_value.split("T", 1)[-1] if isinstance(time_value, str) and "T" in time_value else str(time_value)
        rows.append(
            {
                "time": time_value,
                "hour": hour_label,
                "temperature_f": round_value(temp, 1),
                "apparent_temperature_f": round_value(apparent, 1),
                "precipitation_in": round_value(precipitation, 2),
                "wind_mph": round_value(wind_speed, 1),
                "cloud_cover_pct": round_value(cloud_cover, 0),
                "weather_code": None if weather_code is None else int(weather_code),
                "weather_label": describe_weather_code(None if weather_code is None else int(weather_code)),
            }
        )

    if not rows:
        return {"available": False, "reason": "Weather data is not available for that day."}

    temperatures = [row["temperature_f"] for row in rows if row["temperature_f"] is not None]
    apparent = [row["apparent_temperature_f"] for row in rows if row["apparent_temperature_f"] is not None]
    precipitation = [row["precipitation_in"] or 0.0 for row in rows if row["precipitation_in"] is not None]
    wind = [row["wind_mph"] for row in rows if row["wind_mph"] is not None]

    weather_counts: dict[str, int] = {}
    for row in rows:
        label = row["weather_label"]
        weather_counts[label] = weather_counts.get(label, 0) + 1
    top_weather = max(weather_counts, key=weather_counts.get) if weather_counts else "Weather"

    return {
        "available": True,
        "date": weather_date,
        "location_name": location_name,
        "summary": {
            "high_temp_f": round(max(temperatures), 1) if temperatures else None,
            "low_temp_f": round(min(temperatures), 1) if temperatures else None,
            "high_apparent_f": round(max(apparent), 1) if apparent else None,
            "precipitation_in": round(sum(precipitation), 2),
            "max_wind_mph": round(max(wind), 1) if wind else None,
            "conditions": top_weather,
        },
        "hourly": rows,
    }


def join_natural(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return f"{', '.join(items[:-1])}, and {items[-1]}"


def weather_value(summary: dict[str, object], key: str) -> float | None:
    value = summary.get(key)
    if value is None or pd.isna(value):
        return None
    return float(value)


def weather_metric_text(label: str, value: float | None, suffix: str) -> str | None:
    if value is None:
        return None
    return f"{label} {value:.1f}{suffix}"


def condition_suggests_storm(conditions: str) -> bool:
    normalized = conditions.lower()
    storm_terms = (
        "thunderstorm",
        "storm",
        "hail",
        "heavy rain",
        "rain shower",
        "freezing rain",
        "heavy snow",
        "snow shower",
    )
    return any(term in normalized for term in storm_terms)


def build_weather_context(weather: dict[str, object] | None) -> dict[str, object]:
    if not weather or not weather.get("available"):
        return {
            "available": False,
            "effect": "weather_unavailable",
            "signals": [],
            "summary": str((weather or {}).get("reason") or "Weather was not available for that day."),
            "location_name": (weather or {}).get("location_name"),
            "conditions": None,
            "high_temp_f": None,
            "low_temp_f": None,
            "high_apparent_f": None,
            "precipitation_in": None,
            "max_wind_mph": None,
        }

    summary = weather.get("summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    conditions = str(summary.get("conditions") or "Weather")
    high_temp = round_value(weather_value(summary, "high_temp_f"), 1)
    low_temp = round_value(weather_value(summary, "low_temp_f"), 1)
    high_apparent = round_value(weather_value(summary, "high_apparent_f"), 1)
    precipitation = round_value(weather_value(summary, "precipitation_in"), 2)
    max_wind = round_value(weather_value(summary, "max_wind_mph"), 1)

    signals: list[str] = []
    phrases: list[str] = []
    if (
        (high_temp is not None and high_temp >= WEATHER_HEAT_HIGH_F)
        or (high_apparent is not None and high_apparent >= WEATHER_HEAT_APPARENT_F)
        or (low_temp is not None and low_temp >= WEATHER_HEAT_LOW_F)
    ):
        signals.append("unusual_heat")
        phrases.append("hot weather")
    if (
        (low_temp is not None and low_temp <= WEATHER_COLD_LOW_F)
        or (high_temp is not None and high_temp <= WEATHER_COLD_HIGH_F)
    ):
        signals.append("unusual_cold")
        phrases.append("cold weather")
    if (
        (precipitation is not None and precipitation >= WEATHER_STORM_PRECIP_IN)
        or (max_wind is not None and max_wind >= WEATHER_STORM_WIND_MPH)
        or condition_suggests_storm(conditions)
    ):
        signals.append("storm_conditions")
        phrases.append("storm conditions")

    metric_bits = [
        weather_metric_text("high", high_temp, " F"),
        weather_metric_text("feels like", high_apparent, " F"),
        weather_metric_text("low", low_temp, " F"),
        weather_metric_text("rain", precipitation, " in"),
        weather_metric_text("wind", max_wind, " mph"),
    ]
    metrics_text = "; ".join(bit for bit in metric_bits if bit)
    if signals:
        summary_text = f"{join_natural(phrases).capitalize()} could explain part of the overnight spike."
        if metrics_text:
            summary_text = f"{summary_text} {metrics_text}."
        effect = "plausible_explanation"
    else:
        summary_text = (
            "No unusual heat, cold, or storm signal appears in the available weather; "
            "that makes the spike stand out more."
        )
        if metrics_text:
            summary_text = f"{summary_text} {conditions}: {metrics_text}."
        effect = "makes_spike_stand_out"

    return {
        "available": True,
        "effect": effect,
        "signals": signals,
        "summary": summary_text,
        "location_name": weather.get("location_name"),
        "conditions": conditions,
        "high_temp_f": high_temp,
        "low_temp_f": low_temp,
        "high_apparent_f": high_apparent,
        "precipitation_in": precipitation,
        "max_wind_mph": max_wind,
    }


def fetch_historical_weather(latitude: float, longitude: float, weather_date: str, tz_name: str) -> dict[str, object]:
    params = urlencode(
        {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": weather_date,
            "end_date": weather_date,
            "hourly": ",".join(
                [
                    "temperature_2m",
                    "apparent_temperature",
                    "precipitation",
                    "weather_code",
                    "cloud_cover",
                    "wind_speed_10m",
                ]
            ),
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": tz_name,
        }
    )
    payload = fetch_json(f"{OPEN_METEO_ARCHIVE_URL}?{params}")
    return payload


def load_day_weather(account_number: str | None, weather_date: str | None, tz_name: str) -> dict[str, object]:
    normalized_date = normalize_optional_date(weather_date)
    if normalized_date is None:
        return {"available": False, "reason": "Choose a day to load the weather."}

    try:
        location = resolve_household_weather_location(account_number)
    except Exception:
        return {"available": False, "reason": "Weather could not be looked up right now."}
    if location is None:
        return {"available": False, "reason": "Add the service address to pull weather for that day."}

    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        cached = conn.execute(
            """
            SELECT data_json, latitude, longitude, timezone
            FROM weather_daily_cache
            WHERE account_id = ? AND weather_date = ?
            """,
            (account["id"], normalized_date),
        ).fetchone()
        if (
            cached is not None
            and round(float(cached["latitude"]), 4) == round(float(location["latitude"]), 4)
            and round(float(cached["longitude"]), 4) == round(float(location["longitude"]), 4)
            and cached["timezone"] == tz_name
        ):
            return json.loads(cached["data_json"])

    try:
        payload = fetch_historical_weather(
            latitude=float(location["latitude"]),
            longitude=float(location["longitude"]),
            weather_date=normalized_date,
            tz_name=tz_name,
        )
    except Exception:
        return {"available": False, "reason": "Weather could not be loaded for that day."}
    weather = build_weather_payload(
        payload.get("hourly", {}),
        location.get("weather_location") or location.get("address") or "Weather",
        normalized_date,
    )
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        conn.execute(
            """
            INSERT INTO weather_daily_cache (
                account_id, weather_date, latitude, longitude, timezone, location_name, data_json, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, weather_date) DO UPDATE SET
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                timezone = excluded.timezone,
                location_name = excluded.location_name,
                data_json = excluded.data_json,
                fetched_at = excluded.fetched_at
            """,
            (
                account["id"],
                normalized_date,
                float(location["latitude"]),
                float(location["longitude"]),
                tz_name,
                location.get("weather_location") or location.get("address"),
                json.dumps(weather),
                timestamp_now(),
            ),
        )
        conn.commit()
    return weather


def date_key(value: object) -> str:
    if isinstance(value, ddate):
        return value.isoformat()
    return str(value)


def load_weather_contexts_for_suspicious_days(
    summary: pd.DataFrame,
    account_number: str | None,
    tz_name: str,
) -> dict[str, dict[str, object]]:
    contexts: dict[str, dict[str, object]] = {}
    if summary.empty:
        return contexts

    for reading_date, row in summary.iterrows():
        if not bool(row.get("suspicious", False)):
            continue
        weather_date = date_key(reading_date)
        try:
            weather = load_day_weather(account_number, weather_date, tz_name)
        except Exception:
            weather = {"available": False, "reason": "Weather could not be loaded for that day."}
        contexts[weather_date] = build_weather_context(weather)
    return contexts


def attach_weather_context_to_summary(
    summary: pd.DataFrame,
    weather_contexts: dict[str, dict[str, object]],
) -> pd.DataFrame:
    enriched = summary.copy()
    default_columns: dict[str, object] = {
        "weather_context": "",
        "weather_effect": "",
        "weather_signals": "",
        "weather_location": "",
        "weather_conditions": "",
        "weather_high_temp_f": pd.NA,
        "weather_low_temp_f": pd.NA,
        "weather_high_apparent_f": pd.NA,
        "weather_precipitation_in": pd.NA,
        "weather_max_wind_mph": pd.NA,
    }
    for column, default_value in default_columns.items():
        if column not in enriched.columns:
            enriched[column] = default_value

    for reading_date, context in weather_contexts.items():
        try:
            index_value: object = ddate.fromisoformat(reading_date)
        except ValueError:
            index_value = reading_date
        if index_value not in enriched.index:
            continue
        enriched.loc[index_value, "weather_context"] = context.get("summary") or ""
        enriched.loc[index_value, "weather_effect"] = context.get("effect") or ""
        enriched.loc[index_value, "weather_signals"] = ",".join(str(signal) for signal in context.get("signals", []))
        enriched.loc[index_value, "weather_location"] = context.get("location_name") or ""
        enriched.loc[index_value, "weather_conditions"] = context.get("conditions") or ""
        enriched.loc[index_value, "weather_high_temp_f"] = context.get("high_temp_f")
        enriched.loc[index_value, "weather_low_temp_f"] = context.get("low_temp_f")
        enriched.loc[index_value, "weather_high_apparent_f"] = context.get("high_apparent_f")
        enriched.loc[index_value, "weather_precipitation_in"] = context.get("precipitation_in")
        enriched.loc[index_value, "weather_max_wind_mph"] = context.get("max_wind_mph")
    return enriched


def attach_weather_context_to_rows(
    rows: list[dict[str, object]],
    weather_contexts: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    for row in rows:
        context = weather_contexts.get(str(row.get("date")))
        if context is not None:
            row["weather_context"] = context
    return rows


def serialize_load_item_row(row: sqlite3.Row | None) -> dict[str, object] | None:
    if row is None:
        return None
    mapping = dict(row)
    total_watts = float(mapping["quantity"]) * float(mapping["watts_each"])
    return {
        "id": int(mapping["id"]),
        "label": mapping["label"],
        "quantity": int(mapping["quantity"]),
        "watts_each": round(float(mapping["watts_each"]), 1),
        "total_watts": round(total_watts, 1),
        "include_when_off": bool(mapping["include_when_off"]),
        "notes": mapping["notes"] or "",
    }


def list_load_items(account_number: str | None = None) -> list[dict[str, object]]:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        rows = conn.execute(
            """
            SELECT id, label, quantity, watts_each, include_when_off, notes
            FROM account_load_items
            WHERE account_id = ?
            ORDER BY id ASC
            """,
            (account["id"],),
        ).fetchall()
    items: list[dict[str, object]] = []
    for row in rows:
        serialized = serialize_load_item_row(row)
        if serialized is not None:
            items.append(serialized)
    return items


def build_load_inventory_summary(load_items: list[dict[str, object]]) -> dict[str, object]:
    if not load_items:
        return {
            "all_on_watts": 0.0,
            "all_on_kw": 0.0,
            "off_watts": 0.0,
            "off_kw": 0.0,
            "item_count": 0,
        }
    all_on_watts = sum(float(item["total_watts"]) for item in load_items)
    off_watts = sum(float(item["total_watts"]) for item in load_items if item["include_when_off"])
    return {
        "all_on_watts": round(all_on_watts, 1),
        "all_on_kw": round(all_on_watts / 1000.0, 3),
        "off_watts": round(off_watts, 1),
        "off_kw": round(off_watts / 1000.0, 3),
        "item_count": len(load_items),
    }


def add_load_item(
    account_number: str | None,
    label: str,
    quantity: int,
    watts_each: float,
    include_when_off: bool,
    notes: str | None = None,
) -> dict[str, object]:
    clean_label = (label or "").strip()
    if not clean_label:
        raise ValueError("Give this load a name.")
    if quantity <= 0:
        raise ValueError("Quantity must be at least 1.")
    if watts_each <= 0:
        raise ValueError("Wattage must be above 0.")

    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        timestamp = timestamp_now()
        conn.execute(
            """
            INSERT INTO account_load_items (
                account_id, label, quantity, watts_each, include_when_off, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account["id"],
                clean_label,
                int(quantity),
                float(watts_each),
                1 if include_when_off else 0,
                (notes or "").strip() or None,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            """
            SELECT id, label, quantity, watts_each, include_when_off, notes
            FROM account_load_items
            WHERE account_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (account["id"],),
        ).fetchone()
        conn.commit()
    return serialize_load_item_row(row) or {}


def delete_load_item(account_number: str | None, item_id: int) -> None:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        conn.execute(
            "DELETE FROM account_load_items WHERE account_id = ? AND id = ?",
            (account["id"], int(item_id)),
        )
        conn.commit()


def normalize_interval_value_to_kwh(value: float, unit_of_measure: str | None) -> float:
    if not unit_of_measure:
        return value / 1000.0

    normalized = unit_of_measure.strip().lower().replace("-", "").replace("_", "")
    if normalized in {"kwh", "kilowatthour", "kilowatthours"}:
        return value
    if normalized in {"wh", "watthour", "watthours"}:
        return value / 1000.0
    return value / 1000.0


@dataclass(frozen=True)
class UtilityFeedAdapterInfo:
    adapter_id: str
    display_name: str
    provider_label: str
    standard_label: str
    format_label: str
    file_types: tuple[str, ...]
    customer_label: str
    customer_note: str
    status: str = "supported"


@dataclass(frozen=True)
class ParsedIntervalData:
    frame: pd.DataFrame
    adapter: UtilityFeedAdapterInfo


class UtilityFeedAdapter:
    info: UtilityFeedAdapterInfo

    def detect_score(self, path: Path, source: Any) -> int:
        raise NotImplementedError

    def parse(self, path: Path, source: Any, tz_name: str = DEFAULT_TZ) -> pd.DataFrame:
        local_tz = tz.gettz(tz_name)
        if local_tz is None:
            raise ValueError(f"Unknown timezone: {tz_name}")
        if not isinstance(source, etree._ElementTree):
            raise ValueError("This adapter needs an XML interval source.")
        return build_interval_frame_from_tree(source, local_tz)


class GreenButtonESPIAdapter(UtilityFeedAdapter):
    info = UtilityFeedAdapterInfo(
        adapter_id="green_button_espi",
        display_name="Green Button ESPI XML",
        provider_label="Green Button utility exports",
        standard_label="NAESB REQ.21 ESPI / Green Button",
        format_label="Atom-wrapped ESPI XML",
        file_types=(".xml",),
        customer_label="Green Button history",
        customer_note="Utility account exports that include meter readings over time.",
    )

    def detect_score(self, path: Path, source: Any) -> int:
        if path.suffix.lower() not in self.info.file_types:
            return 0
        if not isinstance(source, etree._ElementTree):
            return 0
        has_espi = bool(source.xpath("//*[namespace-uri()='http://naesb.org/espi']"))
        if not has_espi:
            return 0
        has_atom_root = bool(
            source.xpath(
                "/*[local-name()='feed' or local-name()='entry'][namespace-uri()='http://www.w3.org/2005/Atom']"
            )
        )
        return 120 if has_atom_root else 100


class DukeStyleIntervalXmlAdapter(UtilityFeedAdapter):
    info = UtilityFeedAdapterInfo(
        adapter_id="duke_style_interval_xml",
        display_name="Duke-style interval XML",
        provider_label="Duke-style and other basic interval XML exports",
        standard_label="Utility-specific interval XML",
        format_label="IntervalBlock / IntervalReading XML",
        file_types=(".xml",),
        customer_label="Duke Energy history",
        customer_note="Duke account history files with interval readings.",
    )

    def detect_score(self, path: Path, source: Any) -> int:
        if path.suffix.lower() not in self.info.file_types:
            return 0
        if not isinstance(source, etree._ElementTree):
            return 0
        if bool(source.xpath("//*[namespace-uri()='http://naesb.org/espi']")):
            return 0
        has_interval_reading = bool(source.xpath("//*[local-name()='IntervalReading']"))
        if not has_interval_reading:
            return 0
        if bool(source.xpath("/*[local-name()='UsagePoint']")):
            return 80
        if bool(source.xpath("//*[local-name()='IntervalBlock']")):
            return 60
        return 40


CSV_START_FIELDS = ("interval_start", "start_time", "start", "interval_start_local")
CSV_END_FIELDS = ("interval_end", "end_time", "end", "interval_end_local")
CSV_USAGE_KWH_FIELDS = ("usage_kwh", "kwh", "energy_kwh")
CSV_USAGE_WH_FIELDS = ("usage_wh", "wh", "energy_wh")
CSV_DURATION_SECOND_FIELDS = ("duration_seconds", "duration_s", "seconds_per_interval", "interval_seconds")
CSV_DURATION_MINUTE_FIELDS = ("duration_minutes", "interval_minutes", "minutes_per_interval")


def normalize_feed_column_name(value: str) -> str:
    cleaned: list[str] = []
    previous_was_separator = False
    for char in value.strip().lower():
        if char.isalnum():
            cleaned.append(char)
            previous_was_separator = False
            continue
        if not previous_was_separator:
            cleaned.append("_")
        previous_was_separator = True
    return "".join(cleaned).strip("_")


def find_supported_csv_columns(headers: list[str]) -> dict[str, str] | None:
    normalized = {normalize_feed_column_name(header): header for header in headers if header}

    def find_column(*names: str) -> str | None:
        for name in names:
            if name in normalized:
                return normalized[name]
        return None

    mapping: dict[str, str] = {}
    start_column = find_column(*CSV_START_FIELDS)
    usage_kwh_column = find_column(*CSV_USAGE_KWH_FIELDS)
    usage_wh_column = find_column(*CSV_USAGE_WH_FIELDS)
    end_column = find_column(*CSV_END_FIELDS)
    duration_second_column = find_column(*CSV_DURATION_SECOND_FIELDS)
    duration_minute_column = find_column(*CSV_DURATION_MINUTE_FIELDS)

    if start_column:
        mapping["start"] = start_column
    if usage_kwh_column:
        mapping["usage_kwh"] = usage_kwh_column
    elif usage_wh_column:
        mapping["usage_wh"] = usage_wh_column
    if end_column:
        mapping["end"] = end_column
    elif duration_second_column:
        mapping["duration_s"] = duration_second_column
    elif duration_minute_column:
        mapping["duration_minutes"] = duration_minute_column

    if "start" not in mapping:
        return None
    if "usage_kwh" not in mapping and "usage_wh" not in mapping:
        return None
    if "end" not in mapping and "duration_s" not in mapping and "duration_minutes" not in mapping:
        return None
    return mapping


class UtilityIntervalCsvAdapter(UtilityFeedAdapter):
    info = UtilityFeedAdapterInfo(
        adapter_id="utility_interval_csv",
        display_name="Utility interval CSV",
        provider_label="Utility interval CSV exports",
        standard_label="Utility-specific interval CSV",
        format_label="Timestamped interval CSV",
        file_types=(".csv",),
        customer_label="Interval spreadsheet",
        customer_note="Rows with start time, usage, and either end time or duration.",
    )

    def detect_score(self, path: Path, source: Any) -> int:
        if path.suffix.lower() not in self.info.file_types:
            return 0
        if not isinstance(source, list):
            return 0
        return 90 if find_supported_csv_columns(source) else 0

    def parse(self, path: Path, source: Any, tz_name: str = DEFAULT_TZ) -> pd.DataFrame:
        local_tz = tz.gettz(tz_name)
        if local_tz is None:
            raise ValueError(f"Unknown timezone: {tz_name}")
        return build_interval_frame_from_csv(path, local_tz)


UTILITY_FEED_ADAPTERS: tuple[UtilityFeedAdapter, ...] = (
    GreenButtonESPIAdapter(),
    DukeStyleIntervalXmlAdapter(),
    UtilityIntervalCsvAdapter(),
)


def list_supported_utility_adapters() -> list[dict[str, object]]:
    return [
        {
            "adapter_id": adapter.info.adapter_id,
            "display_name": adapter.info.display_name,
            "provider_label": adapter.info.provider_label,
            "standard_label": adapter.info.standard_label,
            "format_label": adapter.info.format_label,
            "file_types": list(adapter.info.file_types),
            "customer_label": adapter.info.customer_label,
            "customer_note": adapter.info.customer_note,
            "status": adapter.info.status,
        }
        for adapter in UTILITY_FEED_ADAPTERS
    ]


def parse_interval_xml_tree(path: str | Path) -> etree._ElementTree:
    with open(path, "rb") as handle:
        return etree.parse(handle)


def load_interval_source(path: str | Path) -> Any:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix == ".xml":
        return parse_interval_xml_tree(file_path)
    if suffix == ".csv":
        with open(file_path, newline="", encoding="utf-8-sig") as handle:
            reader = csv.reader(handle)
            return next(reader, [])
    return None


def select_utility_feed_adapter(path: str | Path, source: Any) -> UtilityFeedAdapter:
    file_path = Path(path)
    best_adapter: UtilityFeedAdapter | None = None
    best_score = -1
    for adapter in UTILITY_FEED_ADAPTERS:
        score = int(adapter.detect_score(file_path, source))
        if score > best_score:
            best_adapter = adapter
            best_score = score
    if best_adapter is None or best_score <= 0:
        supported = ", ".join(adapter.info.display_name for adapter in UTILITY_FEED_ADAPTERS)
        raise ValueError(f"This file format is not supported yet. Supported feeds: {supported}.")
    return best_adapter


def detect_utility_feed_adapter(path: str | Path) -> dict[str, object]:
    source = load_interval_source(path)
    adapter = select_utility_feed_adapter(path, source)
    return {
        "adapter_id": adapter.info.adapter_id,
        "display_name": adapter.info.display_name,
        "provider_label": adapter.info.provider_label,
        "standard_label": adapter.info.standard_label,
        "format_label": adapter.info.format_label,
        "status": adapter.info.status,
    }


def build_interval_rows_from_tree(tree: etree._ElementTree, local_tz) -> list[dict[str, object]]:
    intervals: list[dict[str, object]] = []
    interval_blocks = tree.xpath("//*[local-name()='IntervalBlock']")

    for block in interval_blocks:
        metadata = block.xpath("./*[local-name()='interval'][1]")
        default_duration = None
        unit_of_measure = None
        if metadata:
            seconds_per_interval = metadata[0].xpath("./*[local-name()='secondsPerInterval']/text()")
            unit_text = metadata[0].xpath("./*[local-name()='unitOfMeasure']/text()")
            if seconds_per_interval:
                try:
                    default_duration = int(seconds_per_interval[0].strip())
                except (TypeError, ValueError):
                    default_duration = None
            if unit_text:
                unit_of_measure = unit_text[0].strip()

        for interval_reading in block.xpath("./*[local-name()='IntervalReading']"):
            start_elem = interval_reading.xpath("./*[local-name()='timePeriod']/*[local-name()='start']/text()")
            duration_elem = interval_reading.xpath("./*[local-name()='timePeriod']/*[local-name()='duration']/text()")
            value_elem = interval_reading.xpath("./*[local-name()='value']/text()")
            if not (start_elem and value_elem):
                continue

            try:
                start_epoch = int(start_elem[0].strip())
                duration_seconds = int(duration_elem[0].strip()) if duration_elem else default_duration
                raw_value = float(value_elem[0].strip())
            except (AttributeError, TypeError, ValueError):
                continue

            if not duration_seconds:
                continue

            dt_utc = datetime.fromtimestamp(start_epoch, tz.UTC)
            dt_local = dt_utc.astimezone(local_tz)
            interval_kwh = normalize_interval_value_to_kwh(raw_value, unit_of_measure)
            watt_hours = interval_kwh * 1000.0
            kw = interval_kwh / (duration_seconds / 3600.0)

            intervals.append(
                {
                    "start_epoch": start_epoch,
                    "start": dt_local,
                    "duration_s": duration_seconds,
                    "wh": watt_hours,
                    "kw": kw,
                }
            )
    if not intervals:
        # Fallback for simpler XML variants that may not use IntervalBlock metadata.
        for interval_reading in tree.xpath("//*[local-name()='IntervalReading']"):
            start_elem = interval_reading.xpath("./*[local-name()='timePeriod']/*[local-name()='start']/text()")
            duration_elem = interval_reading.xpath("./*[local-name()='timePeriod']/*[local-name()='duration']/text()")
            value_elem = interval_reading.xpath("./*[local-name()='value']/text()")
            if not (start_elem and duration_elem and value_elem):
                continue

            try:
                start_epoch = int(start_elem[0].strip())
                duration_seconds = int(duration_elem[0].strip())
                raw_value = float(value_elem[0].strip())
            except (AttributeError, TypeError, ValueError):
                continue

            dt_utc = datetime.fromtimestamp(start_epoch, tz.UTC)
            dt_local = dt_utc.astimezone(local_tz)
            interval_kwh = normalize_interval_value_to_kwh(raw_value, None)
            watt_hours = interval_kwh * 1000.0
            kw = interval_kwh / (duration_seconds / 3600.0)

            intervals.append(
                {
                    "start_epoch": start_epoch,
                    "start": dt_local,
                    "duration_s": duration_seconds,
                    "wh": watt_hours,
                    "kw": kw,
                }
            )

    if not intervals:
        raise ValueError("No IntervalReading elements were found in this XML file.")

    return intervals


def build_interval_frame(intervals: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(intervals)
    frame = frame.sort_values("start").reset_index(drop=True)
    frame["date"] = frame["start"].dt.date
    frame["time"] = frame["start"].dt.time
    return frame


def build_interval_frame_from_tree(tree: etree._ElementTree, local_tz) -> pd.DataFrame:
    intervals = build_interval_rows_from_tree(tree, local_tz)
    return build_interval_frame(intervals)


def parse_interval_csv_timestamp(value: str, local_tz) -> datetime:
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def build_interval_frame_from_csv(path: str | Path, local_tz) -> pd.DataFrame:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        column_map = find_supported_csv_columns(headers)
        if column_map is None:
            raise ValueError("This CSV file format is not supported yet.")

        intervals: list[dict[str, object]] = []
        for row in reader:
            start_text = (row.get(column_map["start"]) or "").strip()
            if not start_text:
                continue

            try:
                start_local = parse_interval_csv_timestamp(start_text, local_tz)
                if "end" in column_map:
                    end_text = (row.get(column_map["end"]) or "").strip()
                    if not end_text:
                        continue
                    end_local = parse_interval_csv_timestamp(end_text, local_tz)
                    duration_seconds = int((end_local.astimezone(tz.UTC) - start_local.astimezone(tz.UTC)).total_seconds())
                elif "duration_s" in column_map:
                    duration_seconds = int(float((row.get(column_map["duration_s"]) or "").strip()))
                else:
                    duration_seconds = int(float((row.get(column_map["duration_minutes"]) or "").strip()) * 60)

                if duration_seconds <= 0:
                    continue

                if "usage_kwh" in column_map:
                    interval_kwh = float((row.get(column_map["usage_kwh"]) or "").strip())
                else:
                    interval_kwh = float((row.get(column_map["usage_wh"]) or "").strip()) / 1000.0
            except (TypeError, ValueError):
                continue

            start_epoch = int(start_local.astimezone(tz.UTC).timestamp())
            watt_hours = interval_kwh * 1000.0
            kw = interval_kwh / (duration_seconds / 3600.0)
            intervals.append(
                {
                    "start_epoch": start_epoch,
                    "start": start_local,
                    "duration_s": duration_seconds,
                    "wh": watt_hours,
                    "kw": kw,
                }
            )

    if not intervals:
        raise ValueError("No interval rows were found in this CSV file.")
    return build_interval_frame(intervals)


def parse_interval_file(path: str | Path, tz_name: str = DEFAULT_TZ) -> ParsedIntervalData:
    source = load_interval_source(path)
    adapter = select_utility_feed_adapter(path, source)
    frame = adapter.parse(Path(path), source, tz_name=tz_name)
    return ParsedIntervalData(frame=frame, adapter=adapter.info)


def parse_interval_xml(path: str | Path, tz_name: str = DEFAULT_TZ) -> ParsedIntervalData:
    return parse_interval_file(path, tz_name=tz_name)


def parse_duke_xml(path: str | Path, tz_name: str = DEFAULT_TZ) -> pd.DataFrame:
    """
    Backward-compatible wrapper around the utility feed adapter system.
    """
    return parse_interval_file(path, tz_name=tz_name).frame


def load_intervals_from_db(account_number: str | None = None, tz_name: str = DEFAULT_TZ) -> pd.DataFrame:
    local_tz = tz.gettz(tz_name)
    if local_tz is None:
        raise ValueError(f"Unknown timezone: {tz_name}")

    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        rows = conn.execute(
            """
            SELECT start_epoch, duration_s, wh
            FROM interval_readings
            WHERE account_id = ?
            ORDER BY start_epoch ASC
            """
            ,
            (account["id"],),
        ).fetchall()

    if not rows:
        return pd.DataFrame(columns=["start_epoch", "start", "duration_s", "wh", "kw", "date", "time"])

    frame = pd.DataFrame([dict(row) for row in rows])
    frame["start"] = pd.to_datetime(frame["start_epoch"], unit="s", utc=True).dt.tz_convert(tz_name)
    frame["kw"] = (frame["wh"] * 3600.0) / (frame["duration_s"] * 1000.0)
    frame["date"] = frame["start"].dt.date
    frame["time"] = frame["start"].dt.time
    return frame


def import_interval_frame_to_db(
    frame: pd.DataFrame,
    source_path: str,
    account_number: str | None = None,
    display_name: str | None = None,
    energy_company: str | None = None,
    baseline_date: str | None = None,
    modified_time: float | None = None,
    adapter_id: str = "interval_frame",
    adapter_name: str = "Interval readings",
) -> dict[str, object]:
    if frame.empty:
        raise ValueError("No interval rows were found to import.")

    imported_at = timestamp_now()
    modified_time = modified_time if modified_time is not None else datetime.now(tz.UTC).timestamp()

    with get_db_connection() as conn:
        account = get_or_create_account(
            conn,
            account_number,
            display_name=display_name,
            energy_company=energy_company,
            baseline_date=baseline_date,
        )
        conn.executemany(
            """
            INSERT INTO interval_readings (account_id, start_epoch, duration_s, wh, source_path, imported_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, start_epoch, duration_s) DO UPDATE SET
                wh = excluded.wh,
                source_path = excluded.source_path,
                imported_at = excluded.imported_at
            """,
            [
                (
                    int(account["id"]),
                    int(row.start_epoch),
                    int(row.duration_s),
                    float(row.wh),
                    source_path,
                    imported_at,
                )
                for row in frame.itertuples(index=False)
            ],
        )
        conn.execute(
            """
            INSERT INTO imported_files (account_id, path, modified_time, interval_count, imported_at, service_point_id)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(account_id, path) DO UPDATE SET
                modified_time = excluded.modified_time,
                interval_count = excluded.interval_count,
                imported_at = excluded.imported_at
            """,
            (int(account["id"]), source_path, modified_time, int(frame.shape[0]), imported_at),
        )
        conn.commit()

    return {
        "path": source_path,
        "imported": True,
        "interval_count": int(frame.shape[0]),
        "account_number": account["account_number"],
        "adapter_id": adapter_id,
        "adapter_name": adapter_name,
    }


def import_interval_file_to_db(
    path: str | Path,
    account_number: str | None = None,
    display_name: str | None = None,
    energy_company: str | None = None,
    baseline_date: str | None = None,
) -> dict[str, object]:
    path = Path(path).resolve()
    stat = path.stat()
    modified_time = stat.st_mtime

    with get_db_connection() as conn:
        account = get_or_create_account(
            conn,
            account_number,
            display_name=display_name,
            energy_company=energy_company,
            baseline_date=baseline_date,
        )
        existing = conn.execute(
            """
            SELECT modified_time, interval_count
            FROM imported_files
            WHERE account_id = ? AND path = ?
            """,
            (account["id"], path.as_posix()),
        ).fetchone()
        if existing and float(existing["modified_time"]) == modified_time:
            adapter = detect_utility_feed_adapter(path)
            conn.commit()
            return {
                "path": path.as_posix(),
                "imported": False,
                "interval_count": int(existing["interval_count"]),
                "account_number": account["account_number"],
                "adapter_id": adapter["adapter_id"],
                "adapter_name": adapter["display_name"],
            }

    parsed = parse_interval_xml(path)
    frame = parsed.frame
    return import_interval_frame_to_db(
        frame,
        source_path=path.as_posix(),
        account_number=account_number,
        display_name=display_name,
        energy_company=energy_company,
        baseline_date=baseline_date,
        modified_time=modified_time,
        adapter_id=parsed.adapter.adapter_id,
        adapter_name=parsed.adapter.display_name,
    )


def sync_input_files_to_db(
    account_number: str | None = None,
    display_name: str | None = None,
    energy_company: str | None = None,
    baseline_date: str | None = None,
) -> dict[str, int]:
    ensure_database()
    xml_files = sorted(path for path in INPUT_DIR.rglob("*.xml") if path.is_file())
    synced = 0
    imported = 0
    for path in xml_files:
        result = import_interval_file_to_db(
            path,
            account_number=account_number,
            display_name=display_name,
            energy_company=energy_company,
            baseline_date=baseline_date,
        )
        synced += 1
        if result["imported"]:
            imported += 1
    return {"files_seen": synced, "files_imported": imported}


def count_imported_files(account_number: str | None = None) -> int:
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM imported_files WHERE account_id = ?",
            (account["id"],),
        ).fetchone()
    return 0 if row is None else int(row["count"])


def in_time_window(value: dtime, start: dtime, end: dtime) -> bool:
    if start <= end:
        return start <= value < end
    return value >= start or value < end


def compute_daily_summary(df: pd.DataFrame, night_start_str: str, night_end_str: str) -> pd.DataFrame:
    night_start = dtime.fromisoformat(night_start_str)
    night_end = dtime.fromisoformat(night_end_str)

    def classify(reading_time: dtime) -> str:
        return "night" if in_time_window(reading_time, night_start, night_end) else "other"

    working = df.copy()
    working["bucket"] = working["time"].map(classify)

    daily = working.groupby("date").agg(
        total_kwh=("wh", lambda values: values.sum() / 1000.0),
        avg_kw=("kw", "mean"),
        min_kw=("kw", "min"),
        max_kw=("kw", "max"),
    )

    night = working[working["bucket"] == "night"].groupby("date").agg(
        night_avg_kw=("kw", "mean"),
        night_min_kw=("kw", "min"),
        night_max_kw=("kw", "max"),
    )

    return daily.join(night, how="left")


def flag_suspicious_days(
    summary: pd.DataFrame,
    min_night_kw: float = DEFAULT_MIN_NIGHT_KW,
    night_multiplier: float = DEFAULT_NIGHT_MULTIPLIER,
    baseline_date: str | None = None,
) -> tuple[pd.DataFrame, float | None]:
    baseline = None
    normalized_baseline_date = normalize_optional_date(baseline_date)
    if normalized_baseline_date:
        try:
            baseline_row = summary.loc[ddate.fromisoformat(normalized_baseline_date)]
            baseline_value = baseline_row.get("night_avg_kw", float("nan"))
            if pd.notna(baseline_value):
                baseline = float(baseline_value)
        except KeyError:
            baseline = None

    if baseline is None:
        valid_nights = summary["night_avg_kw"].dropna()
        baseline = valid_nights.median() if not valid_nights.empty else None

    flags: list[dict[str, object]] = []
    for reading_date, row in summary.iterrows():
        night_avg = row.get("night_avg_kw", float("nan"))
        suspicious = False
        reasons: list[str] = []

        if pd.notna(night_avg):
            if night_avg >= min_night_kw:
                suspicious = True
                reasons.append(f"night average stays at or above {min_night_kw:.2f} kW")
            if baseline is not None and night_avg >= baseline * night_multiplier:
                suspicious = True
                reasons.append(
                    f"night average is at least {night_multiplier:.1f}x the overnight baseline ({baseline:.2f} kW)"
                )

        flags.append(
            {
                "date": reading_date,
                "suspicious": suspicious,
                "reasons": "; ".join(reasons),
            }
        )

    flags_df = pd.DataFrame(flags).set_index("date")
    summary_with_flags = summary.join(flags_df[["suspicious", "reasons"]], how="left")
    summary_with_flags["suspicious"] = summary_with_flags["suspicious"].fillna(False)
    summary_with_flags["reasons"] = summary_with_flags["reasons"].fillna("")
    return summary_with_flags, baseline


def compute_alert_events(
    df: pd.DataFrame,
    alert_start_str: str = DEFAULT_ALERT_WINDOW_START,
    alert_end_str: str = DEFAULT_ALERT_WINDOW_END,
    min_kw: float = DEFAULT_ALERT_MIN_KW,
    alert_multiplier: float = DEFAULT_ALERT_MULTIPLIER,
    jump_kw: float = DEFAULT_ALERT_JUMP_KW,
) -> list[dict[str, object]]:
    alert_start = dtime.fromisoformat(alert_start_str)
    alert_end = dtime.fromisoformat(alert_end_str)

    working = df.copy().sort_values("start").reset_index(drop=True)
    working["hour"] = working["start"].dt.hour
    working["prev_kw"] = working["kw"].shift(1)
    working["delta_kw"] = working["kw"] - working["prev_kw"]

    overnight = working[working["time"].map(lambda value: in_time_window(value, alert_start, alert_end))].copy()
    if overnight.empty:
        return []

    overnight_baseline = float(overnight["kw"].median())
    hour_baselines = overnight.groupby("hour")["kw"].median().to_dict()
    hour_counts = overnight.groupby("hour")["kw"].count().to_dict()

    events: list[dict[str, object]] = []
    for row in overnight.itertuples(index=False):
        reasons: list[str] = []
        expected = overnight_baseline
        if int(hour_counts.get(row.hour, 0)) >= 3:
            expected = max(expected, float(hour_baselines.get(row.hour, overnight_baseline)))
        excess_kw = float(row.kw - expected)

        if row.kw >= max(min_kw, expected * alert_multiplier):
            reasons.append("overnight load is much higher than the normal pattern")

        if pd.notna(row.prev_kw) and row.delta_kw >= jump_kw and row.hour < 4:
            reasons.append("load jumps sharply around midnight")

        if not reasons:
            continue

        events.append(
            {
                "timestamp": row.start.isoformat(),
                "date": row.start.date().isoformat(),
                "kw": round(float(row.kw), 3),
                "delta_kw": None if pd.isna(row.delta_kw) else round(float(row.delta_kw), 3),
                "expected_kw": round(expected, 3),
                "excess_kw": round(excess_kw, 3),
                "reasons": "; ".join(reasons),
            }
        )

    events.sort(
        key=lambda event: (
            event["delta_kw"] if event["delta_kw"] is not None else 0,
            event["excess_kw"],
            event["kw"],
        ),
        reverse=True,
    )
    return events


def label_hour(hour: int) -> str:
    suffix = "a" if hour < 12 else "p"
    normalized = hour % 12 or 12
    return f"{normalized}{suffix}"


def compute_hourly_profile(df: pd.DataFrame) -> list[dict[str, object]]:
    hourly = df.groupby(df["start"].dt.hour)["kw"].median()
    available = [float(value) for value in hourly.values if pd.notna(value)]
    max_kw = max(available) if available else 0.0

    profile: list[dict[str, object]] = []
    for hour in range(24):
        value = hourly.get(hour)
        kw = None if pd.isna(value) else round(float(value), 3)
        pct = 0.0 if kw is None or max_kw == 0 else round((kw / max_kw) * 100, 1)
        profile.append(
            {
                "hour": hour,
                "label": label_hour(hour),
                "kw": kw,
                "pct": pct,
                "overnight": 0 <= hour < 6,
            }
        )
    return profile


def round_value(value: float | int | None, digits: int = 3) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)


def normalize_report_date(value: str | ddate | None) -> ddate | None:
    if value is None:
        return None
    if isinstance(value, ddate):
        return value
    normalized = normalize_optional_date(value)
    if normalized is None:
        return None
    return ddate.fromisoformat(normalized)


def format_timestamp_label(value: pd.Timestamp) -> str:
    hour = value.hour % 12 or 12
    suffix = "a.m." if value.hour < 12 else "p.m."
    return f"{hour}:{value.minute:02d} {suffix}"


def format_date_label(value: ddate | None) -> str | None:
    if value is None:
        return None
    return value.strftime("%b %d, %Y")


def summarize_single_day(summary: pd.DataFrame, reading_date: ddate | None) -> dict[str, object] | None:
    if reading_date is None or reading_date not in summary.index:
        return None
    row = summary.loc[reading_date]
    return {
        "date": reading_date.isoformat(),
        "label": format_date_label(reading_date),
        "total_kwh": round(float(row["total_kwh"]), 3),
        "avg_kw": round(float(row["avg_kw"]), 3),
        "min_kw": round(float(row["min_kw"]), 3),
        "max_kw": round(float(row["max_kw"]), 3),
        "night_avg_kw": round_value(row["night_avg_kw"]),
        "night_min_kw": round_value(row["night_min_kw"]),
        "night_max_kw": round_value(row["night_max_kw"]),
        "suspicious": bool(row["suspicious"]),
        "reasons": row["reasons"],
    }


def summarize_delta(current: dict[str, object] | None, reference: dict[str, object] | None) -> dict[str, object] | None:
    if current is None or reference is None:
        return None
    return {
        "total_kwh": round_value(float(current["total_kwh"]) - float(reference["total_kwh"])),
        "night_avg_kw": None
        if current["night_avg_kw"] is None or reference["night_avg_kw"] is None
        else round_value(float(current["night_avg_kw"]) - float(reference["night_avg_kw"])),
        "max_kw": round_value(float(current["max_kw"]) - float(reference["max_kw"])),
    }


def build_day_series(df: pd.DataFrame, reading_date: ddate | None) -> list[dict[str, object]]:
    if reading_date is None:
        return []
    day_frame = df[df["date"] == reading_date].sort_values("start")
    if day_frame.empty:
        return []

    series: list[dict[str, object]] = []
    for row in day_frame.itertuples(index=False):
        series.append(
            {
                "minute": int(row.start.hour * 60 + row.start.minute),
                "label": format_timestamp_label(row.start),
                "kw": round(float(row.kw), 3),
            }
        )
    return series


def find_top_jumps(df: pd.DataFrame, reading_date: ddate) -> list[dict[str, object]]:
    if df.empty:
        return []
    working = df.sort_values("start").copy()
    working["prev_kw"] = working["kw"].shift(1)
    working["delta_kw"] = working["kw"] - working["prev_kw"]
    top = working[
        (working["date"] == reading_date) & pd.notna(working["delta_kw"]) & (working["delta_kw"] > 0)
    ].sort_values("delta_kw", ascending=False)
    jumps: list[dict[str, object]] = []
    for row in top.head(5).itertuples(index=False):
        jumps.append(
            {
                "time": format_timestamp_label(row.start),
                "kw": round(float(row.kw), 3),
                "delta_kw": round(float(row.delta_kw), 3),
            }
        )
    return jumps


def build_day_detail(
    df: pd.DataFrame,
    summary: pd.DataFrame,
    alert_events: list[dict[str, object]],
    target_date: str | ddate | None,
    baseline_date: str | None = None,
) -> dict[str, object] | None:
    focus_date = normalize_report_date(target_date)
    if focus_date is None:
        return None

    current_day = summarize_single_day(summary, focus_date)
    if current_day is None:
        return None

    previous_date = focus_date - timedelta(days=1)
    baseline_day_date = normalize_report_date(baseline_date)
    previous_day = summarize_single_day(summary, previous_date)
    baseline_day = summarize_single_day(summary, baseline_day_date)
    return {
        "date": focus_date.isoformat(),
        "label": format_date_label(focus_date),
        "current_day": current_day,
        "previous_day": previous_day,
        "baseline_day": baseline_day,
        "vs_previous_day": summarize_delta(current_day, previous_day),
        "vs_baseline_day": summarize_delta(current_day, baseline_day),
        "series": {
            "current": build_day_series(df, focus_date),
            "previous": build_day_series(df, previous_date),
            "baseline": build_day_series(df, baseline_day_date),
        },
        "alert_events": [event for event in alert_events if event["date"] == focus_date.isoformat()][:8],
        "top_jumps": find_top_jumps(df, focus_date),
    }


def build_key_findings(
    df: pd.DataFrame,
    summary: pd.DataFrame,
    alert_events: list[dict[str, object]],
    baseline: float | None,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []

    suspicious_days = summary[summary["suspicious"]]
    if not suspicious_days.empty:
        top_day = suspicious_days.sort_values("night_avg_kw", ascending=False).iloc[0]
        baseline_text = "your overnight baseline" if baseline is None else f"the {baseline:.2f} kW overnight baseline"
        findings.append(
            {
                "title": "Highest overnight pull",
                "detail": (
                    f"{suspicious_days.sort_values('night_avg_kw', ascending=False).index[0]} "
                    f"averaged {top_day['night_avg_kw']:.2f} kW overnight, above {baseline_text}."
                ),
            }
        )

    if alert_events:
        top_event = alert_events[0]
        jump_text = ""
        if top_event["delta_kw"] is not None and top_event["delta_kw"] > 0:
            jump_text = f" after a {top_event['delta_kw']:.2f} kW jump"
        findings.append(
            {
                "title": "Sharpest alert moment",
                "detail": f"{top_event['timestamp']} hit {top_event['kw']:.2f} kW{jump_text}.",
            }
        )

    midnight_window = df[df["start"].dt.hour.isin([0, 1, 2])]["kw"]
    dawn_window = df[df["start"].dt.hour.isin([5, 6])]["kw"]
    if not midnight_window.empty and not dawn_window.empty:
        midnight_avg = float(midnight_window.mean())
        dawn_avg = float(dawn_window.mean())
        if dawn_avg > 0:
            ratio = midnight_avg / dawn_avg
            findings.append(
                {
                    "title": "Midnight versus pre-dawn",
                    "detail": (
                        f"Midnight to 3 a.m. averages {midnight_avg:.2f} kW. "
                        f"Five to 6 a.m. averages {dawn_avg:.2f} kW."
                        + (" That is an early spike." if ratio >= 1.2 else "")
                    ),
                }
            )

    if not findings and not df.empty:
        peak = df.sort_values("kw", ascending=False).iloc[0]
        findings.append(
            {
                "title": "Peak reading",
                "detail": f"{peak['start'].isoformat()} reached {peak['kw']:.2f} kW.",
            }
        )

    return findings[:3]


def find_latest_input_file() -> Path | None:
    ensure_data_dirs()
    files = [path for path in INPUT_DIR.rglob("*.xml") if path.is_file()]
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def analyze_interval_data(
    input_path: str | Path,
    tz_name: str = DEFAULT_TZ,
    night_start_str: str = DEFAULT_NIGHT_START,
    night_end_str: str = DEFAULT_NIGHT_END,
    min_night_kw: float = DEFAULT_MIN_NIGHT_KW,
    night_multiplier: float = DEFAULT_NIGHT_MULTIPLIER,
    baseline_date: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, float | None, list[dict[str, object]]]:
    df = parse_interval_xml(input_path, tz_name=tz_name).frame
    summary = compute_daily_summary(df, night_start_str=night_start_str, night_end_str=night_end_str)
    summary_with_flags, baseline = flag_suspicious_days(
        summary,
        min_night_kw=min_night_kw,
        night_multiplier=night_multiplier,
        baseline_date=baseline_date,
    )
    alert_events = compute_alert_events(df=df)
    return df, summary_with_flags, baseline, alert_events


def analyze_history_store(
    account_number: str | None = None,
    tz_name: str = DEFAULT_TZ,
    night_start_str: str = DEFAULT_NIGHT_START,
    night_end_str: str = DEFAULT_NIGHT_END,
    min_night_kw: float = DEFAULT_MIN_NIGHT_KW,
    night_multiplier: float = DEFAULT_NIGHT_MULTIPLIER,
    baseline_date: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, float | None, list[dict[str, object]]]:
    df = load_intervals_from_db(account_number=account_number, tz_name=tz_name)
    if df.empty:
        empty_summary = pd.DataFrame(
            columns=[
                "total_kwh",
                "avg_kw",
                "min_kw",
                "max_kw",
                "night_avg_kw",
                "night_min_kw",
                "night_max_kw",
                "suspicious",
                "reasons",
            ]
        )
        return df, empty_summary, None, []

    summary = compute_daily_summary(df, night_start_str=night_start_str, night_end_str=night_end_str)
    summary_with_flags, baseline = flag_suspicious_days(
        summary,
        min_night_kw=min_night_kw,
        night_multiplier=night_multiplier,
        baseline_date=baseline_date,
    )
    alert_events = compute_alert_events(df=df)
    return df, summary_with_flags, baseline, alert_events


def build_output_path(input_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return OUTPUT_DIR / f"{input_path.stem}-{timestamp}-{uuid4().hex[:8]}.csv"


def build_compare_output_path(left_input_path: Path, right_input_path: Path) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return OUTPUT_DIR / (
        f"{left_input_path.stem}-vs-{right_input_path.stem}-{timestamp}-{uuid4().hex[:8]}.md"
    )


def build_json_report_path(report_path: Path) -> Path:
    return report_path.with_suffix(".json")


def normalize_report_filename(value: str | Path) -> str:
    filename = Path(value).name
    if filename != str(value) or Path(filename).suffix.lower() not in {".csv", ".json", ".md"}:
        raise ValueError("That report file is not available.")
    return filename


def register_report_artifacts(account_number: str | None, paths: list[Path]) -> None:
    filenames = [normalize_report_filename(path.name) for path in paths]
    timestamp = timestamp_now()
    with get_db_connection() as conn:
        account = get_or_create_account(conn, account_number)
        for filename in filenames:
            conn.execute(
                """
                INSERT INTO report_artifacts (filename, account_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(filename) DO UPDATE SET
                    account_id = excluded.account_id,
                    created_at = excluded.created_at
                """,
                (filename, account["id"], timestamp),
            )
        conn.commit()


def get_report_artifact_account_number(filename: str) -> str | None:
    normalized_filename = normalize_report_filename(filename)
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT accounts.account_number
            FROM report_artifacts
            JOIN accounts ON accounts.id = report_artifacts.account_id
            WHERE report_artifacts.filename = ?
            """,
            (normalized_filename,),
        ).fetchone()
    return None if row is None else str(row["account_number"])


def build_report_downloads(report_path: Path | None) -> list[dict[str, str]]:
    if report_path is None:
        return []

    downloads = [
        {
            "label": report_path.suffix.lstrip(".").upper() or "Report",
            "filename": report_path.name,
        }
    ]
    json_path = build_json_report_path(report_path)
    if json_path.name != report_path.name:
        downloads.append({"label": "JSON", "filename": json_path.name})
    return downloads


def build_analysis_settings_snapshot(settings: dict[str, object]) -> dict[str, object]:
    return {
        "tz": settings["tz"],
        "night_start": settings["night_start"],
        "night_end": settings["night_end"],
        "min_night_kw": float(settings["min_night_kw"]),
        "night_multiplier": float(settings["night_multiplier"]),
        "alert_window_start": DEFAULT_ALERT_WINDOW_START,
        "alert_window_end": DEFAULT_ALERT_WINDOW_END,
        "alert_min_kw": DEFAULT_ALERT_MIN_KW,
        "alert_multiplier": DEFAULT_ALERT_MULTIPLIER,
        "alert_jump_kw": DEFAULT_ALERT_JUMP_KW,
    }


def build_ranked_suspicious_days(
    summary_rows: list[dict[str, object]],
    alert_events: list[dict[str, object]],
    baseline: float | None,
    settings: dict[str, object],
) -> list[dict[str, object]]:
    alert_counts: dict[str, int] = {}
    for event in alert_events:
        reading_date = str(event["date"])
        alert_counts[reading_date] = alert_counts.get(reading_date, 0) + 1

    min_night_kw = float(settings["min_night_kw"])
    night_multiplier = float(settings["night_multiplier"])
    baseline_threshold = None if baseline is None else float(baseline) * night_multiplier

    ranked: list[dict[str, object]] = []
    for row in summary_rows:
        if not row["suspicious"]:
            continue

        night_avg = row["night_avg_kw"]
        alert_count = alert_counts.get(str(row["date"]), 0)
        baseline_ratio = None
        if night_avg is not None and baseline not in (None, 0):
            baseline_ratio = round(float(night_avg) / float(baseline), 3)

        threshold_gap_kw = None
        if night_avg is not None:
            threshold_gap_kw = round(max(0.0, float(night_avg) - min_night_kw), 3)

        baseline_gap_kw = None
        if night_avg is not None and baseline_threshold is not None:
            baseline_gap_kw = round(max(0.0, float(night_avg) - baseline_threshold), 3)

        severity_score = round(
            max(0.0, float(night_avg or 0.0) - min_night_kw)
            + max(0.0, float((baseline_ratio or 1.0) - 1.0))
            + (alert_count * 0.25)
            + (float(row["max_kw"]) * 0.1),
            3,
        )

        ranked.append(
            {
                **row,
                "alert_count": alert_count,
                "baseline_ratio": baseline_ratio,
                "threshold_gap_kw": threshold_gap_kw,
                "baseline_gap_kw": baseline_gap_kw,
                "severity_score": severity_score,
            }
        )

    ranked.sort(
        key=lambda row: (
            float(row["severity_score"]),
            float(row["baseline_ratio"] or 0.0),
            float(row["night_avg_kw"] or 0.0),
            float(row["max_kw"]),
            int(row["alert_count"]),
        ),
        reverse=True,
    )
    for index, row in enumerate(ranked, start=1):
        row["severity_rank"] = index
    return ranked


def build_analysis_snapshot(
    subject_name: str,
    df: pd.DataFrame,
    summary: pd.DataFrame,
    alert_events: list[dict[str, object]],
    baseline: float | None,
    report_path: Path | None,
    settings: dict[str, object],
    weather_contexts: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    rows = serialize_summary(summary)
    weather_contexts = weather_contexts or {}
    suspicious_rows = [row for row in rows if row["suspicious"]]
    ranked_suspicious_days = build_ranked_suspicious_days(rows, alert_events, baseline, settings)
    attach_weather_context_to_rows(suspicious_rows, weather_contexts)
    attach_weather_context_to_rows(ranked_suspicious_days, weather_contexts)
    coverage_start = df["date"].min().isoformat() if not df.empty else None
    coverage_end = df["date"].max().isoformat() if not df.empty else None
    focus_date = ranked_suspicious_days[0]["date"] if ranked_suspicious_days else choose_focus_date(rows)
    return {
        "input_file": subject_name,
        "report_file": None if report_path is None else report_path.name,
        "report_files": build_report_downloads(report_path),
        "analysis_generated_at": timestamp_now(),
        "baseline": None if baseline is None else round(float(baseline), 3),
        "summary_rows": rows,
        "suspicious_rows": suspicious_rows,
        "ranked_suspicious_days": ranked_suspicious_days,
        "alert_events": alert_events,
        "hourly_profile": compute_hourly_profile(df),
        "key_findings": build_key_findings(df, summary, alert_events, baseline),
        "days_analyzed": int(summary.shape[0]),
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "settings": build_analysis_settings_snapshot(settings),
        "focus_date": focus_date,
    }


def save_json_report(report_path: Path, payload: dict[str, object]) -> Path:
    json_path = build_json_report_path(report_path)
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return json_path


def analyze_interval_file(
    input_path: str | Path,
    output_path: str | Path | None = None,
    tz_name: str = DEFAULT_TZ,
    night_start_str: str = DEFAULT_NIGHT_START,
    night_end_str: str = DEFAULT_NIGHT_END,
    min_night_kw: float = DEFAULT_MIN_NIGHT_KW,
    night_multiplier: float = DEFAULT_NIGHT_MULTIPLIER,
    baseline_date: str | None = None,
    account_number: str | None = None,
) -> tuple[pd.DataFrame, float | None, Path]:
    ensure_data_dirs()

    input_path = Path(input_path)
    report_path = Path(output_path) if output_path else build_output_path(input_path)

    settings = {
        "tz": tz_name,
        "night_start": night_start_str,
        "night_end": night_end_str,
        "min_night_kw": min_night_kw,
        "night_multiplier": night_multiplier,
    }
    df, summary_with_flags, baseline, alert_events = analyze_interval_data(
        input_path=input_path,
        tz_name=tz_name,
        night_start_str=night_start_str,
        night_end_str=night_end_str,
        min_night_kw=min_night_kw,
        night_multiplier=night_multiplier,
        baseline_date=baseline_date,
    )
    weather_contexts = load_weather_contexts_for_suspicious_days(summary_with_flags, account_number, tz_name)
    report_summary = attach_weather_context_to_summary(summary_with_flags, weather_contexts)
    report_summary.to_csv(report_path, index=True)
    save_json_report(
        report_path,
        build_analysis_snapshot(
            input_path.name,
            df,
            report_summary,
            alert_events,
            baseline,
            report_path,
            settings,
            weather_contexts=weather_contexts,
        ),
    )
    return report_summary, baseline, report_path


def summarize_monthly_usage(summary: pd.DataFrame) -> pd.DataFrame:
    if summary.empty:
        return pd.DataFrame(
            columns=[
                "period_start",
                "period_end",
                "total_kwh",
                "overnight_baseline_kw",
                "flagged_nights",
                "days_in_period",
            ]
        )

    working = summary.reset_index()
    working = working.rename(columns={working.columns[0]: "date"})
    working["month"] = pd.to_datetime(working["date"]).dt.to_period("M")

    monthly = working.groupby("month").agg(
        period_start=("date", "min"),
        period_end=("date", "max"),
        total_kwh=("total_kwh", "sum"),
        overnight_baseline_kw=("night_avg_kw", "median"),
        flagged_nights=("suspicious", "sum"),
        days_in_period=("date", "count"),
    )
    monthly["flagged_nights"] = monthly["flagged_nights"].fillna(0).astype(int)
    return monthly


def format_month_period(period: pd.Period) -> str:
    return period.to_timestamp().strftime("%b %Y")


def calculate_delta(reference: float | int | None, current: float | int | None) -> float | None:
    if reference is None or current is None:
        return None
    if pd.isna(reference) or pd.isna(current):
        return None
    return float(current) - float(reference)


def calculate_percent_change(reference: float | int | None, current: float | int | None) -> float | None:
    if reference is None or current is None:
        return None
    if pd.isna(reference) or pd.isna(current):
        return None
    reference_value = float(reference)
    if reference_value == 0:
        return None
    return ((float(current) - reference_value) / reference_value) * 100.0


def count_month_matches(left_periods: list[pd.Period], right_periods: set[pd.Period], offset_months: int) -> int:
    return sum(1 for period in left_periods if period + offset_months in right_periods)


def select_best_month_offset(
    offsets: set[int],
    left_periods: list[pd.Period],
    right_periods: set[pd.Period],
) -> int | None:
    best_offset = None
    best_score: tuple[int, int, int] | None = None
    for offset in sorted(offsets):
        matches = count_month_matches(left_periods, right_periods, offset)
        if matches == 0:
            continue
        score = (matches, -abs(offset), 1 if offset >= 0 else 0)
        if best_score is None or score > best_score:
            best_offset = offset
            best_score = score
    return best_offset


def choose_comparison_alignment(left_monthly: pd.DataFrame, right_monthly: pd.DataFrame) -> dict[str, object]:
    left_periods = list(left_monthly.index)
    right_periods = list(right_monthly.index)
    right_period_set = set(right_periods)

    if not left_periods or not right_periods:
        raise ValueError("Both files need at least one month of interval data to compare.")

    year_offsets = {
        right.ordinal - left.ordinal
        for left in left_periods
        for right in right_periods
        if right.ordinal != left.ordinal and left.month == right.month and (right.ordinal - left.ordinal) % 12 == 0
    }
    year_offset = select_best_month_offset(year_offsets, left_periods, right_period_set)
    if year_offset is not None:
        return {
            "offset_months": year_offset,
            "alignment_mode": "year_over_year",
            "alignment_label": "year-over-year",
        }

    month_offsets = {-1, 1}
    month_offset = select_best_month_offset(month_offsets, left_periods, right_period_set)
    if month_offset is not None:
        return {
            "offset_months": month_offset,
            "alignment_mode": "month_over_month",
            "alignment_label": "month-over-month",
        }

    generic_offsets = {right.ordinal - left.ordinal for left in left_periods for right in right_periods}
    generic_offset = select_best_month_offset(generic_offsets, left_periods, right_period_set)
    if generic_offset is None:
        raise ValueError("The two files do not share any comparable monthly periods.")
    if generic_offset == 0:
        return {
            "offset_months": generic_offset,
            "alignment_mode": "same_period",
            "alignment_label": "same-period",
        }
    return {
        "offset_months": generic_offset,
        "alignment_mode": "period_shift",
        "alignment_label": f"{abs(generic_offset)}-month shift",
    }


def build_comparison_rows(
    left_monthly: pd.DataFrame,
    right_monthly: pd.DataFrame,
    offset_months: int,
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    rows: list[dict[str, object]] = []
    matched_right_periods: set[pd.Period] = set()

    for left_period, left_row in left_monthly.iterrows():
        right_period = left_period + offset_months
        if right_period not in right_monthly.index:
            continue
        right_row = right_monthly.loc[right_period]
        matched_right_periods.add(right_period)
        rows.append(
            {
                "left_period": str(left_period),
                "right_period": str(right_period),
                "left_period_label": format_month_period(left_period),
                "right_period_label": format_month_period(right_period),
                "comparison_label": f"{format_month_period(left_period)} vs {format_month_period(right_period)}",
                "left_total_kwh": round(float(left_row["total_kwh"]), 3),
                "right_total_kwh": round(float(right_row["total_kwh"]), 3),
                "total_kwh_delta": round(float(right_row["total_kwh"] - left_row["total_kwh"]), 3),
                "total_kwh_delta_pct": calculate_percent_change(left_row["total_kwh"], right_row["total_kwh"]),
                "left_overnight_baseline_kw": None
                if pd.isna(left_row["overnight_baseline_kw"])
                else round(float(left_row["overnight_baseline_kw"]), 3),
                "right_overnight_baseline_kw": None
                if pd.isna(right_row["overnight_baseline_kw"])
                else round(float(right_row["overnight_baseline_kw"]), 3),
                "overnight_baseline_delta_kw": calculate_delta(
                    left_row["overnight_baseline_kw"], right_row["overnight_baseline_kw"]
                ),
                "overnight_baseline_delta_pct": calculate_percent_change(
                    left_row["overnight_baseline_kw"], right_row["overnight_baseline_kw"]
                ),
                "left_flagged_nights": int(left_row["flagged_nights"]),
                "right_flagged_nights": int(right_row["flagged_nights"]),
                "flagged_nights_delta": int(right_row["flagged_nights"] - left_row["flagged_nights"]),
                "left_period_start": left_row["period_start"].isoformat(),
                "left_period_end": left_row["period_end"].isoformat(),
                "right_period_start": right_row["period_start"].isoformat(),
                "right_period_end": right_row["period_end"].isoformat(),
            }
        )

    if not rows:
        raise ValueError("The two files do not share any comparable monthly periods.")

    left_only = [format_month_period(period) for period in left_monthly.index if period + offset_months not in right_monthly.index]
    right_only = [format_month_period(period) for period in right_monthly.index if period not in matched_right_periods]
    return rows, left_only, right_only


def build_comparison_overview(
    rows: list[dict[str, object]],
    left_baseline: float | None,
    right_baseline: float | None,
) -> dict[str, object]:
    left_total_kwh = round(sum(float(row["left_total_kwh"]) for row in rows), 3)
    right_total_kwh = round(sum(float(row["right_total_kwh"]) for row in rows), 3)
    left_flagged_nights = sum(int(row["left_flagged_nights"]) for row in rows)
    right_flagged_nights = sum(int(row["right_flagged_nights"]) for row in rows)
    baseline_delta_kw = calculate_delta(left_baseline, right_baseline)
    baseline_delta_pct = calculate_percent_change(left_baseline, right_baseline)
    return {
        "matched_periods": len(rows),
        "left_total_kwh": left_total_kwh,
        "right_total_kwh": right_total_kwh,
        "total_kwh_delta": round(right_total_kwh - left_total_kwh, 3),
        "total_kwh_delta_pct": calculate_percent_change(left_total_kwh, right_total_kwh),
        "left_flagged_nights": left_flagged_nights,
        "right_flagged_nights": right_flagged_nights,
        "flagged_nights_delta": right_flagged_nights - left_flagged_nights,
        "left_baseline_kw": None if left_baseline is None else round(float(left_baseline), 3),
        "right_baseline_kw": None if right_baseline is None else round(float(right_baseline), 3),
        "baseline_delta_kw": None if baseline_delta_kw is None else round(float(baseline_delta_kw), 3),
        "baseline_delta_pct": None if baseline_delta_pct is None else round(float(baseline_delta_pct), 1),
    }


def build_major_delta_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    candidates: list[tuple[float, dict[str, object]]] = []
    for row in rows:
        total_kwh_pct = row["total_kwh_delta_pct"]
        baseline_delta_kw = row["overnight_baseline_delta_kw"]
        flagged_delta = row["flagged_nights_delta"]

        exceeds_threshold = (
            (total_kwh_pct is not None and abs(float(total_kwh_pct)) >= COMPARE_MAJOR_DELTA_KWH_PCT)
            or (baseline_delta_kw is not None and abs(float(baseline_delta_kw)) >= COMPARE_MAJOR_DELTA_BASELINE_KW)
            or abs(int(flagged_delta)) >= COMPARE_MAJOR_DELTA_FLAGGED_NIGHTS
        )
        if not exceeds_threshold:
            continue

        score = 0.0
        if total_kwh_pct is not None:
            score += abs(float(total_kwh_pct)) / COMPARE_MAJOR_DELTA_KWH_PCT
        if baseline_delta_kw is not None:
            score += abs(float(baseline_delta_kw)) / COMPARE_MAJOR_DELTA_BASELINE_KW
        score += abs(int(flagged_delta)) / COMPARE_MAJOR_DELTA_FLAGGED_NIGHTS
        candidates.append((score, row))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in candidates[:5]]


def build_interval_comparison(
    left_summary: pd.DataFrame,
    right_summary: pd.DataFrame,
    left_baseline: float | None,
    right_baseline: float | None,
    left_label: str,
    right_label: str,
) -> dict[str, object]:
    left_monthly = summarize_monthly_usage(left_summary)
    right_monthly = summarize_monthly_usage(right_summary)
    alignment = choose_comparison_alignment(left_monthly, right_monthly)
    rows, left_only, right_only = build_comparison_rows(
        left_monthly,
        right_monthly,
        int(alignment["offset_months"]),
    )
    return {
        "left_label": left_label,
        "right_label": right_label,
        "alignment_mode": alignment["alignment_mode"],
        "alignment_label": alignment["alignment_label"],
        "offset_months": alignment["offset_months"],
        "rows": rows,
        "overview": build_comparison_overview(rows, left_baseline, right_baseline),
        "major_deltas": build_major_delta_rows(rows),
        "left_only_periods": left_only,
        "right_only_periods": right_only,
    }


def format_number(value: float | int | None, digits: int = 1, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):,.{digits}f}{suffix}"


def format_signed_number(value: float | int | None, digits: int = 1, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):+,.{digits}f}{suffix}"


def format_percent(value: float | int | None, digits: int = 1) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{float(value):+,.{digits}f}%"


def render_comparison_markdown(comparison: dict[str, object]) -> str:
    overview = comparison["overview"]
    rows = comparison["rows"]
    lines = [
        "# Duke interval comparison",
        "",
        f"Compared `{comparison['left_label']}` to `{comparison['right_label']}` using {comparison['alignment_label']} monthly alignment.",
        "",
        f"- Matched months: {overview['matched_periods']}",
        (
            f"- Total kWh: {format_number(overview['left_total_kwh'], 1)} -> "
            f"{format_number(overview['right_total_kwh'], 1)} "
            f"({format_signed_number(overview['total_kwh_delta'], 1)} / {format_percent(overview['total_kwh_delta_pct'])})"
        ),
        (
            f"- Overnight baseline: {format_number(overview['left_baseline_kw'], 2, ' kW')} -> "
            f"{format_number(overview['right_baseline_kw'], 2, ' kW')} "
            f"({format_signed_number(overview['baseline_delta_kw'], 2, ' kW')} / {format_percent(overview['baseline_delta_pct'])})"
        ),
        (
            f"- Flagged nights: {overview['left_flagged_nights']} -> "
            f"{overview['right_flagged_nights']} "
            f"({overview['flagged_nights_delta']:+d})"
        ),
        "",
        "Biggest follow-up points:",
    ]

    major_deltas = comparison["major_deltas"]
    if major_deltas:
        for row in major_deltas:
            lines.append(
                (
                    f"- {row['comparison_label']}: total kWh {format_percent(row['total_kwh_delta_pct'])}, "
                    f"overnight baseline {format_signed_number(row['overnight_baseline_delta_kw'], 2, ' kW')}, "
                    f"flagged nights {int(row['flagged_nights_delta']):+d}"
                )
            )
    else:
        lines.append("- No monthly swings crossed the review thresholds.")

    if comparison["left_only_periods"] or comparison["right_only_periods"]:
        lines.extend(["", "Unmatched months left out of the side-by-side totals:"])
        if comparison["left_only_periods"]:
            lines.append(f"- Left file only: {', '.join(comparison['left_only_periods'])}")
        if comparison["right_only_periods"]:
            lines.append(f"- Right file only: {', '.join(comparison['right_only_periods'])}")

    lines.extend(
        [
            "",
            "| Left period | Right period | Total kWh | Delta | Overnight baseline | Delta | Flagged nights | Delta |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            (
                f"| {row['left_period_label']} | {row['right_period_label']} | "
                f"{format_number(row['left_total_kwh'], 1)} -> {format_number(row['right_total_kwh'], 1)} | "
                f"{format_signed_number(row['total_kwh_delta'], 1)} / {format_percent(row['total_kwh_delta_pct'])} | "
                f"{format_number(row['left_overnight_baseline_kw'], 2, ' kW')} -> "
                f"{format_number(row['right_overnight_baseline_kw'], 2, ' kW')} | "
                f"{format_signed_number(row['overnight_baseline_delta_kw'], 2, ' kW')} / "
                f"{format_percent(row['overnight_baseline_delta_pct'])} | "
                f"{row['left_flagged_nights']} -> {row['right_flagged_nights']} | "
                f"{int(row['flagged_nights_delta']):+d} |"
            )
        )

    return "\n".join(lines) + "\n"


def save_comparison_artifact(report_path: Path, comparison: dict[str, object]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if report_path.suffix.lower() == ".csv":
        pd.DataFrame(comparison["rows"]).to_csv(report_path, index=False)
        return
    report_path.write_text(render_comparison_markdown(comparison), encoding="utf-8")


def build_web_comparison_csv_path(report_path: Path) -> Path:
    return report_path.with_suffix(".csv")


def build_comparison_downloads(report_path: Path, csv_report_path: Path | None = None) -> list[dict[str, str]]:
    downloads = [
        {
            "label": "Markdown" if report_path.suffix.lower() == ".md" else report_path.suffix.lstrip(".").upper(),
            "filename": report_path.name,
        }
    ]
    if csv_report_path is not None:
        downloads.append({"label": "CSV", "filename": csv_report_path.name})
    return downloads


def build_comparison_packet_context(
    comparison: dict[str, object],
    report_path: Path,
    csv_report_path: Path | None = None,
) -> dict[str, object]:
    overview = comparison["overview"]
    total_kwh_detail = (
        f"{format_number(overview['left_total_kwh'], 1, ' kWh')} to "
        f"{format_number(overview['right_total_kwh'], 1, ' kWh')} "
        f"({format_percent(overview['total_kwh_delta_pct'])})."
    )
    baseline_detail = (
        f"{format_number(overview['left_baseline_kw'], 2, ' kW')} to "
        f"{format_number(overview['right_baseline_kw'], 2, ' kW')} "
        f"({format_percent(overview['baseline_delta_pct'])})."
    )
    flagged_delta = int(overview["flagged_nights_delta"])
    metrics = [
        {
            "label": "Matched months",
            "value": str(overview["matched_periods"]),
            "detail": f"Compared with {comparison['alignment_label']} matching.",
        },
        {
            "label": "Total kWh change",
            "value": format_signed_number(overview["total_kwh_delta"], 1, " kWh"),
            "detail": total_kwh_detail,
        },
        {
            "label": "Overnight baseline shift",
            "value": format_signed_number(overview["baseline_delta_kw"], 2, " kW"),
            "detail": baseline_detail,
        },
        {
            "label": "Flagged-night change",
            "value": f"{flagged_delta:+d}",
            "detail": (
                f"{overview['left_flagged_nights']} to {overview['right_flagged_nights']} flagged nights."
            ),
        },
    ]

    follow_up_points = [
        {
            "label": row["comparison_label"],
            "detail": (
                f"Total kWh {format_percent(row['total_kwh_delta_pct'])}, "
                f"overnight baseline {format_signed_number(row['overnight_baseline_delta_kw'], 2, ' kW')}, "
                f"flagged nights {int(row['flagged_nights_delta']):+d}."
            ),
        }
        for row in comparison["major_deltas"]
    ]
    if not follow_up_points:
        follow_up_points.append(
            {
                "label": "No large monthly swing",
                "detail": "The matched months stayed below the review thresholds for kWh, baseline, and flagged nights.",
            }
        )

    matched_rows = [
        {
            "label": row["comparison_label"],
            "total_kwh": (
                f"{format_number(row['left_total_kwh'], 1)} to "
                f"{format_number(row['right_total_kwh'], 1)}"
            ),
            "total_delta": (
                f"{format_signed_number(row['total_kwh_delta'], 1)} / "
                f"{format_percent(row['total_kwh_delta_pct'])}"
            ),
            "overnight_baseline": (
                f"{format_number(row['left_overnight_baseline_kw'], 2, ' kW')} to "
                f"{format_number(row['right_overnight_baseline_kw'], 2, ' kW')}"
            ),
            "baseline_delta": (
                f"{format_signed_number(row['overnight_baseline_delta_kw'], 2, ' kW')} / "
                f"{format_percent(row['overnight_baseline_delta_pct'])}"
            ),
            "flagged_nights": f"{row['left_flagged_nights']} to {row['right_flagged_nights']}",
            "flagged_delta": f"{int(row['flagged_nights_delta']):+d}",
        }
        for row in comparison["rows"]
    ]

    return {
        "report_file": report_path.name,
        "report_files": build_comparison_downloads(report_path, csv_report_path),
        "left_label": comparison["left_label"],
        "right_label": comparison["right_label"],
        "alignment_label": comparison["alignment_label"],
        "metrics": metrics,
        "follow_up_points": follow_up_points,
        "matched_rows": matched_rows,
        "left_only_periods": comparison["left_only_periods"],
        "right_only_periods": comparison["right_only_periods"],
    }


def analyze_interval_file_comparison(
    left_input_path: str | Path,
    right_input_path: str | Path,
    output_path: str | Path | None = None,
    tz_name: str = DEFAULT_TZ,
    night_start_str: str = DEFAULT_NIGHT_START,
    night_end_str: str = DEFAULT_NIGHT_END,
    min_night_kw: float = DEFAULT_MIN_NIGHT_KW,
    night_multiplier: float = DEFAULT_NIGHT_MULTIPLIER,
) -> tuple[dict[str, object], Path]:
    ensure_data_dirs()

    left_input_path = Path(left_input_path)
    right_input_path = Path(right_input_path)
    report_path = Path(output_path) if output_path else build_compare_output_path(left_input_path, right_input_path)

    _, left_summary, left_baseline, _ = analyze_interval_data(
        input_path=left_input_path,
        tz_name=tz_name,
        night_start_str=night_start_str,
        night_end_str=night_end_str,
        min_night_kw=min_night_kw,
        night_multiplier=night_multiplier,
    )
    _, right_summary, right_baseline, _ = analyze_interval_data(
        input_path=right_input_path,
        tz_name=tz_name,
        night_start_str=night_start_str,
        night_end_str=night_end_str,
        min_night_kw=min_night_kw,
        night_multiplier=night_multiplier,
    )

    comparison = build_interval_comparison(
        left_summary=left_summary,
        right_summary=right_summary,
        left_baseline=left_baseline,
        right_baseline=right_baseline,
        left_label=left_input_path.name,
        right_label=right_input_path.name,
    )
    save_comparison_artifact(report_path, comparison)
    return comparison, report_path


def print_human_report(summary: pd.DataFrame, baseline: float | None) -> None:
    print("")
    print("=== OVERNIGHT LOAD REPORT ===")
    print("")
    if baseline is None:
        print("No valid overnight intervals were found, so there is no baseline yet.")
    else:
        print(f"Estimated overnight baseline: {baseline:.2f} kW")
    print("")

    suspicious_days = summary[summary["suspicious"]]
    if suspicious_days.empty:
        print("No days were flagged with the current thresholds.")
        return

    print("Days worth a closer look:")
    for reading_date, row in suspicious_days.iterrows():
        print(
            f"  {reading_date} | total_kWh={row['total_kwh']:.1f} | "
            f"night_avg_kw={row['night_avg_kw']:.2f} | min_kw={row['min_kw']:.2f} | max_kw={row['max_kw']:.2f}"
        )
        if row["reasons"]:
            print(f"    reasons: {row['reasons']}")
        weather_context = row.get("weather_context", "")
        if weather_context:
            print(f"    weather: {weather_context}")
    print("")


def print_comparison_report(comparison: dict[str, object], report_path: Path) -> None:
    overview = comparison["overview"]
    print("")
    print("=== DUKE EXPORT COMPARISON ===")
    print("")
    print(
        f"Aligned {overview['matched_periods']} month(s) using {comparison['alignment_label']} matching."
    )
    print(
        f"Total kWh: {format_number(overview['left_total_kwh'], 1)} -> "
        f"{format_number(overview['right_total_kwh'], 1)} "
        f"({format_signed_number(overview['total_kwh_delta'], 1)} / {format_percent(overview['total_kwh_delta_pct'])})"
    )
    print(
        f"Overnight baseline: {format_number(overview['left_baseline_kw'], 2, ' kW')} -> "
        f"{format_number(overview['right_baseline_kw'], 2, ' kW')} "
        f"({format_signed_number(overview['baseline_delta_kw'], 2, ' kW')} / {format_percent(overview['baseline_delta_pct'])})"
    )
    print(
        f"Flagged nights: {overview['left_flagged_nights']} -> "
        f"{overview['right_flagged_nights']} "
        f"({overview['flagged_nights_delta']:+d})"
    )
    if comparison["major_deltas"]:
        print("")
        print("Months worth a closer look:")
        for row in comparison["major_deltas"]:
            print(
                f"  {row['comparison_label']} | total_kWh={format_percent(row['total_kwh_delta_pct'])} | "
                f"baseline={format_signed_number(row['overnight_baseline_delta_kw'], 2, ' kW')} | "
                f"flagged_nights={int(row['flagged_nights_delta']):+d}"
            )
    if comparison["left_only_periods"] or comparison["right_only_periods"]:
        print("")
        if comparison["left_only_periods"]:
            print(f"Left-only months excluded: {', '.join(comparison['left_only_periods'])}")
        if comparison["right_only_periods"]:
            print(f"Right-only months excluded: {', '.join(comparison['right_only_periods'])}")
    print("")
    print(f"Comparison artifact saved to: {report_path}")


def list_input_files(limit: int = 100) -> list[str]:
    ensure_data_dirs()
    files = [path.relative_to(INPUT_DIR).as_posix() for path in INPUT_DIR.rglob("*.xml")]
    return sorted(files)[:limit]


def list_report_files(limit: int = 20) -> list[str]:
    ensure_data_dirs()
    files = [
        path.name
        for path in OUTPUT_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in {".csv", ".json", ".md"}
    ]
    return sorted(files, reverse=True)[:limit]


def resolve_input_file(relative_path: str) -> Path:
    candidate = (INPUT_DIR / relative_path).resolve()
    base = INPUT_DIR.resolve()
    if not str(candidate).startswith(str(base)):
        raise ValueError("That file is outside the saved history folder.")
    if not candidate.exists():
        raise FileNotFoundError("That file could not be found.")
    if candidate.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("That file format is not supported yet.")
    return candidate


def save_uploaded_file(uploaded_file) -> Path:
    if uploaded_file is None or not uploaded_file.filename:
        raise ValueError("Choose a usage history file.")

    filename = secure_filename(uploaded_file.filename)
    if not filename:
        filename = f"interval-{uuid4().hex}.xml"
    if Path(filename).suffix.lower() not in ALLOWED_SUFFIXES:
        raise ValueError("That file format is not supported yet.")

    destination = INPUT_DIR / filename
    if destination.exists():
        destination = INPUT_DIR / f"{destination.stem}-{uuid4().hex[:8]}{destination.suffix}"
    uploaded_file.save(destination)
    return destination


def serialize_summary(summary: pd.DataFrame) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for reading_date, row in summary.iterrows():
        rows.append(
            {
                "date": reading_date.isoformat(),
                "total_kwh": round(float(row["total_kwh"]), 3),
                "avg_kw": round(float(row["avg_kw"]), 3),
                "min_kw": round(float(row["min_kw"]), 3),
                "max_kw": round(float(row["max_kw"]), 3),
                "night_avg_kw": None if pd.isna(row["night_avg_kw"]) else round(float(row["night_avg_kw"]), 3),
                "night_min_kw": None if pd.isna(row["night_min_kw"]) else round(float(row["night_min_kw"]), 3),
                "night_max_kw": None if pd.isna(row["night_max_kw"]) else round(float(row["night_max_kw"]), 3),
                "suspicious": bool(row["suspicious"]),
                "reasons": row["reasons"],
            }
        )
    return rows


def choose_focus_date(summary_rows: list[dict[str, object]]) -> str | None:
    flagged = [row for row in summary_rows if row["suspicious"]]
    if flagged:
        return flagged[-1]["date"]
    if summary_rows:
        return summary_rows[-1]["date"]
    return None


def build_inventory_comparison(
    load_summary: dict[str, object],
    df: pd.DataFrame,
    baseline: float | None,
) -> dict[str, object]:
    peak_kw = None if df.empty else round(float(df["kw"].max()), 3)
    off_gap_kw = None
    all_on_gap_kw = None
    if baseline is not None:
        off_gap_kw = round(float(baseline) - float(load_summary["off_kw"]), 3)
    if peak_kw is not None:
        all_on_gap_kw = round(float(peak_kw) - float(load_summary["all_on_kw"]), 3)
    return {
        "peak_kw": peak_kw,
        "off_gap_kw": off_gap_kw,
        "all_on_gap_kw": all_on_gap_kw,
    }


def build_settings_defaults() -> dict[str, object]:
    return {
        "tz": DEFAULT_TZ,
        "night_start": DEFAULT_NIGHT_START,
        "night_end": DEFAULT_NIGHT_END,
        "min_night_kw": DEFAULT_MIN_NIGHT_KW,
        "night_multiplier": DEFAULT_NIGHT_MULTIPLIER,
    }


def build_report_context(
    subject_name: str,
    df: pd.DataFrame,
    summary: pd.DataFrame,
    alert_events: list[dict[str, object]],
    baseline: float | None,
    report_path: Path | None,
    settings: dict[str, object],
    account: dict[str, object],
    accounts: list[dict[str, object]],
    household_profile: dict[str, object],
    load_items: list[dict[str, object]],
    imported_files_count: int = 0,
    weather_contexts: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    snapshot = build_analysis_snapshot(
        subject_name,
        df,
        summary,
        alert_events,
        baseline,
        report_path,
        settings,
        weather_contexts=weather_contexts,
    )
    focus_date = snapshot["focus_date"]
    load_summary = build_load_inventory_summary(load_items)
    initial_day_detail = build_day_detail(
        df,
        summary,
        alert_events,
        focus_date,
        baseline_date=account.get("baseline_date"),
    )
    if initial_day_detail is not None:
        initial_day_detail["load_summary"] = load_summary
        initial_day_detail["inventory_alignment"] = {
            "off_gap_kw": None
            if initial_day_detail["current_day"]["night_avg_kw"] is None
            else round(
                float(initial_day_detail["current_day"]["night_avg_kw"]) - float(load_summary["off_kw"]),
                3,
            ),
            "all_on_gap_kw": round(
                float(initial_day_detail["current_day"]["max_kw"]) - float(load_summary["all_on_kw"]),
                3,
            ),
        }
        initial_day_detail["weather"] = load_day_weather(account["account_number"], focus_date, settings["tz"])
    return {
        **snapshot,
        "baseline_date": account.get("baseline_date"),
        "imported_files_count": imported_files_count,
        "initial_day_detail": initial_day_detail,
        "account": account,
        "accounts": accounts,
        "household_profile": household_profile,
        "load_items": load_items,
        "load_summary": load_summary,
        "inventory_comparison": build_inventory_comparison(load_summary, df, baseline),
    }


def parse_settings(form_like) -> dict[str, object]:
    return {
        "tz": form_like.get("tz", DEFAULT_TZ),
        "night_start": form_like.get("night_start", DEFAULT_NIGHT_START),
        "night_end": form_like.get("night_end", DEFAULT_NIGHT_END),
        "min_night_kw": float(form_like.get("min_night_kw", DEFAULT_MIN_NIGHT_KW)),
        "night_multiplier": float(form_like.get("night_multiplier", DEFAULT_NIGHT_MULTIPLIER)),
    }


def has_household_profile_fields(form_like) -> bool:
    keys = {
        "address",
        "zip_code",
        "occupant_count",
        "year_built",
        "square_footage",
        "heating_system",
        "cooling_system",
        "water_heater",
        "notes",
    }
    return any(form_like.get(key) is not None for key in keys)


def build_account_view(
    account_number: str | None,
    settings: dict[str, object],
    report_path: Path | None = None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    account = load_account(account_number)
    accounts = list_accounts()
    household_profile = load_household_profile(account["account_number"])
    load_items = list_load_items(account["account_number"])
    df, summary, baseline, alert_events = analyze_history_store(
        account_number=account["account_number"],
        tz_name=settings["tz"],
        night_start_str=settings["night_start"],
        night_end_str=settings["night_end"],
        min_night_kw=settings["min_night_kw"],
        night_multiplier=settings["night_multiplier"],
        baseline_date=account.get("baseline_date"),
    )
    if df.empty:
        return account, None

    return account, build_report_context(
        "Customer history",
        df,
        summary,
        alert_events,
        baseline,
        report_path,
        settings,
        account=account,
        accounts=accounts,
        household_profile=household_profile,
        load_items=load_items,
        imported_files_count=count_imported_files(account["account_number"]),
    )


def parse_positive_int(value: str | None, default: int = 1) -> int:
    try:
        return max(1, int(value or default))
    except (TypeError, ValueError):
        return default


def build_account_scaffold(
    account_number: str | None,
    account_search: str | None = None,
    account_page_number: int = 1,
) -> dict[str, object]:
    account = load_account(account_number)
    account_page = list_account_page(search=account_search, page=account_page_number, per_page=10)
    household_profile = load_household_profile(account["account_number"])
    load_items = list_load_items(account["account_number"])
    data_authorizations = list_account_data_authorizations(account["account_number"])
    return {
        "account": account,
        "accounts": account_page["accounts"],
        "account_page": account_page,
        "household_profile": household_profile,
        "load_items": load_items,
        "load_summary": build_load_inventory_summary(load_items),
        "account_access": list_account_access_emails(account["account_number"]),
        "utility_connections": list_utility_connections(account["account_number"]),
        "data_authorizations": data_authorizations,
        "has_active_data_authorization": any(item["active"] for item in data_authorizations),
    }


def build_customer_account_scaffold(
    customer_user: dict[str, object],
    account_number: str | None,
    account_search: str | None = None,
    account_page_number: int = 1,
) -> dict[str, object]:
    customer_email = str(customer_user["email"])
    selected_account_number = choose_customer_account_number(customer_email, account_number)
    account_page = list_customer_account_page(
        customer_email,
        search=account_search,
        page=account_page_number,
        per_page=10,
    )
    if selected_account_number is None:
        return {
            "account": None,
            "accounts": account_page["accounts"],
            "account_page": account_page,
            "household_profile": {},
            "load_items": [],
            "load_summary": build_load_inventory_summary([]),
            "account_access": [],
            "customer_account_access": None,
            "utility_connections": [],
            "data_authorizations": [],
            "customer_data_authorization": None,
            "has_active_data_authorization": False,
        }
    account = find_account(selected_account_number) or load_account(selected_account_number)
    household_profile = load_household_profile(account["account_number"])
    load_items = list_load_items(account["account_number"])
    data_authorizations = list_account_data_authorizations(account["account_number"])
    customer_data_authorization = next(
        (
            authorization
            for authorization in data_authorizations
            if int(authorization["customer_user_id"]) == int(customer_user["id"])
        ),
        None,
    )
    return {
        "account": account,
        "accounts": account_page["accounts"],
        "account_page": account_page,
        "household_profile": household_profile,
        "load_items": load_items,
        "load_summary": build_load_inventory_summary(load_items),
        "account_access": list_account_access_emails(account["account_number"]),
        "customer_account_access": get_customer_account_access(customer_email, account["account_number"]),
        "utility_connections": list_utility_connections(account["account_number"]),
        "data_authorizations": data_authorizations,
        "customer_data_authorization": customer_data_authorization,
        "has_active_data_authorization": any(item["active"] for item in data_authorizations),
    }


def create_web_app() -> Flask:
    validate_runtime_security()
    app = Flask(__name__)
    app.secret_key = get_app_secret()
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=is_production_environment(),
        SESSION_COOKIE_NAME="home_energy_watch_session",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    )
    if (os.getenv("POWER_TRUST_PROXY") or "").strip().lower() in {"1", "true", "yes"}:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    marketing_endpoints = {
        "index",
        "pricing_page",
        "how_it_works_page",
        "for_homeowners_page",
        "for_commissions_page",
        "terms_page",
        "privacy_page",
        "utility_data_authorization_page",
        "robots_txt",
        "sitemap_xml",
        "health",
        "static",
    }

    def get_csrf_token() -> str:
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return str(token)

    def auth_request_identity(scope: str, email: str | None) -> str:
        return auth_rate_limit_identity(scope, email, request.remote_addr)

    def auth_limit_message(retry_after: int) -> str:
        minutes = max(1, (int(retry_after) + 59) // 60)
        return f"Too many sign-in attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}."

    @app.before_request
    def establish_request_context():
        g.request_id = uuid4().hex

    @app.before_request
    def enforce_csrf_protection():
        if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            return None
        if request.endpoint == "stripe_webhook":
            return None
        if app.config.get("TESTING") and not app.config.get("CSRF_ENFORCE_TESTS"):
            return None
        expected = session.get("_csrf_token")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
        if expected and supplied and secrets.compare_digest(str(expected), str(supplied)):
            return None
        message = "That form has expired. Refresh the page and try again."
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": message}), 400
        return message, 400

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Request-ID"] = str(getattr(g, "request_id", "") or uuid4().hex)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
            "form-action 'self' https://app.homeenergywatch.com",
        )
        if session.get("staff_user_id") or session.get("customer_user_id"):
            response.headers["Cache-Control"] = "no-store, private"
        if is_production_environment():
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    def current_staff_user() -> dict[str, object] | None:
        staff_user_id = session.get("staff_user_id")
        if staff_user_id is None:
            return None
        staff_user = get_staff_user_by_id(int(staff_user_id))
        if staff_user is None or not staff_user.get("is_active"):
            session.pop("staff_user_id", None)
            session.pop("staff_auth_version", None)
            return None
        session_auth_version = session.get("staff_auth_version")
        current_auth_version = int(staff_user.get("auth_version") or 1)
        if session_auth_version is None:
            session["staff_auth_version"] = current_auth_version
        elif int(session_auth_version) != current_auth_version:
            session.pop("staff_user_id", None)
            session.pop("staff_auth_version", None)
            return None
        return staff_user

    def current_customer_user() -> dict[str, object] | None:
        customer_user_id = session.get("customer_user_id")
        if customer_user_id is None:
            return None
        customer_user = get_customer_user_by_id(int(customer_user_id))
        if customer_user is None or not customer_user.get("is_active"):
            session.pop("customer_user_id", None)
            session.pop("customer_auth_version", None)
            return None
        session_auth_version = session.get("customer_auth_version")
        current_auth_version = int(customer_user.get("auth_version") or 1)
        if session_auth_version is None:
            session["customer_auth_version"] = current_auth_version
        elif int(session_auth_version) != current_auth_version:
            session.pop("customer_user_id", None)
            session.pop("customer_auth_version", None)
            return None
        return customer_user

    def establish_staff_session(staff_user: dict[str, object]) -> None:
        session.clear()
        session.permanent = True
        session["staff_user_id"] = int(staff_user["id"])
        session["staff_auth_version"] = int(staff_user.get("auth_version") or 1)

    def establish_pending_staff_mfa_session(staff_user: dict[str, object], next_url: str) -> None:
        session.clear()
        session.permanent = True
        session["pending_staff_user_id"] = int(staff_user["id"])
        session["pending_staff_auth_version"] = int(staff_user.get("auth_version") or 1)
        session["pending_staff_mfa_at"] = timestamp_now()
        if next_url.startswith("/") and not next_url.startswith("//"):
            session["pending_staff_next"] = next_url

    def pending_staff_mfa_user() -> dict[str, object] | None:
        staff_user_id = session.get("pending_staff_user_id")
        started_at = session.get("pending_staff_mfa_at")
        if staff_user_id is None or not started_at:
            return None
        try:
            expired = datetime.fromisoformat(str(started_at)) + timedelta(
                minutes=MFA_CHALLENGE_EXPIRY_MINUTES
            ) <= datetime.now()
        except ValueError:
            expired = True
        staff_user = get_staff_user_by_id(int(staff_user_id))
        if (
            expired
            or staff_user is None
            or not staff_user.get("is_active")
            or not staff_user.get("mfa_enabled")
            or int(session.get("pending_staff_auth_version") or 0)
            != int(staff_user.get("auth_version") or 1)
        ):
            session.clear()
            return None
        return staff_user

    def staff_bootstrap_needed() -> bool:
        return count_staff_users() == 0

    def next_destination(default_endpoint: str = "index") -> str:
        next_url = request.args.get("next") or request.form.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//"):
            return next_url
        return url_for(default_endpoint)

    def require_staff_user(api: bool = False):
        if staff_bootstrap_needed():
            if api:
                return jsonify({"error": "Set up the first commission user to continue."}), 403
            return redirect(url_for("first_run"))

        staff_user = current_staff_user()
        if staff_user is None:
            if api:
                return jsonify({"error": "Sign in to continue."}), 401
            return redirect(url_for("login", next=request.full_path if request.query_string else request.path))
        return staff_user

    def require_customer_user(api: bool = False):
        customer_user = current_customer_user()
        if customer_user is None:
            if api:
                return jsonify({"error": "Sign in to continue."}), 401
            return redirect(url_for("customer_login", next=request.full_path if request.query_string else request.path))
        return customer_user

    @app.before_request
    def enforce_required_staff_mfa():
        if not staff_mfa_required() or session.get("staff_user_id") is None:
            return None
        allowed_endpoints = {
            "staff_security",
            "staff_mfa_start",
            "staff_mfa_confirm",
            "staff_mfa_cancel",
            "logout",
            "health",
            "static",
        }
        if request.endpoint in allowed_endpoints:
            return None
        staff_user = current_staff_user()
        if staff_user is not None and not staff_user.get("mfa_enabled"):
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"error": "Set up authenticator protection to continue."}), 403
            return redirect(url_for("staff_security"))
        return None

    def require_account_actor(account_number: str | None, api: bool = False, write: bool = False):
        staff_user = current_staff_user()
        if staff_user is not None:
            return {"kind": "staff", "user": staff_user}

        customer_user = current_customer_user()
        if customer_user is not None:
            account_access = get_customer_account_access(str(customer_user["email"]), account_number)
            if account_access is not None:
                if write and account_access["access_level"] != "Manager":
                    message = "Manager access is required to change this account."
                    if api:
                        return jsonify({"error": message}), 403
                    return message, 403
                return {"kind": "customer", "user": customer_user, "account_access": account_access}
            if api:
                return jsonify({"error": "That account is not available for this sign-in."}), 403
            flash("That account is not available for this sign-in.")
            return redirect(url_for("customer_dashboard"))

        if api:
            return jsonify({"error": "Sign in to continue."}), 401
        return redirect(url_for("customer_login", next=request.full_path if request.query_string else request.path))

    def record_actor_event(
        actor: dict[str, object],
        action: str,
        *,
        account_number: str | None = None,
        target_type: str | None = None,
        target_id: object | None = None,
        metadata: dict[str, object] | None = None,
    ) -> int:
        user = actor["user"]
        return record_audit_event(
            action,
            actor_type=str(actor["kind"]),
            actor_id=int(user["id"]),
            account_number=account_number,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata,
        )

    def signed_in_for_reports(api: bool = False):
        staff_user = current_staff_user()
        if staff_user is not None:
            return {"kind": "staff", "user": staff_user}
        customer_user = current_customer_user()
        if customer_user is not None:
            return {"kind": "customer", "user": customer_user}
        if api:
            return jsonify({"error": "Sign in to continue."}), 401
        return redirect(url_for("customer_login", next=request.full_path if request.query_string else request.path))

    def actor_redirect(account_number: str | None = None):
        if current_customer_user() is not None and current_staff_user() is None:
            return redirect(url_for("customer_dashboard", account_number=normalize_account_number(account_number)))
        return redirect(url_for("index", account_number=normalize_account_number(account_number)))

    def redirect_back_or_account(account_number: str | None = None):
        return_to = (request.form.get("return_to") or "").strip()
        if return_to.startswith("/") and not return_to.startswith("//"):
            return redirect(return_to)
        return actor_redirect(account_number)

    def require_commissioner(api: bool = False):
        staff_user = require_staff_user(api=api)
        if not isinstance(staff_user, dict):
            return staff_user
        if staff_user["role"] != "Commissioner":
            if api:
                return jsonify({"error": "Commissioner access is required for that action."}), 403
            flash("Commissioner access is required for that action.")
            return redirect(url_for("index"))
        return staff_user

    def consume_latest_invite_url() -> str | None:
        token = session.pop("latest_invite_token", None)
        if not token:
            return None
        return url_for("accept_staff_invite_route", token=token, _external=True)

    def build_marketing_page_context(
        *,
        page_title: str,
        page_description: str,
        active_page: str,
    ) -> dict[str, object]:
        marketing_base_url = build_marketing_base_url(request.url_root)
        app_base_url = build_public_base_url(request.url_root)
        return {
            "page_title": page_title,
            "page_description": page_description,
            "active_page": active_page,
            "marketing_base_url": marketing_base_url,
            "app_base_url": app_base_url,
            "home_url": build_absolute_url(marketing_base_url, "/"),
            "pricing_url": build_absolute_url(marketing_base_url, "/pricing"),
            "how_it_works_url": build_absolute_url(marketing_base_url, "/how-it-works"),
            "for_homeowners_url": build_absolute_url(marketing_base_url, "/for-homeowners"),
            "for_commissions_url": build_absolute_url(marketing_base_url, "/for-commissions"),
            "terms_url": build_absolute_url(marketing_base_url, "/terms"),
            "privacy_url": build_absolute_url(marketing_base_url, "/privacy"),
            "utility_authorization_url": build_absolute_url(marketing_base_url, "/utility-data-authorization"),
            "start_home_url": build_absolute_url(app_base_url, "/signup"),
            "home_login_url": build_absolute_url(app_base_url, "/customer/login"),
            "commission_login_url": build_absolute_url(app_base_url, "/login"),
            "canonical_url": build_absolute_url(marketing_base_url, request.path),
            "robots_meta": "index,follow",
        }

    def build_staff_account_context(page_title: str, setup_section: str | None = None) -> dict[str, object]:
        ensure_data_dirs()
        defaults = build_settings_defaults()
        account_number = request.args.get("account_number")
        account_search = request.args.get("account_search")
        account_page_number = parse_positive_int(request.args.get("account_page"), default=1)
        scaffold = build_account_scaffold(account_number, account_search, account_page_number)
        return {
            "defaults": defaults,
            "analysis": None,
            "account": scaffold["account"],
            "accounts": scaffold["accounts"],
            "account_page": scaffold["account_page"],
            "household_profile": scaffold["household_profile"],
            "load_items": scaffold["load_items"],
            "load_summary": scaffold["load_summary"],
            "account_access": scaffold["account_access"],
            "utility_connections": scaffold["utility_connections"],
            "data_authorizations": scaffold["data_authorizations"],
            "has_active_data_authorization": scaffold["has_active_data_authorization"],
            "setup_section": setup_section,
            "active_account_number": scaffold["account"]["account_number"],
            "page_title": page_title,
        }

    def build_customer_account_context(
        customer_user: dict[str, object],
        page_title: str,
        setup_section: str | None = None,
    ) -> dict[str, object] | None:
        ensure_data_dirs()
        account_number = request.args.get("account_number")
        account_search = request.args.get("account_search")
        account_page_number = parse_positive_int(request.args.get("account_page"), default=1)
        scaffold = build_customer_account_scaffold(customer_user, account_number, account_search, account_page_number)
        if scaffold["account"] is None:
            return None
        return {
            "defaults": build_settings_defaults(),
            "analysis": None,
            "account": scaffold["account"],
            "accounts": scaffold["accounts"],
            "account_page": scaffold["account_page"],
            "household_profile": scaffold["household_profile"],
            "load_items": scaffold["load_items"],
            "load_summary": scaffold["load_summary"],
            "account_access": scaffold["account_access"],
            "customer_account_access": scaffold["customer_account_access"],
            "utility_connections": scaffold["utility_connections"],
            "data_authorizations": scaffold["data_authorizations"],
            "customer_data_authorization": scaffold["customer_data_authorization"],
            "has_active_data_authorization": scaffold["has_active_data_authorization"],
            "setup_section": setup_section,
            "active_account_number": scaffold["account"]["account_number"],
            "customer_mode": True,
            "billing": load_customer_billing(int(customer_user["id"])),
            "page_title": page_title,
        }

    def render_customer_setup_page(customer_user: dict[str, object], page_title: str, setup_section: str):
        context = build_customer_account_context(customer_user, page_title, setup_section)
        if context is None:
            return render_template("customer_empty.html", page_title="Your energy history", customer_mode=True)
        return render_template("setup_section.html", **context)

    def build_customer_signup_form_state(form_like=None) -> dict[str, str]:
        source = form_like or {}

        def value(name: str, default: str = "") -> str:
            raw = source.get(name, default)
            if raw is None:
                return default
            return str(raw)

        selected_plan_id = value("plan_id", "home").strip().lower() or "home"
        return {
            "full_name": value("full_name"),
            "email": value("email"),
            "password": "",
            "account_number": value("account_number"),
            "address": value("address"),
            "zip_code": value("zip_code"),
            "plan_id": selected_plan_id,
            "accept_policies": "yes" if form_checkbox_checked(source.get("accept_policies")) else "",
            "confirm_account_authority": (
                "yes" if form_checkbox_checked(source.get("confirm_account_authority")) else ""
            ),
        }

    def render_customer_signup_page(form_like=None):
        signup_form = build_customer_signup_form_state(form_like)
        return render_template(
            "customer_signup.html",
            page_title="Create Your Account",
            selected_plan_id=signup_form["plan_id"],
            signup_form=signup_form,
        )

    def start_customer_session(customer_user: dict[str, object]) -> None:
        session.clear()
        session.permanent = True
        session["customer_user_id"] = int(customer_user["id"])
        session["customer_auth_version"] = int(customer_user.get("auth_version") or 1)

    def render_verification_notice(email: str | None = None):
        pending_email = (email or session.get("pending_verification_email") or "").strip()
        return render_template(
            "customer_verification_notice.html",
            page_title="Check Your Email",
            pending_email=pending_email,
            masked_email=mask_email_address(pending_email),
        )

    def continue_customer_after_verification(customer_user: dict[str, object]):
        start_customer_session(customer_user)
        billing = load_customer_billing(int(customer_user["id"]))
        selected_plan = get_billing_plan(str(billing["plan_id"]))
        if selected_plan["payment_ready"]:
            try:
                session_obj = create_customer_checkout_session(
                    customer_user,
                    str(billing["plan_id"]),
                    build_public_base_url(request.url_root),
                )
                return redirect(str(session_obj["url"]))
            except Exception as exc:
                flash(str(exc))
        return redirect(url_for("customer_dashboard"))

    @app.context_processor
    def inject_layout_context():
        return {
            "staff_user": current_staff_user(),
            "customer_user": current_customer_user(),
            "staff_roles": STAFF_ROLES,
            "customer_access_levels": CUSTOMER_ACCESS_LEVELS,
            "billing_plans": list_billing_plans(),
            "supported_feeds": list_supported_utility_adapters(),
            "utility_access_guides": list_utility_access_guides(),
            "csrf_token": get_csrf_token,
            "app_base_url": build_public_base_url(),
            "marketing_base_url": build_marketing_base_url(),
            "request_on_marketing_host": is_marketing_host(current_request_host()),
        }

    @app.before_request
    def send_marketing_host_app_routes_to_app_host():
        endpoint = request.endpoint or ""
        if not is_marketing_host(current_request_host()):
            return None
        if endpoint in marketing_endpoints:
            return None

        app_base_url = build_public_base_url(request.url_root)
        query_suffix = f"?{request.query_string.decode()}" if request.query_string else ""
        target = f"{app_base_url}{request.path}{query_suffix}"
        redirect_code = 307 if request.method not in {"GET", "HEAD", "OPTIONS"} else 302
        return redirect(target, code=redirect_code)

    def render_customer_dashboard(customer_user: dict[str, object]):
        ensure_data_dirs()
        defaults = build_settings_defaults()
        account_number = request.args.get("account_number")
        account_search = request.args.get("account_search")
        account_page_number = parse_positive_int(request.args.get("account_page"), default=1)
        scaffold = build_customer_account_scaffold(customer_user, account_number, account_search, account_page_number)
        if scaffold["account"] is None:
            return render_template("customer_empty.html", page_title="Your energy history", customer_mode=True)

        latest_analysis = None
        account = scaffold["account"]
        try:
            df, summary, baseline, alert_events = analyze_history_store(
                account_number=account["account_number"],
                tz_name=defaults["tz"],
                night_start_str=defaults["night_start"],
                night_end_str=defaults["night_end"],
                min_night_kw=defaults["min_night_kw"],
                night_multiplier=defaults["night_multiplier"],
                baseline_date=account.get("baseline_date"),
            )
            if not df.empty:
                latest_analysis = build_report_context(
                    "Customer history",
                    df,
                    summary,
                    alert_events,
                    baseline,
                    None,
                    defaults,
                    account=account,
                    accounts=scaffold["accounts"],
                    household_profile=scaffold["household_profile"],
                    load_items=scaffold["load_items"],
                    imported_files_count=count_imported_files(account["account_number"]),
                )
        except Exception:
            latest_analysis = None

        return render_template(
            "index.html",
            defaults=defaults,
            analysis=latest_analysis,
            account=account,
            accounts=scaffold["accounts"],
            account_page=scaffold["account_page"],
            household_profile=scaffold["household_profile"],
            load_items=scaffold["load_items"],
            load_summary=scaffold["load_summary"],
            account_access=scaffold["account_access"],
            customer_account_access=scaffold["customer_account_access"],
            utility_connections=scaffold["utility_connections"],
            data_authorizations=scaffold["data_authorizations"],
            customer_data_authorization=scaffold["customer_data_authorization"],
            has_active_data_authorization=scaffold["has_active_data_authorization"],
            staff_team=[],
            latest_invite_url=None,
            customer_mode=True,
            billing=load_customer_billing(int(customer_user["id"])),
            active_account_number=account["account_number"],
            page_title="Your energy history",
        )

    @app.get("/first-run")
    def first_run():
        if not staff_bootstrap_needed():
            if current_staff_user() is not None:
                return redirect(url_for("index"))
            return redirect(url_for("login"))
        return render_template("first_run.html", page_title="Commission Setup")

    @app.post("/first-run")
    def create_first_run_user():
        if not staff_bootstrap_needed():
            return redirect(url_for("login"))
        try:
            staff_user = create_first_staff_user(
                request.form.get("email", ""),
                request.form.get("full_name", ""),
                request.form.get("password", ""),
            )
        except Exception as exc:
            flash(str(exc))
            return redirect(url_for("first_run"))

        session.clear()
        session.permanent = True
        session["staff_user_id"] = int(staff_user["id"])
        session["staff_auth_version"] = int(staff_user.get("auth_version") or 1)
        record_audit_event(
            "staff.first_run_completed",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user["id"],
        )
        return redirect(url_for("index"))

    @app.get("/signup")
    def customer_signup():
        if current_customer_user() is not None and current_staff_user() is None:
            return redirect(url_for("customer_dashboard"))
        return render_customer_signup_page(request.args)

    @app.post("/signup")
    def customer_signup_post():
        selected_plan_id = request.form.get("plan_id") or "home"
        try:
            accept_policies = form_checkbox_checked(request.form.get("accept_policies"))
            confirm_account_authority = form_checkbox_checked(
                request.form.get("confirm_account_authority")
            )
            if not accept_policies:
                raise ValueError("Agree to the Terms and Privacy Notice to create an account.")
            if not confirm_account_authority:
                raise ValueError("Confirm that you are allowed to manage this electric account.")
            energy_company = resolve_energy_company_for_form(request.form, require_zip=True)
            customer_user, account, billing = create_customer_signup(
                email=request.form.get("email", ""),
                full_name=request.form.get("full_name", ""),
                password=request.form.get("password", ""),
                account_number=request.form.get("account_number"),
                energy_company=energy_company,
                plan_id=selected_plan_id,
                household_form=request.form,
                accept_policies=accept_policies,
                confirm_account_authority=confirm_account_authority,
                evidence_remote_hash=request_remote_hash(),
                evidence_user_agent_hash=request_user_agent_hash(),
            )
        except Exception as exc:
            flash(str(exc))
            return render_customer_signup_page(request.form)

        record_audit_event(
            "customer.signup",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            account_number=str(account["account_number"]),
            target_type="customer_user",
            target_id=customer_user["id"],
            metadata={
                "plan_id": str(billing["plan_id"]),
                "terms_version": CURRENT_TERMS_VERSION,
                "privacy_version": CURRENT_PRIVACY_VERSION,
                "utility_authorization_version": CURRENT_UTILITY_AUTHORIZATION_VERSION,
            },
        )
        record_audit_event(
            "customer.policy_accepted",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            account_number=str(account["account_number"]),
            target_type="policy_acceptance",
            target_id=f"{CURRENT_TERMS_VERSION}:{CURRENT_PRIVACY_VERSION}",
            metadata={
                "terms_version": CURRENT_TERMS_VERSION,
                "privacy_version": CURRENT_PRIVACY_VERSION,
            },
        )
        signup_authorization = get_customer_account_data_authorization(
            str(account["account_number"]),
            int(customer_user["id"]),
        )
        if signup_authorization is None:
            raise RuntimeError("The utility data permission record could not be confirmed.")
        record_audit_event(
            "utility.authorization_granted",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            account_number=str(account["account_number"]),
            target_type="account_data_authorization",
            target_id=signup_authorization["id"],
            metadata={
                "authorization_version": CURRENT_UTILITY_AUTHORIZATION_VERSION,
                "authorization_scope": UTILITY_AUTHORIZATION_SCOPE,
            },
        )
        if not customer_user.get("email_verified"):
            session.clear()
            session.permanent = True
            session["pending_verification_email"] = str(customer_user["email"])
            try:
                send_customer_verification_email(
                    customer_user,
                    build_public_base_url(request.url_root),
                )
                record_audit_event(
                    "customer.email_verification_sent",
                    actor_type="customer",
                    actor_id=int(customer_user["id"]),
                    account_number=str(account["account_number"]),
                    target_type="customer_user",
                    target_id=customer_user["id"],
                )
            except Exception as exc:
                flash("We could not send the confirmation message. Try sending it again in a moment.")
                record_audit_event(
                    "customer.email_verification_delivery_failed",
                    actor_type="customer",
                    actor_id=int(customer_user["id"]),
                    account_number=str(account["account_number"]),
                    target_type="customer_user",
                    target_id=customer_user["id"],
                    metadata={"error_type": type(exc).__name__},
                )
            return render_verification_notice(str(customer_user["email"]))

        return continue_customer_after_verification(customer_user)

    @app.get("/customer/verification-sent")
    def customer_verification_sent():
        return render_verification_notice()

    @app.post("/customer/verification/resend")
    def customer_verification_resend():
        email = request.form.get("email") or session.get("pending_verification_email") or ""
        identity_hash = auth_request_identity("customer_verification_resend", str(email))
        limit_status = auth_rate_limit_status("customer_verification_resend", identity_hash)
        if limit_status["blocked"]:
            flash("Please wait before asking for another confirmation message.")
            return render_verification_notice(str(email)), 429
        record_auth_failure("customer_verification_resend", identity_hash)
        customer_user = get_customer_user_by_email(str(email))
        if customer_user is not None and not customer_user.get("email_verified"):
            try:
                send_customer_verification_email(customer_user, build_public_base_url(request.url_root))
                record_audit_event(
                    "customer.email_verification_resent",
                    actor_type="customer",
                    actor_id=int(customer_user["id"]),
                    target_type="customer_user",
                    target_id=customer_user["id"],
                )
            except Exception as exc:
                record_audit_event(
                    "customer.email_verification_delivery_failed",
                    actor_type="customer",
                    actor_id=int(customer_user["id"]),
                    target_type="customer_user",
                    target_id=customer_user["id"],
                    metadata={"error_type": type(exc).__name__},
                )
        session["pending_verification_email"] = str(email).strip()
        flash("If that address still needs confirmation, a new message is on its way.")
        return render_verification_notice(str(email))

    @app.get("/customer/verify-email")
    def customer_verify_email():
        token = request.args.get("token", "")
        token_record = load_valid_customer_auth_token(token, "verify_email")
        return render_template(
            "customer_verify_email.html",
            page_title="Confirm Your Email",
            token=token,
            token_valid=token_record is not None,
            masked_email=(
                mask_email_address(str(token_record["customer_user"]["email"]))
                if token_record is not None
                else ""
            ),
        )

    @app.post("/customer/verify-email")
    def customer_verify_email_post():
        try:
            customer_user = verify_customer_email(request.form.get("token"))
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("customer_verification_sent"))
        session.pop("pending_verification_email", None)
        account_number = choose_customer_account_number(str(customer_user["email"]))
        record_audit_event(
            "customer.email_verified",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            account_number=account_number,
            target_type="customer_user",
            target_id=customer_user["id"],
        )
        flash("Your email address is confirmed.")
        return continue_customer_after_verification(customer_user)

    @app.get("/customer/forgot-password")
    def customer_forgot_password():
        return render_template(
            "customer_forgot_password.html",
            page_title="Reset Your Password",
            request_sent=False,
        )

    @app.post("/customer/forgot-password")
    def customer_forgot_password_post():
        email = request.form.get("email", "")
        identity_hash = auth_request_identity("customer_password_reset", email)
        limit_status = auth_rate_limit_status("customer_password_reset", identity_hash)
        if limit_status["blocked"]:
            return (
                render_template(
                    "customer_forgot_password.html",
                    page_title="Reset Your Password",
                    request_sent=True,
                    rate_limited=True,
                ),
                429,
            )
        record_auth_failure("customer_password_reset", identity_hash)
        customer_user = get_customer_user_by_email(email)
        delivered = False
        if customer_user is not None and customer_user.get("is_active"):
            try:
                send_customer_password_reset_email(customer_user, build_public_base_url(request.url_root))
                delivered = True
            except Exception:
                delivered = False
        record_audit_event(
            "customer.password_reset_requested",
            actor_type="customer" if customer_user is not None else "anonymous",
            actor_id=None if customer_user is None else int(customer_user["id"]),
            target_type="customer_user" if customer_user is not None else None,
            target_id=None if customer_user is None else customer_user["id"],
            metadata={"delivery_attempted": delivered},
        )
        return render_template(
            "customer_forgot_password.html",
            page_title="Reset Your Password",
            request_sent=True,
            rate_limited=False,
        )

    @app.get("/customer/reset-password")
    def customer_reset_password():
        token = request.args.get("token", "")
        token_record = load_valid_customer_auth_token(token, "password_reset")
        return render_template(
            "customer_reset_password.html",
            page_title="Choose a New Password",
            token=token,
            token_valid=token_record is not None,
        )

    @app.post("/customer/reset-password")
    def customer_reset_password_post():
        try:
            if request.form.get("password") != request.form.get("password_confirm"):
                raise ValueError("Enter the same new password twice.")
            customer_user = reset_customer_password(
                request.form.get("token"),
                request.form.get("password"),
            )
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("customer_forgot_password"))
        session.clear()
        record_audit_event(
            "customer.password_reset_completed",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            account_number=choose_customer_account_number(str(customer_user["email"])),
            target_type="customer_user",
            target_id=customer_user["id"],
        )
        flash("Your password has been changed. Sign in with the new password.")
        return redirect(url_for("customer_login"))

    @app.get("/customer/login")
    def customer_login():
        if current_customer_user() is not None and current_staff_user() is None:
            return redirect(url_for("customer_dashboard"))
        return render_template("customer_login.html", page_title="Customer Sign In", next_url=request.args.get("next", ""))

    @app.post("/customer/login")
    def customer_login_post():
        email = request.form.get("email", "")
        identity_hash = auth_request_identity("customer_login", email)
        limit_status = auth_rate_limit_status("customer_login", identity_hash)
        if limit_status["blocked"]:
            flash(auth_limit_message(int(limit_status["retry_after"])))
            return (
                render_template(
                    "customer_login.html",
                    page_title="Customer Sign In",
                    next_url=request.form.get("next", ""),
                ),
                429,
            )
        try:
            customer_user = authenticate_customer_user(
                email,
                request.form.get("password", ""),
            )
        except EmailVerificationRequired as exc:
            clear_auth_failures("customer_login", identity_hash)
            session.clear()
            session.permanent = True
            session["pending_verification_email"] = str(exc.customer_user["email"])
            record_audit_event(
                "customer.login_verification_required",
                actor_type="customer",
                actor_id=int(exc.customer_user["id"]),
                target_type="customer_user",
                target_id=exc.customer_user["id"],
            )
            return redirect(url_for("customer_verification_sent"))
        except Exception as exc:
            failed_status = record_auth_failure("customer_login", identity_hash)
            record_audit_event(
                "customer.login_failed",
                actor_type="anonymous",
                metadata={"rate_limited": bool(failed_status["blocked"])},
            )
            flash(str(exc))
            if failed_status["blocked"]:
                flash(auth_limit_message(int(failed_status["retry_after"])))
                return (
                    render_template(
                        "customer_login.html",
                        page_title="Customer Sign In",
                        next_url=request.form.get("next", ""),
                    ),
                    429,
                )
            return redirect(url_for("customer_login", next=request.form.get("next", "")))

        clear_auth_failures("customer_login", identity_hash)
        start_customer_session(customer_user)
        record_audit_event(
            "customer.login_succeeded",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            account_number=choose_customer_account_number(str(customer_user["email"])),
            target_type="customer_user",
            target_id=customer_user["id"],
        )
        return redirect(next_destination(default_endpoint="customer_dashboard"))

    @app.get("/pricing")
    def pricing_page():
        customer_user = current_customer_user()
        billing = None if customer_user is None else load_customer_billing(int(customer_user["id"]))
        return render_template(
            "marketing_pricing.html",
            billing=billing,
            **build_marketing_page_context(
                page_title="Pricing",
                page_description="Choose the Home Energy Watch plan that fits one household, a small review desk, or a commission pilot.",
                active_page="pricing",
            ),
        )

    @app.get("/how-it-works")
    def how_it_works_page():
        return render_template(
            "marketing_how_it_works.html",
            **build_marketing_page_context(
                page_title="How It Works",
                page_description="See how Home Energy Watch turns utility exports into a clear overnight-load review you can use at home or in a commission follow-up.",
                active_page="how-it-works",
            ),
        )

    @app.get("/for-homeowners")
    def for_homeowners_page():
        return render_template(
            "marketing_homeowners.html",
            **build_marketing_page_context(
                page_title="For Homeowners",
                page_description="Use Home Energy Watch to compare your overnight baseline, flagged nights, and utility export history without giving up your own records.",
                active_page="for-homeowners",
            ),
        )

    @app.get("/for-commissions")
    def for_commissions_page():
        return render_template(
            "marketing_commissions.html",
            **build_marketing_page_context(
                page_title="For Commissions",
                page_description="Review export files, compare periods, and keep a sharper record when an overnight-load question needs a regulator follow-up.",
                active_page="for-commissions",
            ),
        )

    @app.get("/terms")
    def terms_page():
        return render_template(
            "marketing_terms.html",
            effective_date="July 21, 2026",
            **build_marketing_page_context(
                page_title="Terms",
                page_description="The terms for using Home Energy Watch to store and review electric usage history.",
                active_page="terms",
            ),
        )

    @app.get("/privacy")
    def privacy_page():
        return render_template(
            "marketing_privacy.html",
            effective_date="July 21, 2026",
            **build_marketing_page_context(
                page_title="Privacy Notice",
                page_description="How Home Energy Watch handles account, household, usage, and billing information.",
                active_page="privacy",
            ),
        )

    @app.get("/utility-data-authorization")
    def utility_data_authorization_page():
        return render_template(
            "marketing_data_authorization.html",
            effective_date="July 21, 2026",
            authorization_scope=UTILITY_AUTHORIZATION_SCOPE,
            **build_marketing_page_context(
                page_title="Utility Data Permission",
                page_description="What permission Home Energy Watch needs to import and analyze electric usage history.",
                active_page="utility-data-authorization",
            ),
        )

    @app.post("/billing/checkout")
    def billing_checkout():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        try:
            session_obj = create_customer_checkout_session(
                customer_user,
                request.form.get("plan_id"),
                build_public_base_url(request.url_root),
            )
        except Exception as exc:
            flash(str(exc))
            return redirect(url_for("pricing_page"))
        record_audit_event(
            "billing.checkout_started",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            account_number=choose_customer_account_number(str(customer_user["email"])),
            target_type="stripe_checkout_session",
            target_id=session_obj.get("id"),
            metadata={"plan_id": str(request.form.get("plan_id") or "")},
        )
        return redirect(str(session_obj["url"]))

    @app.get("/billing/success")
    def billing_success():
        customer_user = current_customer_user()
        if customer_user is None:
            return redirect(url_for("customer_login"))
        try:
            billing = refresh_customer_billing_from_stripe(customer_user, request.args.get("session_id"))
            record_audit_event(
                "billing.status_refreshed",
                actor_type="customer",
                actor_id=int(customer_user["id"]),
                account_number=choose_customer_account_number(str(customer_user["email"])),
                target_type="customer_billing",
                target_id=customer_user["id"],
                metadata={"status": str(billing.get("status") or "")},
            )
        except Exception:
            pass
        flash("Billing is being confirmed.")
        return redirect(url_for("customer_dashboard"))

    @app.get("/billing/cancel")
    def billing_cancel():
        flash("Billing was not changed.")
        if current_customer_user() is not None:
            return redirect(url_for("customer_dashboard"))
        return redirect(url_for("pricing_page"))

    @app.post("/billing/portal")
    def billing_portal():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        try:
            session_obj = create_customer_portal_session(customer_user, build_public_base_url(request.url_root))
        except Exception as exc:
            flash(str(exc))
            return redirect(url_for("customer_dashboard"))
        record_audit_event(
            "billing.portal_opened",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            account_number=choose_customer_account_number(str(customer_user["email"])),
            target_type="customer_billing",
            target_id=customer_user["id"],
        )
        return redirect(str(session_obj["url"]))

    @app.post("/stripe/webhook")
    def stripe_webhook():
        try:
            if stripe is None:
                return jsonify({"error": "Stripe is not available in this build."}), 400
            webhook_secret = get_stripe_webhook_secret()
            if not webhook_secret:
                return jsonify({"error": "Stripe webhook is not connected."}), 400
            payload = request.get_data()
            signature = request.headers.get("Stripe-Signature", "")
            event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
            handle_stripe_event(event)
            event_type = str(event.get("type") or "unknown")
            record_audit_event(
                "billing.webhook_processed",
                actor_type="system",
                target_type="stripe_event",
                target_id=event.get("id"),
                metadata={"event_type": event_type},
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"received": True})

    @app.get("/customer")
    def customer_dashboard():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        return render_customer_dashboard(customer_user)

    @app.get("/customer/account")
    def customer_account_page():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        return render_customer_setup_page(customer_user, "Account", "account")

    @app.get("/customer/data-export.zip")
    def customer_data_export():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        archive, account_count = build_customer_data_archive(customer_user)
        record_audit_event(
            "customer.data_exported",
            actor_type="customer",
            actor_id=int(customer_user["id"]),
            target_type="customer_data",
            target_id=customer_user["id"],
            metadata={"account_count": account_count},
        )
        filename = f"home-energy-watch-data-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        response = app.response_class(archive, mimetype="application/zip")
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/customer/utility")
    def customer_utility_page():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        return render_customer_setup_page(customer_user, "Utility", "utility")

    @app.get("/customer/inventory")
    def customer_inventory_page():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        return render_customer_setup_page(customer_user, "Inventory", "inventory")

    @app.get("/customer/history")
    def customer_history_page():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        context = build_customer_account_context(customer_user, "History")
        if context is None:
            return render_template("customer_empty.html", page_title="Your energy history", customer_mode=True)
        return render_template("history_page.html", **context)

    @app.get("/customer/billing")
    def customer_billing_page():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        context = build_customer_account_context(customer_user, "Billing")
        if context is None:
            return render_template(
                "billing_page.html",
                billing=load_customer_billing(int(customer_user["id"])),
                customer_mode=True,
                page_title="Billing",
            )
        return render_template("billing_page.html", **context)

    @app.get("/login")
    def login():
        if staff_bootstrap_needed():
            return redirect(url_for("first_run"))
        if current_staff_user() is not None:
            return redirect(url_for("index"))
        return render_template("login.html", page_title="Commission Sign In", next_url=request.args.get("next", ""))

    @app.get("/staff/forgot-password")
    def staff_forgot_password():
        if staff_bootstrap_needed():
            return redirect(url_for("first_run"))
        return render_template(
            "staff_forgot_password.html",
            page_title="Reset Commission Password",
            request_sent=False,
        )

    @app.post("/staff/forgot-password")
    def staff_forgot_password_post():
        if staff_bootstrap_needed():
            return redirect(url_for("first_run"))
        email = request.form.get("email", "")
        identity_hash = auth_request_identity("staff_password_reset", email)
        limit_status = auth_rate_limit_status("staff_password_reset", identity_hash)
        if limit_status["blocked"]:
            return (
                render_template(
                    "staff_forgot_password.html",
                    page_title="Reset Commission Password",
                    request_sent=True,
                    rate_limited=True,
                ),
                429,
            )
        record_auth_failure("staff_password_reset", identity_hash)
        staff_user = get_staff_user_by_email(email)
        delivered = False
        if staff_user is not None and staff_user.get("is_active") and not staff_user.get("invite_pending"):
            try:
                send_staff_password_reset_email(staff_user, build_public_base_url(request.url_root))
                delivered = True
            except Exception:
                delivered = False
        record_audit_event(
            "staff.password_reset_requested",
            actor_type="staff" if staff_user is not None else "anonymous",
            actor_id=None if staff_user is None else int(staff_user["id"]),
            target_type="staff_user" if staff_user is not None else None,
            target_id=None if staff_user is None else staff_user["id"],
            metadata={"delivery_attempted": delivered},
        )
        return render_template(
            "staff_forgot_password.html",
            page_title="Reset Commission Password",
            request_sent=True,
            rate_limited=False,
        )

    @app.get("/staff/reset-password")
    def staff_reset_password():
        token = request.args.get("token", "")
        token_record = load_valid_staff_auth_token(token, "password_reset")
        return render_template(
            "staff_reset_password.html",
            page_title="Choose a Commission Password",
            token=token,
            token_valid=token_record is not None,
        )

    @app.post("/staff/reset-password")
    def staff_reset_password_post():
        token = request.form.get("token", "")
        try:
            if request.form.get("password") != request.form.get("password_confirm"):
                raise ValueError("Enter the same new password twice.")
            staff_user = reset_staff_password(
                token,
                request.form.get("password"),
            )
        except ValueError as exc:
            flash(str(exc))
            token_record = load_valid_staff_auth_token(token, "password_reset")
            return render_template(
                "staff_reset_password.html",
                page_title="Choose a Commission Password",
                token=token,
                token_valid=token_record is not None,
            ), 400
        session.clear()
        record_audit_event(
            "staff.password_reset_completed",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user["id"],
        )
        flash("Your password has been changed. Sign in with the new password.")
        return redirect(url_for("login"))

    @app.post("/login")
    def login_post():
        if staff_bootstrap_needed():
            return redirect(url_for("first_run"))
        email = request.form.get("email", "")
        identity_hash = auth_request_identity("staff_login", email)
        limit_status = auth_rate_limit_status("staff_login", identity_hash)
        if limit_status["blocked"]:
            flash(auth_limit_message(int(limit_status["retry_after"])))
            return (
                render_template(
                    "login.html",
                    page_title="Commission Sign In",
                    next_url=request.form.get("next", ""),
                ),
                429,
            )
        try:
            staff_user = authenticate_staff_user(
                email,
                request.form.get("password", ""),
            )
        except Exception as exc:
            failed_status = record_auth_failure("staff_login", identity_hash)
            record_audit_event(
                "staff.login_failed",
                actor_type="anonymous",
                metadata={"rate_limited": bool(failed_status["blocked"])},
            )
            flash(str(exc))
            if failed_status["blocked"]:
                flash(auth_limit_message(int(failed_status["retry_after"])))
                return (
                    render_template(
                        "login.html",
                        page_title="Commission Sign In",
                        next_url=request.form.get("next", ""),
                    ),
                    429,
                )
            return redirect(url_for("login", next=request.form.get("next", "")))

        clear_auth_failures("staff_login", identity_hash)
        destination = next_destination()
        if staff_user.get("mfa_enabled"):
            establish_pending_staff_mfa_session(staff_user, destination)
            record_audit_event(
                "staff.password_verified",
                actor_type="staff",
                actor_id=int(staff_user["id"]),
                target_type="staff_user",
                target_id=staff_user["id"],
            )
            return redirect(url_for("staff_mfa_challenge"))

        establish_staff_session(staff_user)
        mark_staff_login_succeeded(int(staff_user["id"]))
        record_audit_event(
            "staff.login_succeeded",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user["id"],
            metadata={"mfa": "not_enabled"},
        )
        return redirect(destination)

    @app.get("/staff/mfa/challenge")
    def staff_mfa_challenge():
        staff_user = pending_staff_mfa_user()
        if staff_user is None:
            flash("Sign in again to continue.")
            return redirect(url_for("login"))
        return render_template(
            "staff_mfa_challenge.html",
            page_title="Verify Commission Sign In",
            staff_email=mask_email_address(str(staff_user["email"])),
        )

    @app.post("/staff/mfa/challenge")
    def staff_mfa_challenge_post():
        staff_user = pending_staff_mfa_user()
        if staff_user is None:
            flash("Sign in again to continue.")
            return redirect(url_for("login"))
        identity_hash = auth_request_identity("staff_mfa", str(staff_user["email"]))
        limit_status = auth_rate_limit_status("staff_mfa", identity_hash)
        if limit_status["blocked"]:
            flash(auth_limit_message(int(limit_status["retry_after"])))
            return (
                render_template(
                    "staff_mfa_challenge.html",
                    page_title="Verify Commission Sign In",
                    staff_email=mask_email_address(str(staff_user["email"])),
                ),
                429,
            )
        method = verify_staff_mfa_code(int(staff_user["id"]), request.form.get("code"))
        if method is None:
            failed_status = record_auth_failure("staff_mfa", identity_hash)
            record_audit_event(
                "staff.mfa_failed",
                actor_type="staff",
                actor_id=int(staff_user["id"]),
                target_type="staff_user",
                target_id=staff_user["id"],
                metadata={"rate_limited": bool(failed_status["blocked"])},
            )
            flash("That authenticator or recovery code did not work.")
            status_code = 429 if failed_status["blocked"] else 400
            if failed_status["blocked"]:
                flash(auth_limit_message(int(failed_status["retry_after"])))
            return (
                render_template(
                    "staff_mfa_challenge.html",
                    page_title="Verify Commission Sign In",
                    staff_email=mask_email_address(str(staff_user["email"])),
                ),
                status_code,
            )
        clear_auth_failures("staff_mfa", identity_hash)
        destination = str(session.get("pending_staff_next") or url_for("index"))
        establish_staff_session(staff_user)
        mark_staff_login_succeeded(int(staff_user["id"]))
        record_audit_event(
            "staff.login_succeeded",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user["id"],
            metadata={"mfa": method},
        )
        return redirect(destination)

    @app.get("/staff/security")
    def staff_security():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        return render_template(
            "staff_security.html",
            page_title="Sign-in Security",
            enrollment=load_staff_mfa_enrollment(int(staff_user["id"])),
            recovery_code_count=count_staff_mfa_recovery_codes(int(staff_user["id"])),
            mfa_required=staff_mfa_required(),
        )

    @app.post("/staff/security/mfa/start")
    def staff_mfa_start():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        try:
            begin_staff_mfa_enrollment(int(staff_user["id"]))
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("staff_security"))
        record_audit_event(
            "staff.mfa_enrollment_started",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user["id"],
        )
        return redirect(url_for("staff_security"))

    @app.post("/staff/security/mfa/confirm")
    def staff_mfa_confirm():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        try:
            result = confirm_staff_mfa_enrollment(
                int(staff_user["id"]),
                request.form.get("code"),
            )
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("staff_security"))
        updated_user = result["staff_user"]
        session["staff_auth_version"] = int(updated_user.get("auth_version") or 1)
        record_audit_event(
            "staff.mfa_enabled",
            actor_type="staff",
            actor_id=int(updated_user["id"]),
            target_type="staff_user",
            target_id=updated_user["id"],
        )
        return render_template(
            "staff_mfa_recovery_codes.html",
            page_title="Recovery Codes",
            recovery_codes=result["recovery_codes"],
        )

    @app.post("/staff/security/mfa/cancel")
    def staff_mfa_cancel():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        cancel_staff_mfa_enrollment(int(staff_user["id"]))
        record_audit_event(
            "staff.mfa_enrollment_cancelled",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user["id"],
        )
        flash("Authenticator setup was cancelled.")
        return redirect(url_for("staff_security"))

    @app.post("/staff/security/mfa/recovery-codes")
    def staff_mfa_recovery_codes():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        try:
            result = regenerate_staff_mfa_recovery_codes(
                int(staff_user["id"]),
                request.form.get("code"),
            )
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("staff_security"))
        updated_user = result["staff_user"]
        session["staff_auth_version"] = int(updated_user.get("auth_version") or 1)
        record_audit_event(
            "staff.mfa_recovery_codes_replaced",
            actor_type="staff",
            actor_id=int(updated_user["id"]),
            target_type="staff_user",
            target_id=updated_user["id"],
        )
        return render_template(
            "staff_mfa_recovery_codes.html",
            page_title="Recovery Codes",
            recovery_codes=result["recovery_codes"],
        )

    @app.post("/staff/security/mfa/disable")
    def staff_mfa_disable():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        if staff_mfa_required():
            flash("Authenticator protection is required for commission access.")
            return redirect(url_for("staff_security"))
        try:
            updated_user = disable_staff_mfa(
                int(staff_user["id"]),
                request.form.get("password"),
                request.form.get("code"),
            )
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("staff_security"))
        session["staff_auth_version"] = int(updated_user.get("auth_version") or 1)
        record_audit_event(
            "staff.mfa_disabled",
            actor_type="staff",
            actor_id=int(updated_user["id"]),
            target_type="staff_user",
            target_id=updated_user["id"],
        )
        flash("Authenticator protection was turned off.")
        return redirect(url_for("staff_security"))

    @app.post("/logout")
    def logout():
        customer_user = current_customer_user()
        staff_user = current_staff_user()
        had_customer = customer_user is not None and staff_user is None
        if customer_user is not None:
            record_audit_event(
                "customer.logout",
                actor_type="customer",
                actor_id=int(customer_user["id"]),
                account_number=choose_customer_account_number(str(customer_user["email"])),
                target_type="customer_user",
                target_id=customer_user["id"],
            )
        elif staff_user is not None:
            record_audit_event(
                "staff.logout",
                actor_type="staff",
                actor_id=int(staff_user["id"]),
                target_type="staff_user",
                target_id=staff_user["id"],
            )
        session.clear()
        return redirect(url_for("customer_login" if had_customer else "login"))

    @app.get("/staff/setup/<token>")
    def accept_staff_invite_route(token: str):
        invited_user = load_invited_staff_user(token)
        if invited_user is None:
            flash("That setup link is no longer available.")
            return redirect(url_for("login"))
        return render_template(
            "staff_setup.html",
            page_title="Finish Sign In",
            invited_user=invited_user,
            invite_token=token,
        )

    @app.post("/staff/setup/<token>")
    def accept_staff_invite_post(token: str):
        try:
            staff_user = accept_staff_invite(
                token,
                request.form.get("password", ""),
                full_name=request.form.get("full_name"),
            )
        except Exception as exc:
            flash(str(exc))
            return redirect(url_for("accept_staff_invite_route", token=token))

        session.clear()
        session.permanent = True
        session["staff_user_id"] = int(staff_user["id"])
        session["staff_auth_version"] = int(staff_user.get("auth_version") or 1)
        record_audit_event(
            "staff.invite_accepted",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user["id"],
        )
        return redirect(url_for("index"))

    @app.post("/staff/invite")
    def invite_staff():
        staff_user = require_commissioner()
        if not isinstance(staff_user, dict):
            return staff_user
        try:
            invite = invite_staff_user(
                request.form.get("email", ""),
                request.form.get("full_name", ""),
                request.form.get("role", "Analyst"),
                invited_by_id=int(staff_user["id"]),
            )
        except Exception as exc:
            flash(str(exc))
            return redirect(url_for("index"))

        delivery = "manual"
        if email_delivery_backend() == "disabled":
            session["latest_invite_token"] = invite["token"]
        else:
            try:
                send_staff_invitation_email(invite, build_public_base_url(request.url_root))
                delivery = "email"
            except Exception:
                delivery = "failed"
        record_audit_event(
            "staff.invited",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=invite["email"],
            metadata={"role": str(invite["role"]), "delivery": delivery},
        )
        if delivery == "email":
            flash(f"An invitation was sent to {invite['email']}.")
        elif delivery == "failed":
            flash("The invitation was saved, but the email could not be sent. Try inviting that address again.")
        else:
            flash(f"{invite['full_name']} is ready for setup.")
        return_to = (request.form.get("return_to") or "").strip()
        if return_to.startswith("/") and not return_to.startswith("//"):
            return redirect(return_to)
        return redirect(url_for("staff_page"))

    @app.post("/staff/<int:staff_user_id>/access")
    def update_staff_access(staff_user_id: int):
        staff_user = require_commissioner()
        if not isinstance(staff_user, dict):
            return staff_user
        try:
            change = update_staff_user_access(
                staff_user_id,
                role=request.form.get("role", ""),
                is_active=request.form.get("status") == "active",
                acting_staff_user_id=int(staff_user["id"]),
            )
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("staff_page"))
        before = change["before"]
        after = change["after"]
        record_audit_event(
            "staff.access_updated",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user_id,
            metadata={
                "previous_role": before["role"],
                "role": after["role"],
                "previous_status": "active" if before["is_active"] else "inactive",
                "status": "active" if after["is_active"] else "inactive",
            },
        )
        flash(f"Access for {after['full_name']} was updated.")
        return redirect(url_for("staff_page"))

    @app.get("/staff/<int:staff_user_id>/mfa/reset")
    def staff_mfa_reset(staff_user_id: int):
        staff_user = require_commissioner()
        if not isinstance(staff_user, dict):
            return staff_user
        if int(staff_user_id) == int(staff_user["id"]):
            flash("Another commissioner must reset your authenticator.")
            return redirect(url_for("staff_page"))
        target_user = get_staff_user_by_id(staff_user_id)
        if target_user is None:
            flash("That staff member is no longer available.")
            return redirect(url_for("staff_page"))
        if not target_user.get("mfa_enabled"):
            flash("Authenticator protection is already off for that staff member.")
            return redirect(url_for("staff_page"))
        return render_template(
            "staff_mfa_reset.html",
            page_title="Reset Authenticator",
            target_user=target_user,
        )

    @app.post("/staff/<int:staff_user_id>/mfa/reset")
    def staff_mfa_reset_post(staff_user_id: int):
        staff_user = require_commissioner()
        if not isinstance(staff_user, dict):
            return staff_user
        try:
            change = reset_staff_mfa_by_commissioner(
                staff_user_id,
                acting_staff_user_id=int(staff_user["id"]),
            )
        except ValueError as exc:
            flash(str(exc))
            return redirect(url_for("staff_page"))
        target_user = change["after"]
        record_audit_event(
            "staff.mfa_reset_by_commissioner",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="staff_user",
            target_id=staff_user_id,
            metadata={"target_role": str(target_user["role"])},
        )
        flash(f"Authenticator protection for {target_user['full_name']} was reset. They must set it up again.")
        return redirect(url_for("staff_page"))

    @app.get("/")
    def index():
        if is_marketing_host(current_request_host()):
            return render_template(
                "marketing_home.html",
                **build_marketing_page_context(
                    page_title="Home Energy Watch",
                    page_description="Read your utility export with a cleaner eye, compare suspicious overnight load, and keep one record for homeowners and commissions.",
                    active_page="home",
                ),
            )
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        ensure_data_dirs()
        defaults = build_settings_defaults()
        account_number = request.args.get("account_number")
        account_search = request.args.get("account_search")
        account_page_number = parse_positive_int(request.args.get("account_page"), default=1)
        scaffold = build_account_scaffold(account_number, account_search, account_page_number)
        latest_analysis = None
        try:
            _, latest_analysis = build_account_view(scaffold["account"]["account_number"], defaults)
        except Exception:
            latest_analysis = None

        return render_template(
            "index.html",
            defaults=defaults,
            analysis=latest_analysis,
            account=scaffold["account"],
            accounts=scaffold["accounts"],
            account_page=scaffold["account_page"],
            household_profile=scaffold["household_profile"],
            load_items=scaffold["load_items"],
            load_summary=scaffold["load_summary"],
            account_access=scaffold["account_access"],
            utility_connections=scaffold["utility_connections"],
            data_authorizations=scaffold["data_authorizations"],
            has_active_data_authorization=scaffold["has_active_data_authorization"],
            staff_team=list_staff_users(),
            latest_invite_url=consume_latest_invite_url(),
            active_account_number=scaffold["account"]["account_number"],
            page_title="Commission Review",
        )

    @app.get("/account")
    def account_page():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        return render_template("setup_section.html", **build_staff_account_context("Customer", "account"))

    @app.get("/people")
    def people_page():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        return render_template("setup_section.html", **build_staff_account_context("People", "people"))

    @app.get("/utility")
    def utility_page():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        return render_template("setup_section.html", **build_staff_account_context("Utility", "utility"))

    @app.get("/inventory")
    def inventory_page():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        return render_template("setup_section.html", **build_staff_account_context("Inventory", "inventory"))

    @app.get("/history")
    def history_page():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        return render_template("history_page.html", **build_staff_account_context("History"))

    @app.get("/staff")
    def staff_page():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        return render_template(
            "staff_page.html",
            staff_team=list_staff_users(),
            latest_invite_url=consume_latest_invite_url(),
            page_title="Staff",
        )

    @app.get("/audit")
    def audit_page():
        staff_user = require_commissioner()
        if not isinstance(staff_user, dict):
            return staff_user
        return render_template(
            "audit_page.html",
            audit_page=list_audit_events(
                account_number=request.args.get("account_number"),
                action=request.args.get("action"),
                page=parse_positive_int(request.args.get("page"), default=1),
            ),
            audit_integrity=verify_audit_chain(),
            page_title="Audit Record",
        )

    @app.get("/audit/export.csv")
    def audit_export():
        staff_user = require_commissioner()
        if not isinstance(staff_user, dict):
            return staff_user
        integrity = verify_audit_chain()
        if not integrity["valid"]:
            return app.response_class(
                "The audit record did not pass its integrity check.\n",
                status=409,
                mimetype="text/plain",
            )
        account_number = request.args.get("account_number")
        action = request.args.get("action")
        csv_body = build_audit_export_csv(account_number=account_number, action=action)
        record_audit_event(
            "audit.exported",
            actor_type="staff",
            actor_id=int(staff_user["id"]),
            target_type="audit_record",
            metadata={
                "account_number": normalize_account_number(account_number) if account_number else "",
                "action": (action or "").strip(),
            },
        )
        filename = f"home-energy-watch-audit-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        response = app.response_class(csv_body, mimetype="text/csv")
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        ensure_database()
        return jsonify({"status": "ok"})

    @app.get("/robots.txt")
    def robots_txt():
        if is_marketing_host(current_request_host()):
            sitemap_url = build_absolute_url(build_marketing_base_url(request.url_root), "/sitemap.xml")
            return app.response_class(
                f"User-agent: *\nAllow: /\nSitemap: {sitemap_url}\n",
                mimetype="text/plain",
            )

        return app.response_class("User-agent: *\nDisallow: /\n", mimetype="text/plain")

    @app.get("/sitemap.xml")
    def sitemap_xml():
        marketing_base_url = build_marketing_base_url(request.url_root)
        urls = [
            build_absolute_url(marketing_base_url, "/"),
            build_absolute_url(marketing_base_url, "/pricing"),
            build_absolute_url(marketing_base_url, "/how-it-works"),
            build_absolute_url(marketing_base_url, "/for-homeowners"),
            build_absolute_url(marketing_base_url, "/for-commissions"),
            build_absolute_url(marketing_base_url, "/terms"),
            build_absolute_url(marketing_base_url, "/privacy"),
            build_absolute_url(marketing_base_url, "/utility-data-authorization"),
        ]
        xml_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        xml_lines.extend(f"  <url><loc>{url}</loc></url>" for url in urls)
        xml_lines.append("</urlset>")
        return app.response_class("\n".join(xml_lines) + "\n", mimetype="application/xml")

    @app.get("/reports/<path:filename>")
    def download_report(filename: str):
        staff_user = current_staff_user()
        if staff_user is not None:
            return send_from_directory(OUTPUT_DIR, normalize_report_filename(filename), as_attachment=True)

        customer_user = current_customer_user()
        if customer_user is None:
            return redirect(url_for("customer_login", next=request.path))
        try:
            artifact_account_number = get_report_artifact_account_number(filename)
        except ValueError:
            artifact_account_number = None
        if artifact_account_number is None or not customer_has_account_access(
            str(customer_user["email"]),
            artifact_account_number,
        ):
            return "Report not found.", 404
        return send_from_directory(OUTPUT_DIR, normalize_report_filename(filename), as_attachment=True)

    @app.post("/compare")
    def compare_exports():
        ensure_data_dirs()
        settings = parse_settings(request.form)
        account_number = request.form.get("account_number")
        actor = require_account_actor(account_number, write=True)
        if not isinstance(actor, dict):
            return actor

        try:
            if not account_has_active_data_authorization(account_number):
                raise ValueError("Customer data permission is required before exports can be compared.")
            left_path = save_uploaded_file(request.files.get("left_file"))
            right_path = save_uploaded_file(request.files.get("right_file"))
            comparison, report_path = analyze_interval_file_comparison(
                left_input_path=left_path,
                right_input_path=right_path,
                tz_name=settings["tz"],
                night_start_str=settings["night_start"],
                night_end_str=settings["night_end"],
                min_night_kw=settings["min_night_kw"],
                night_multiplier=settings["night_multiplier"],
            )
            csv_report_path = build_web_comparison_csv_path(report_path)
            save_comparison_artifact(csv_report_path, comparison)
            register_report_artifacts(account_number, [report_path, csv_report_path])
            record_actor_event(
                actor,
                "history.compared",
                account_number=account_number,
                target_type="comparison_report",
                target_id=report_path.name,
            )
        except Exception as exc:
            flash(str(exc))
            endpoint = "customer_history_page" if actor["kind"] == "customer" else "history_page"
            return redirect(url_for(endpoint, account_number=normalize_account_number(account_number)))

        return render_template(
            "comparison_report.html",
            comparison_packet=build_comparison_packet_context(comparison, report_path, csv_report_path),
            account=load_account(account_number),
            active_account_number=normalize_account_number(account_number),
            customer_mode=actor["kind"] == "customer",
            page_title="Comparison packet",
        )

    @app.get("/api/files")
    def api_files():
        staff_user = require_staff_user(api=True)
        if not isinstance(staff_user, dict):
            return staff_user
        return jsonify({"input_files": list_input_files(), "report_files": list_report_files()})

    @app.get("/api/supported-feeds")
    def api_supported_feeds():
        return jsonify({"supported_feeds": list_supported_utility_adapters()})

    @app.get("/api/utility-by-zip")
    def utility_lookup_api():
        try:
            match = lookup_energy_company_by_zip(
                request.args.get("zip_code", ""),
                request.args.get("address"),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception:
            return jsonify({"error": "The electric company lookup is unavailable right now. Please try again."}), 503
        return jsonify(match)

    @app.get("/api/day-detail")
    def api_day_detail():
        actor = require_account_actor(request.args.get("account_number"), api=True)
        if not isinstance(actor, dict):
            return actor
        settings = parse_settings(request.args)
        account = load_account(request.args.get("account_number"))
        load_items = list_load_items(account["account_number"])
        load_summary = build_load_inventory_summary(load_items)
        df, summary, baseline, alert_events = analyze_history_store(
            account_number=account["account_number"],
            tz_name=settings["tz"],
            night_start_str=settings["night_start"],
            night_end_str=settings["night_end"],
            min_night_kw=settings["min_night_kw"],
            night_multiplier=settings["night_multiplier"],
            baseline_date=account.get("baseline_date"),
        )
        detail = build_day_detail(
            df,
            summary,
            alert_events,
            request.args.get("date"),
            baseline_date=account.get("baseline_date"),
        )
        if detail is None:
            return jsonify({"error": "That day is not available for this account."}), 404

        detail["load_summary"] = load_summary
        detail["inventory_alignment"] = {
            "off_gap_kw": None
            if detail["current_day"]["night_avg_kw"] is None
            else round(float(detail["current_day"]["night_avg_kw"]) - float(load_summary["off_kw"]), 3),
            "all_on_gap_kw": round(float(detail["current_day"]["max_kw"]) - float(load_summary["all_on_kw"]), 3),
        }
        detail["baseline_kw"] = None if baseline is None else round(float(baseline), 3)
        detail["weather"] = load_day_weather(account["account_number"], detail["date"], settings["tz"])
        return jsonify(detail)

    @app.post("/api/analyze")
    def api_analyze():
        ensure_data_dirs()
        payload = {}
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            settings = parse_settings(payload)
            account_number = payload.get("account_number")
            display_name = payload.get("display_name")
            energy_company = payload.get("energy_company")
            baseline_date = payload.get("baseline_date")
        else:
            settings = parse_settings(request.form)
            account_number = request.form.get("account_number")
            display_name = request.form.get("display_name")
            energy_company = request.form.get("energy_company")
            baseline_date = request.form.get("baseline_date")

        actor = require_account_actor(account_number, api=True, write=True)
        if not isinstance(actor, dict):
            return actor
        if not account_has_active_data_authorization(account_number):
            return jsonify({"error": "Customer data permission is required before new history can be added."}), 403
        if not request.is_json:
            input_path = save_uploaded_file(request.files.get("xml_file"))

        if request.is_json:
            mounted_name = payload.get("input_file")
            if mounted_name:
                input_path = resolve_input_file(mounted_name)
                import_interval_file_to_db(
                    input_path,
                    account_number=account_number,
                    display_name=display_name,
                    energy_company=energy_company,
                    baseline_date=baseline_date,
                )
        else:
            import_interval_file_to_db(
                input_path,
                account_number=account_number,
                display_name=display_name,
                energy_company=energy_company,
                baseline_date=baseline_date,
            )

        account = save_account_profile(
            account_number,
            display_name=display_name,
            energy_company=energy_company,
            baseline_date=baseline_date,
        )
        profile_source = payload if request.is_json else request.form
        if has_household_profile_fields(profile_source):
            save_household_profile(account["account_number"], profile_source)
        df, summary, baseline, alert_events = analyze_history_store(
            account_number=account["account_number"],
            tz_name=settings["tz"],
            night_start_str=settings["night_start"],
            night_end_str=settings["night_end"],
            min_night_kw=settings["min_night_kw"],
            night_multiplier=settings["night_multiplier"],
            baseline_date=account.get("baseline_date"),
        )
        report_path = build_output_path(Path("combined-history.xml"))
        weather_contexts = load_weather_contexts_for_suspicious_days(
            summary,
            account["account_number"],
            settings["tz"],
        )
        report_summary = attach_weather_context_to_summary(summary, weather_contexts)
        report_summary.to_csv(report_path, index=True)
        visible_accounts = list_accounts()
        if actor["kind"] == "customer":
            visible_accounts = list_customer_account_page(str(actor["user"]["email"]))["accounts"]
        analysis = build_report_context(
            "Customer history",
            df,
            report_summary,
            alert_events,
            baseline,
            report_path,
            settings,
            account=account,
            accounts=visible_accounts,
            household_profile=load_household_profile(account["account_number"]),
            load_items=list_load_items(account["account_number"]),
            imported_files_count=count_imported_files(account["account_number"]),
            weather_contexts=weather_contexts,
        )
        json_report_path = save_json_report(report_path, analysis)
        register_report_artifacts(account["account_number"], [report_path, json_report_path])
        record_actor_event(
            actor,
            "history.imported_and_analyzed",
            account_number=account["account_number"],
            target_type="imported_history",
            target_id=report_path.name,
            metadata={"interval_rows": int(len(df)), "flagged_days": int(report_summary["suspicious"].sum())},
        )
        return jsonify(analysis)

    @app.post("/account")
    def save_account():
        account_number = request.form.get("account_number")
        actor = require_account_actor(account_number, write=True)
        if not isinstance(actor, dict):
            return actor
        try:
            existing_account = find_account(account_number)
            existing_profile = None if existing_account is None else load_household_profile(account_number)
            energy_company = resolve_energy_company_for_form(
                request.form,
                existing_account=existing_account,
                existing_profile=existing_profile,
                require_zip=existing_account is None,
            )
            account = save_account_profile(
                account_number,
                display_name=request.form.get("display_name"),
                energy_company=energy_company,
                baseline_date=request.form.get("baseline_date"),
            )
            save_household_profile(account["account_number"], request.form)
            record_actor_event(
                actor,
                "account.profile_updated",
                account_number=account["account_number"],
                target_type="account",
                target_id=account["account_number"],
                metadata={"energy_company": str(account.get("energy_company") or "")},
            )
        except Exception as exc:
            flash(str(exc))
            return redirect_back_or_account(request.form.get("account_number"))

        return redirect_back_or_account(account["account_number"])

    @app.post("/account-access")
    def create_account_access():
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        account_number = request.form.get("account_number")
        try:
            access = add_account_access_email(
                account_number,
                request.form.get("email", ""),
                full_name=request.form.get("full_name"),
                access_level=request.form.get("access_level", "Viewer"),
            )
            record_actor_event(
                {"kind": "staff", "user": staff_user},
                "account.access_granted",
                account_number=account_number,
                target_type="account_access",
                target_id=access["id"],
                metadata={"access_level": str(access["access_level"])},
            )
        except Exception as exc:
            flash(str(exc))
        return redirect_back_or_account(account_number)

    @app.post("/account-access/<int:access_id>/delete")
    def remove_account_access(access_id: int):
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        account_number = request.form.get("account_number")
        removal = delete_account_access_email(account_number, access_id)
        record_actor_event(
            {"kind": "staff", "user": staff_user},
            "account.access_revoked",
            account_number=account_number,
            target_type="account_access",
            target_id=access_id,
            metadata={
                "authorization_count": len(removal["authorization_ids"]),
                "credentials_cleared": bool(removal["credentials_cleared"]),
            },
        )
        if removal["authorization_ids"]:
            record_actor_event(
                {"kind": "staff", "user": staff_user},
                "utility.authorization_revoked",
                account_number=account_number,
                target_type="account_access",
                target_id=access_id,
                metadata={
                    "reason": "account_access_removed",
                    "authorization_ids": removal["authorization_ids"],
                },
            )
        return redirect_back_or_account(account_number)

    @app.post("/account/data-authorization")
    def grant_data_authorization():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        account_number = request.form.get("account_number")
        access = get_customer_account_access(str(customer_user["email"]), account_number)
        if access is None or access["access_level"] != "Manager":
            return "Manager access is required to change data permission for this account.", 403
        try:
            if not form_checkbox_checked(request.form.get("confirm_data_authorization")):
                raise ValueError("Confirm the data permission before authorizing access.")
            authorization = grant_account_data_authorization(
                account_number,
                int(customer_user["id"]),
                remote_hash=request_remote_hash(),
                user_agent_hash=request_user_agent_hash(),
            )
            record_audit_event(
                "utility.authorization_granted",
                actor_type="customer",
                actor_id=int(customer_user["id"]),
                account_number=account_number,
                target_type="account_data_authorization",
                target_id=authorization["id"],
                metadata={"authorization_version": str(authorization["authorization_version"])},
            )
            flash("Permission to use this account's utility data is active.")
        except Exception as exc:
            flash(str(exc))
        return redirect_back_or_account(account_number)

    @app.post("/account/data-authorization/revoke")
    def revoke_data_authorization():
        customer_user = require_customer_user()
        if not isinstance(customer_user, dict):
            return customer_user
        account_number = request.form.get("account_number")
        access = get_customer_account_access(str(customer_user["email"]), account_number)
        if access is None or access["access_level"] != "Manager":
            return "Manager access is required to change data permission for this account.", 403
        try:
            authorization = revoke_account_data_authorization(
                account_number,
                int(customer_user["id"]),
            )
            record_audit_event(
                "utility.authorization_revoked",
                actor_type="customer",
                actor_id=int(customer_user["id"]),
                account_number=account_number,
                target_type="account_data_authorization",
                target_id=authorization["id"],
                metadata={"authorization_version": str(authorization["authorization_version"])},
            )
            flash("Permission was withdrawn. Saved utility access details were removed.")
        except Exception as exc:
            flash(str(exc))
        return redirect_back_or_account(account_number)

    @app.post("/utility-connection")
    def save_connection():
        account_number = request.form.get("account_number")
        actor = require_account_actor(account_number, write=True)
        if not isinstance(actor, dict):
            return actor
        try:
            account = find_account(account_number)
            if account is None or not account.get("energy_company"):
                raise ValueError("Add the service ZIP code before saving a utility connection.")
            if not account_has_active_data_authorization(account_number):
                raise ValueError("Customer authorization is required before saving a utility connection.")
            connection_form = request.form.to_dict(flat=True)
            connection_form["provider_name"] = str(account["energy_company"])
            connection = save_utility_connection(account_number, connection_form)
            record_actor_event(
                actor,
                "utility.connection_saved",
                account_number=account_number,
                target_type="utility_connection",
                target_id=connection["id"],
                metadata={"access_method": str(connection["access_method"])},
            )
        except Exception as exc:
            flash(str(exc))
        return redirect_back_or_account(account_number)

    @app.post("/utility-connection/<int:connection_id>/delete")
    def remove_connection(connection_id: int):
        account_number = request.form.get("account_number")
        actor = require_account_actor(account_number, write=True)
        if not isinstance(actor, dict):
            return actor
        delete_utility_connection(account_number, connection_id)
        record_actor_event(
            actor,
            "utility.connection_deleted",
            account_number=account_number,
            target_type="utility_connection",
            target_id=connection_id,
        )
        return redirect_back_or_account(account_number)

    @app.post("/utility-connection/<int:connection_id>/sync")
    def sync_connection(connection_id: int):
        account_number = request.form.get("account_number")
        actor = require_account_actor(account_number, write=True)
        if not isinstance(actor, dict):
            return actor
        try:
            sync_utility_connection(account_number, connection_id)
            record_actor_event(
                actor,
                "utility.connection_synced",
                account_number=account_number,
                target_type="utility_connection",
                target_id=connection_id,
            )
            flash("Utility history synced.")
        except Exception as exc:
            try:
                record_utility_connection_sync_failure(account_number, connection_id, exc)
            except Exception:
                pass
            record_actor_event(
                actor,
                "utility.connection_sync_failed",
                account_number=account_number,
                target_type="utility_connection",
                target_id=connection_id,
                metadata={"error_type": type(exc).__name__},
            )
            flash(str(exc))
        return redirect_back_or_account(account_number)

    @app.post("/load-items")
    def create_load_item():
        account_number = request.form.get("account_number")
        actor = require_account_actor(account_number, write=True)
        if not isinstance(actor, dict):
            return actor
        try:
            item = add_load_item(
                account_number=account_number,
                label=request.form.get("label", ""),
                quantity=int(request.form.get("quantity", "1")),
                watts_each=float(request.form.get("watts_each", "0")),
                include_when_off=bool(request.form.get("include_when_off")),
                notes=request.form.get("notes"),
            )
            record_actor_event(
                actor,
                "inventory.item_saved",
                account_number=account_number,
                target_type="load_item",
                target_id=item["id"],
                metadata={"quantity": int(item["quantity"]), "watts_each": float(item["watts_each"])},
            )
        except Exception as exc:
            flash(str(exc))
        return redirect_back_or_account(account_number)

    @app.post("/load-items/<int:item_id>/delete")
    def remove_load_item(item_id: int):
        account_number = request.form.get("account_number")
        actor = require_account_actor(account_number, write=True)
        if not isinstance(actor, dict):
            return actor
        delete_load_item(account_number, item_id)
        record_actor_event(
            actor,
            "inventory.item_deleted",
            account_number=account_number,
            target_type="load_item",
            target_id=item_id,
        )
        return redirect_back_or_account(account_number)

    @app.get("/analyze")
    def analyze_page():
        account_number = request.args.get("account_number")
        if current_customer_user() is not None and current_staff_user() is None:
            return redirect(url_for("customer_dashboard", account_number=normalize_account_number(account_number)))
        staff_user = require_staff_user()
        if not isinstance(staff_user, dict):
            return staff_user
        return redirect(url_for("index", account_number=normalize_account_number(account_number)))

    @app.post("/analyze")
    def analyze():
        ensure_data_dirs()
        settings = parse_settings(request.form)
        account_number = request.form.get("account_number")
        display_name = request.form.get("display_name")
        energy_company = request.form.get("energy_company")
        baseline_date = request.form.get("baseline_date")
        actor = require_account_actor(account_number, write=True)
        if not isinstance(actor, dict):
            return actor

        try:
            if not account_has_active_data_authorization(account_number):
                raise ValueError("Customer data permission is required before new history can be added.")
            input_path = save_uploaded_file(request.files.get("xml_file"))
            import_interval_file_to_db(
                input_path,
                account_number=account_number,
                display_name=display_name,
                energy_company=energy_company,
                baseline_date=baseline_date,
            )
            account = save_account_profile(
                account_number,
                display_name=display_name,
                energy_company=energy_company,
                baseline_date=baseline_date,
            )
            df, summary, baseline, alert_events = analyze_history_store(
                account_number=account["account_number"],
                tz_name=settings["tz"],
                night_start_str=settings["night_start"],
                night_end_str=settings["night_end"],
                min_night_kw=settings["min_night_kw"],
                night_multiplier=settings["night_multiplier"],
                baseline_date=account.get("baseline_date"),
            )
            report_path = build_output_path(Path("combined-history.xml"))
            weather_contexts = load_weather_contexts_for_suspicious_days(
                summary,
                account["account_number"],
                settings["tz"],
            )
            report_summary = attach_weather_context_to_summary(summary, weather_contexts)
            report_summary.to_csv(report_path, index=True)
        except Exception as exc:
            flash(str(exc))
            return redirect(url_for("index", account_number=normalize_account_number(account_number)))

        analysis = build_report_context(
            "Customer history",
            df,
            report_summary,
            alert_events,
            baseline,
            report_path,
            settings,
            account=account,
            accounts=list_accounts(),
            household_profile=load_household_profile(account["account_number"]),
            load_items=list_load_items(account["account_number"]),
            imported_files_count=count_imported_files(account["account_number"]),
            weather_contexts=weather_contexts,
        )
        account_page = list_account_page()
        rendered_accounts = list_accounts()
        if actor["kind"] == "customer":
            account_page = list_customer_account_page(str(actor["user"]["email"]))
            rendered_accounts = account_page["accounts"]
            analysis["accounts"] = rendered_accounts
        json_report_path = save_json_report(report_path, analysis)
        register_report_artifacts(account["account_number"], [report_path, json_report_path])
        record_actor_event(
            actor,
            "history.imported_and_analyzed",
            account_number=account["account_number"],
            target_type="imported_history",
            target_id=report_path.name,
            metadata={"interval_rows": int(len(df)), "flagged_days": int(report_summary["suspicious"].sum())},
        )
        return render_template(
            "report.html",
            analysis=analysis,
            defaults=settings,
            account=account,
            accounts=rendered_accounts,
            account_page=account_page,
            household_profile=load_household_profile(account["account_number"]),
            load_items=list_load_items(account["account_number"]),
            load_summary=build_load_inventory_summary(list_load_items(account["account_number"])),
            account_access=list_account_access_emails(account["account_number"]),
            utility_connections=list_utility_connections(account["account_number"]),
            data_authorizations=list_account_data_authorizations(account["account_number"]),
            has_active_data_authorization=account_has_active_data_authorization(account["account_number"]),
            staff_team=[] if actor["kind"] == "customer" else list_staff_users(),
            latest_invite_url=None if actor["kind"] == "customer" else consume_latest_invite_url(),
            customer_mode=actor["kind"] == "customer",
            page_title="Your energy history" if actor["kind"] == "customer" else "Commission Review",
        )

    return app


web_app = create_web_app()


def print_scheduled_utility_sync_report(summary: dict[str, object]) -> None:
    total = int(summary["total"])
    succeeded = int(summary["succeeded"])
    failed = int(summary["failed"])
    print(f"Utility sync: {succeeded}/{total} succeeded, {failed} failed")
    for connection in summary["connections"]:
        account_number = connection["account_number"]
        label = f"{account_number} {connection['provider_name']} - {connection['connection_label']}"
        if connection["success"]:
            interval_count = int(connection.get("interval_count") or 0)
            print(f"[ok] {label}: imported {interval_count} intervals")
        else:
            print(f"[failed] {label}: {connection['error']}")


def main() -> None:
    parser = build_cli_parser()
    args = parser.parse_args()

    if args.serve:
        ensure_data_dirs()
        web_app.run(host=args.host, port=args.port, debug=False)
        return

    if args.sync_utilities:
        try:
            sync_summary = run_scheduled_utility_sync(account_number=args.account_number)
        except Exception as exc:
            print(f"Error syncing utilities: {exc}", file=sys.stderr)
            sys.exit(1)
        print_scheduled_utility_sync_report(sync_summary)
        if sync_summary["failed"]:
            sys.exit(1)
        return

    if not args.input:
        parser.error("--input is required unless --serve or --sync-utilities is used")

    try:
        if args.compare_to:
            output_path = None if args.output == DEFAULT_CLI_OUTPUT else args.output
            comparison, report_path = analyze_interval_file_comparison(
                left_input_path=args.input,
                right_input_path=args.compare_to,
                output_path=output_path,
                tz_name=args.tz,
                night_start_str=args.night_start,
                night_end_str=args.night_end,
                min_night_kw=args.min_night_kw,
                night_multiplier=args.night_multiplier,
            )
            print_comparison_report(comparison, report_path)
            return

        summary, baseline, report_path = analyze_interval_file(
            input_path=args.input,
            output_path=args.output,
            tz_name=args.tz,
            night_start_str=args.night_start,
            night_end_str=args.night_end,
            min_night_kw=args.min_night_kw,
            night_multiplier=args.night_multiplier,
            account_number=args.account_number,
        )
    except Exception as exc:
        print(f"Error analyzing XML: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Daily summary saved to: {report_path}")
    print_human_report(summary, baseline)


if __name__ == "__main__":
    main()
