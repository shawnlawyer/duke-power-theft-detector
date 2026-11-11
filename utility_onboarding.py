"""Internal utility onboarding register. Never controls live data collection."""

import json
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from flask import abort, flash, redirect, render_template, request, url_for


REGISTRATION = {
    "unreviewed": "Not yet reviewed", "unverified": "Prior registration unverified",
    "inquiry": "Inquiry sent", "preparing": "Preparing application",
    "submitted": "Application submitted", "approved": "Registration approved",
    "declined": "Registration declined",
}
CONNECTION = {
    "not_started": "Not built", "upload_only": "Customer-upload route",
    "disabled": "Implemented; collection disabled", "testing": "Testing",
    "verified": "Working connection verified",
}
COST = {
    "unverified": "Full cost not confirmed", "data_free": "Data free; other costs unconfirmed",
    "confirmed_free": "Complete route confirmed free", "paid": "Paid route; do not proceed",
}
QUEUE = {"active": "Ready for next action", "waiting": "Waiting for a response", "blocked": "Blocked", "complete": "Complete"}
CHOICES = {"registration": REGISTRATION, "connection": CONNECTION, "cost": COST, "queue": QUEUE}
TEXT_LIMITS = {
    "name": 180, "country": 80, "territory": 200, "owner": 120,
    "next_action": 2000, "requirements": 6000, "blocker": 2000,
    "evidence_note": 4000, "evidence_url": 1000,
}
DATES = ("follow_up", "reviewed_on", "verified_on")
FIELDS = tuple(TEXT_LIMITS) + tuple(CHOICES) + DATES
SEED_PATH = Path(__file__).with_name("utility_onboarding_seed.json")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def defaults():
    return {**{key: "" for key in FIELDS}, "registration": "unreviewed",
            "connection": "not_started", "cost": "unverified", "queue": "active"}


def ensure_schema(conn):
    # TEXT identifiers and explicit timestamps work with both supported databases.
    columns = ", ".join(f"{key} TEXT NOT NULL DEFAULT ''" for key in FIELDS)
    conn.execute(f"""CREATE TABLE IF NOT EXISTS utility_onboarding (
        id TEXT PRIMARY KEY, {columns}, revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS utility_onboarding_history (
        id TEXT PRIMARY KEY, utility_id TEXT NOT NULL, revision INTEGER NOT NULL,
        actor TEXT NOT NULL, changed_at TEXT NOT NULL, snapshot TEXT NOT NULL,
        UNIQUE (utility_id, revision))""")
    seeds = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    for seed in seeds:
        record = {**defaults(), **seed}
        stamp = "2026-11-01T00:00:00+00:00"
        values = [record[key] for key in FIELDS]
        cursor = conn.execute(
            f"INSERT INTO utility_onboarding (id, {', '.join(FIELDS)}, revision, updated_at, created_at) "
            f"VALUES ({', '.join(['?'] * (len(FIELDS) + 4))}) ON CONFLICT (id) DO NOTHING",
            [record["id"], *values, 1, stamp, stamp],
        )
        if cursor.rowcount:
            add_history(conn, record["id"], record, 1, "Project records imported", stamp)


def add_history(conn, utility_id, record, revision, actor, stamp):
    conn.execute(
        "INSERT INTO utility_onboarding_history (id, utility_id, revision, actor, changed_at, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utility_id, revision, actor, stamp, json.dumps({key: record[key] for key in FIELDS}, ensure_ascii=False)),
    )


def validate(form):
    record = defaults()
    errors = []
    for key, limit in TEXT_LIMITS.items():
        value = str(form.get(key, "")).strip()
        record[key] = value
        if len(value) > limit:
            errors.append(f"{key.replace('_', ' ').capitalize()} must be {limit} characters or fewer.")
    for key in ("name", "country", "next_action"):
        if not record[key]:
            errors.append(f"{key.replace('_', ' ').capitalize()} is required.")
    for key, choices in CHOICES.items():
        value = form.get(key, "")
        if value not in choices:
            errors.append(f"Choose a valid {key.replace('_', ' ')} status.")
        else:
            record[key] = value
    for key in DATES:
        value = str(form.get(key, "")).strip()
        record[key] = value
        try:
            if value and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or date.fromisoformat(value) is None):
                raise ValueError
            if value and key != "follow_up" and date.fromisoformat(value) > date.today():
                raise ValueError
        except ValueError:
            errors.append(f"Enter a valid {key.replace('_', ' ')} date{'' if key == 'follow_up' else ' that is not in the future'}.")
    if record["evidence_url"]:
        try:
            parsed = urlsplit(record["evidence_url"])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or any(ch.isspace() for ch in record["evidence_url"]):
                raise ValueError
            # URLs are links only: never fetched by this feature.
        except ValueError:
            errors.append("Use an HTTPS evidence link without embedded credentials.")
    if record["registration"] in ("submitted", "approved") and (not record["evidence_note"] or not record["reviewed_on"]):
        errors.append("Record the submission or approval evidence and a reviewed date.")
    if record["connection"] == "verified" and (
        record["registration"] != "approved" or record["cost"] != "confirmed_free"
        or not record["verified_on"] or not record["evidence_note"]
    ):
        errors.append("A verified connection needs registration approval, a confirmed free route, a verification date, and evidence of a successful authorized import.")
    if record["queue"] == "complete" and (record["connection"] != "verified" or record["blocker"] or record["requirements"]):
        errors.append("Complete requires a verified working connection with no outstanding requirements or blockers.")
    if record["queue"] == "blocked" and not record["blocker"]:
        errors.append("Describe what is blocking this utility.")
    if record["cost"] == "paid" and record["queue"] != "blocked":
        errors.append("Paid routes must stay blocked under the free-services policy.")
    return record, errors


