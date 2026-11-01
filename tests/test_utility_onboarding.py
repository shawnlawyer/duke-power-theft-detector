import json
from datetime import date, timedelta

import pytest

import app
import utility_onboarding as onboarding
from test_app import configure_tmp_paths, sign_in, customer_sign_in


@pytest.fixture
def client(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    sign_in(client)
    return client


def saved(utility_id="xcel"):
    with app.get_db_connection() as conn:
        return dict(conn.execute("SELECT * FROM utility_onboarding WHERE id = ?", (utility_id,)).fetchone())


def test_seed_idempotent_and_initial_status_truthful(client):
    with app.get_db_connection() as conn:
        onboarding.ensure_schema(conn)
        rows = conn.execute("SELECT * FROM utility_onboarding").fetchall()
        assert len(rows) == 72
        assert len(conn.execute("SELECT * FROM utility_onboarding_history").fetchall()) == 72
        assert sum(row["registration"] == "approved" for row in rows) == 1
        assert not any(row["connection"] == "verified" for row in rows)
        assert sum(row["country"] == "Canada" and row["registration"] == "unreviewed" for row in rows) == 42
    assert saved("national_grid")["connection"] == saved()["connection"] == "disabled"
    response = client.get("/staff/utilities")
    assert response.status_code == 200
    assert b"72" in response.data and b"not a complete global directory" in response.data
    assert b"No follow-up date" in response.data


@pytest.mark.parametrize("path", ["/staff/utilities", "/staff/utilities/xcel", "/staff/utilities/new"])
def test_anonymous_and_customers_cannot_read_or_write(client, path):
    anonymous = app.web_app.test_client()
    assert anonymous.get(path).status_code == 302
    assert "/login" in anonymous.get(path).location
    app.create_customer_user("owner@example.com", "Owner", "test-password-123")
    customer_sign_in(anonymous)
    assert anonymous.get(path).status_code == 302
    if path != "/staff/utilities":
        assert anonymous.post(path, data=saved()).status_code == 302
    assert saved()["revision"] == 1


@pytest.mark.parametrize("role", ["Analyst", "Investigator"])
def test_non_admin_staff_cannot_access(client, role):
    with app.get_db_connection() as conn:
        conn.execute("UPDATE staff_users SET role = ?", (role,))
    for path in ("/staff/utilities", "/staff/utilities/xcel", "/staff/utilities/new"):
        assert client.get(path).status_code == 403
        if path != "/staff/utilities":
            assert client.post(path, data=saved()).status_code == 403


def test_external_commission_cannot_access_even_with_commissioner_role(client, monkeypatch):
    monkeypatch.setattr(app, "get_staff_commission_access", lambda _: {"commission_id": "external"})
    for path in ("/staff/utilities", "/staff/utilities/xcel", "/staff/utilities/new"):
        assert client.get(path).status_code == 403
        if path != "/staff/utilities":
            assert client.post(path, data=saved()).status_code == 403


def test_edit_history_and_stale_conflict_preserve_changes(client, monkeypatch):
    original = saved()
    monkeypatch.setenv("POWER_XCEL_ENABLED", "false")
    monkeypatch.setenv("POWER_NATIONAL_GRID_ENABLED", "false")
    response = client.post("/staff/utilities/xcel", data={**original, "owner": "Shawn", "next_action": "Review the territory choices."})
    assert response.status_code == 302
    updated = saved()
    assert updated["revision"] == 2 and updated["owner"] == "Shawn"
    assert updated["connection"] == "disabled"
    assert client.post("/staff/utilities/xcel", data={**original, "owner": "Stale edit"}).status_code == 409
    assert saved()["owner"] == "Shawn"
    with app.get_db_connection() as conn:
        onboarding.ensure_schema(conn)
        history = conn.execute("SELECT * FROM utility_onboarding_history WHERE utility_id = ? ORDER BY revision", ("xcel",)).fetchall()
        assert len(history) == 2
        assert json.loads(history[1]["snapshot"])["owner"] == "Shawn"
    assert saved()["next_action"] == "Review the territory choices."
    assert b"Version 2" in client.get("/staff/utilities/xcel").data


@pytest.mark.parametrize("changes,expected", [
    ({"name": ""}, b"Name is required"),
    ({"next_action": ""}, b"Next action is required"),
    ({"owner": "x" * 121}, b"120 characters"),
    ({"registration": "invented"}, b"valid registration"),
    ({"evidence_url": "javascript:alert(1)"}, b"HTTPS"),
    ({"evidence_url": "https://user:secret@example.com"}, b"HTTPS"),
    ({"reviewed_on": "2099-01-01"}, b"not in the future"),
    ({"follow_up": "2026-02-31"}, b"valid follow up date"),
    ({"connection": "verified"}, b"successful authorized import"),
    ({"queue": "complete"}, b"Complete requires"),
    ({"queue": "blocked", "blocker": ""}, b"Describe what is blocking"),
    ({"registration": "approved", "evidence_note": ""}, b"approval evidence"),
    ({"cost": "paid"}, b"Paid routes must stay blocked"),
])
def test_invalid_changes_not_saved(client, changes, expected):
    response = client.post("/staff/utilities/xcel", data={**saved(), **changes})
    assert response.status_code == 400
    assert expected in response.data
    assert saved()["revision"] == 1


def test_csrf_enforced(client):
    app.web_app.config["CSRF_ENFORCE_TESTS"] = True
    try:
        assert client.post("/staff/utilities/xcel", data=saved()).status_code == 400
        client.get("/staff/utilities/xcel")
        with client.session_transaction() as session:
            token = session["_csrf_token"]
        assert client.post("/staff/utilities/xcel", data={**saved(), "_csrf_token": token}).status_code == 302
    finally:
        app.web_app.config["CSRF_ENFORCE_TESTS"] = False


def test_worldwide_add_filter_duplicate_and_safe_html(client):
    payload = {**onboarding.defaults(), "name": "Example <script>alert(1)</script> Utility",
               "country": "New Zealand", "territory": "Example region",
               "next_action": "Verify the program.", "owner": "Alex",
               "follow_up": (date.today() - timedelta(days=1)).isoformat()}
    response = client.post("/staff/utilities/new", data=payload)
    assert response.status_code == 302
    detail = client.get(response.location)
    assert b"&lt;script&gt;" in detail.data and b"<script>alert(1)</script>" not in detail.data
    assert client.post("/staff/utilities/new", data=payload).status_code == 409
    filtered = client.get("/staff/utilities?country=New+Zealand&q=Alex&due=1")
    assert b"Showing 1 of 73" in filtered.data
    assert b"Example region" in filtered.data
    assert b"Showing 0 of 73" in client.get("/staff/utilities?country=New+Zealand&registration=approved").data
    assert client.get("/staff/utilities/no-such-id").status_code == 404


def test_history_failure_rolls_back_update(client, monkeypatch):
    def fail(*args):
        raise RuntimeError("History unavailable")
    monkeypatch.setattr(onboarding, "add_history", fail)
    assert client.post("/staff/utilities/xcel", data={**saved(), "owner": "Should roll back"}).status_code == 500
    assert saved()["revision"] == 1 and not saved()["owner"]


def test_recorded_verification_requires_full_gates_but_never_activates(client):
    payload = {**saved(), "registration": "approved", "connection": "verified", "queue": "complete",
               "cost": "confirmed_free", "verified_on": date.today().isoformat(),
               "reviewed_on": date.today().isoformat(), "requirements": "", "blocker": "",
               "evidence_note": "Synthetic test only: authorized import and free route verified.",
               "next_action": "Monitor for changes."}
    assert client.post("/staff/utilities/xcel", data=payload).status_code == 302
    assert b"Showing 1 of 72" in client.get("/staff/utilities?connection=verified&queue=complete").data
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM utility_connections").fetchone()["n"] == 0