def register_routes(app, host, require_staff):
    def authorized():
        staff = require_staff()
        if not isinstance(staff, dict):
            return staff
        if staff["role"] not in host.STAFF_ADMIN_ROLES or host.get_staff_commission_access(int(staff["id"])) is not None:
            abort(403)
        return staff

    def get_record(utility_id):
        with host.get_db_connection() as conn:
            row = conn.execute("SELECT * FROM utility_onboarding WHERE id = ?", (utility_id,)).fetchone()
        if row is None:
            abort(404)
        return dict(row)

    def render_detail(record, errors=(), status=200):
        history = []
        if record.get("id"):
            with host.get_db_connection() as conn:
                history = [dict(row) for row in conn.execute(
                    "SELECT * FROM utility_onboarding_history WHERE utility_id = ? ORDER BY revision DESC LIMIT 50", (record["id"],)
                ).fetchall()]
            for entry in history:
                entry["values"] = json.loads(entry["snapshot"])
        return render_template("utility_onboarding_detail.html", page_title=record.get("name") or "Add a utility",
                               record=record, errors=errors, history=history, choices=CHOICES,
                               text_limits=TEXT_LIMITS), status

    @app.route("/staff/utilities", methods=["GET"])
    def utility_onboarding_list():
        staff = authorized()
        if not isinstance(staff, dict):
            return staff
        with host.get_db_connection() as conn:
            records = [dict(row) for row in conn.execute("SELECT * FROM utility_onboarding ORDER BY LOWER(name), country, territory").fetchall()]
        today = date.today().isoformat()
        totals = {
            "tracked": len(records), "approved": sum(r["registration"] == "approved" for r in records),
            "working": sum(r["connection"] == "verified" for r in records),
            "blocked": sum(r["queue"] == "blocked" for r in records),
            "due": sum(bool(r["follow_up"]) and r["follow_up"] <= today and r["queue"] != "complete" for r in records),
        }
        countries = sorted({r["country"] for r in records})
        filters = {key: request.args.get(key, "").strip() for key in ("q", "country", *CHOICES, "due")}
        visible = []
        for record in records:
            record["due"] = bool(record["follow_up"]) and record["follow_up"] <= today and record["queue"] != "complete"
            if any(filters[key] and filters[key] != record[key] for key in ("country", *CHOICES)):
                continue
            if filters["due"] and not record["due"]:
                continue
            if filters["q"].casefold() not in " ".join(record[k] for k in ("name", "country", "territory", "owner", "next_action", "requirements", "blocker")).casefold():
                continue
            visible.append(record)
        return render_template("utility_onboarding.html", page_title="Utility onboarding", records=visible,
                               totals=totals, countries=countries, filters=filters, choices=CHOICES)

    @app.route("/staff/utilities/new", methods=["GET", "POST"])
    def utility_onboarding_new():
        staff = authorized()
        if not isinstance(staff, dict):
            return staff
        if request.method == "GET":
            return render_detail(defaults())
        record, errors = validate(request.form)
        if errors:
            return render_detail(record, errors, 400)
        utility_id, stamp = str(uuid.uuid4()), now()
        with host.get_db_connection() as conn:
            existing = conn.execute(
                "SELECT id FROM utility_onboarding WHERE LOWER(name) = LOWER(?) AND LOWER(country) = LOWER(?) AND LOWER(territory) = LOWER(?)",
                (record["name"], record["country"], record["territory"]),
            ).fetchone()
            if existing:
                return render_detail(record, ["This utility and territory already have a record. Open that record to update it."], 409)
            conn.execute(
                f"INSERT INTO utility_onboarding (id, {', '.join(FIELDS)}, revision, updated_at, created_at) VALUES ({', '.join(['?'] * (len(FIELDS) + 4))})",
                [utility_id, *[record[key] for key in FIELDS], 1, stamp, stamp],
            )
            add_history(conn, utility_id, record, 1, f"Staff #{staff['id']}", stamp)
        flash("Utility added. No application was sent and no connection settings changed.")
        return redirect(url_for("utility_onboarding_detail", utility_id=utility_id))

    @app.route("/staff/utilities/<utility_id>", methods=["GET", "POST"])
    def utility_onboarding_detail(utility_id):
        staff = authorized()
        if not isinstance(staff, dict):
            return staff
        original = get_record(utility_id)
        if request.method == "GET":
            return render_detail(original)
        record, errors = validate(request.form)
        try:
            revision = int(request.form.get("revision", ""))
        except ValueError:
            revision = -1
        # Never substitute a fresh revision for a stale form, including after validation errors.
        display = {**original, **record, "revision": revision}
        if revision != original["revision"]:
            return render_detail(display, ["This record changed while you were editing. Your entries are below; reload the saved record before applying them."], 409)
        if errors:
            return render_detail(display, errors, 400)
        stamp = now()
        with host.get_db_connection() as conn:
            updated = conn.execute(
                f"UPDATE utility_onboarding SET {', '.join(key + ' = ?' for key in FIELDS)}, revision = revision + 1, updated_at = ? WHERE id = ? AND revision = ?",
                [*[record[key] for key in FIELDS], stamp, utility_id, revision],
            )
            if updated.rowcount != 1:
                return render_detail(display, ["Another edit was saved first. Reload the saved record before applying your entries."], 409)
            add_history(conn, utility_id, record, revision + 1, f"Staff #{staff['id']}", stamp)
        flash("Status saved. No application was sent and no connection settings changed.")
        return redirect(url_for("utility_onboarding_detail", utility_id=utility_id))
