"""Owner, Admin and Commissioner are separate roles."""
import pytest

import app
from test_app import authorize_account, configure_tmp_paths, email_token_sign_in


@pytest.fixture
def team(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    app.web_app.config["TESTING"] = True
    owner = app.create_first_staff_user("owner@example.com", "Owner", "owner-password-123")
    members = {"owner": owner}
    for role in ("Admin", "Commissioner"):
        invite = app.invite_staff_user(f"{role.lower()}@example.com", role, role, invited_by_id=int(owner["id"]))
        members[role.lower()] = app.accept_staff_invite(invite["token"], f"{role.lower()}-password-123")
    authorize_account("bot-1", "bot1@example.com")
    authorize_account("bot-2", "bot2@example.com")
    return members


def client_for(email):
    client = app.web_app.test_client()
    email_token_sign_in(client, email)
    return client


def manage(client, action, numbers, **extra):
    return client.post("/accounts/manage", data={"action": action, "account_numbers": numbers, "return_to": "/account", **extra})


def test_first_staff_user_is_owner(team):
    assert team["owner"]["role"] == "Owner"


def test_owner_can_disable_admin_cannot(team):
    manage(client_for("admin@example.com"), "disable", ["bot-1"])
    assert not app.find_account("bot-1")["disabled_at"]
    manage(client_for("owner@example.com"), "disable", ["bot-1"])
    assert app.find_account("bot-1")["disabled_at"]


def test_owner_and_admin_can_delete(team):
    manage(client_for("admin@example.com"), "delete", ["bot-1"], confirm_delete="DELETE")
    manage(client_for("owner@example.com"), "delete", ["bot-2"], confirm_delete="DELETE")
    assert app.find_account("bot-1") is None and app.find_account("bot-2") is None


def test_commissioner_is_review_only(team):
    commissioner = client_for("commissioner@example.com")
    manage(commissioner, "delete", ["bot-1"], confirm_delete="DELETE")
    manage(commissioner, "disable", ["bot-1"])
    assert app.find_account("bot-1") is not None and not app.find_account("bot-1")["disabled_at"]
    page = commissioner.get("/account?account_number=bot-1")
    assert page.status_code == 200
    assert b"Delete account" not in page.data and b"Disable account" not in page.data
    edit = commissioner.post("/account", data={"account_number": "bot-1", "original_account_number": "bot-1",
                                               "address": "Changed", "zip_code": "28715"})
    assert edit.status_code == 403
    assert commissioner.get("/staff").status_code in (200, 302)
    assert commissioner.get("/audit").status_code == 302


def test_buttons_shown_by_role(team):
    owner_page = client_for("owner@example.com").get("/account?account_number=bot-1").data
    admin_page = client_for("admin@example.com").get("/account?account_number=bot-1").data
    assert b"Disable account" in owner_page and b"Delete account" in owner_page
    assert b"Disable account" not in admin_page and b"Delete account" in admin_page


def test_admin_cannot_touch_owner_role(team):
    admin = team["admin"]
    with pytest.raises(ValueError, match="Only the owner"):
        app.update_staff_user_access(int(team["owner"]["id"]), role="Admin", is_active=True, acting_staff_user_id=int(admin["id"]))
    with pytest.raises(ValueError, match="Only the owner"):
        app.update_staff_user_access(int(team["commissioner"]["id"]), role="Owner", is_active=True, acting_staff_user_id=int(admin["id"]))
    with pytest.raises(ValueError, match="Admin access"):
        app.update_staff_user_access(int(team["admin"]["id"]), role="Analyst", is_active=True, acting_staff_user_id=int(team["commissioner"]["id"]))


def test_owner_can_add_and_demote_another_owner(team):
    second = app.invite_staff_user("owner2@example.com", "Owner Two", "Owner", invited_by_id=int(team["owner"]["id"]))
    owner2 = app.accept_staff_invite(second["token"], "owner2-password-123")
    assert owner2["role"] == "Owner"
    app.update_staff_user_access(int(owner2["id"]), role="Admin", is_active=True, acting_staff_user_id=int(team["owner"]["id"]))
    assert app.get_staff_user_by_id(int(owner2["id"]))["role"] == "Admin"


def test_migration_splits_old_commissioners(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    with app.get_db_connection() as conn:
        for email in ("commission@example.gov", "shawnlawyer@gmail.com"):
            conn.execute(
                "INSERT INTO staff_users (email, full_name, role, is_active, auth_version, created_at, updated_at) "
                "VALUES (?, 'Old', 'Commissioner', 1, 1, 'now', 'now')", (email,))
        conn.execute("UPDATE staff_users SET role = 'Commissioner' WHERE role = 'Owner'")
        app.migrate_staff_roles(conn)
        conn.commit()
    roles = {row["email"]: row["role"] for row in app.list_staff_users()}
    assert roles["shawn@epistemiccognition.com"] == "Owner"
    assert roles["shawnlawyer@gmail.com"] == "Admin"
    assert roles["commission@example.gov"] == "Admin"
    # Running again changes nothing.
    with app.get_db_connection() as conn:
        app.migrate_staff_roles(conn)
        conn.commit()
    assert {row["email"]: row["role"] for row in app.list_staff_users()} == roles


def test_admin_cannot_invite_or_reinvite_owner(team):
    admin_id = int(team["admin"]["id"])
    with pytest.raises(ValueError, match="Only the owner"):
        app.invite_staff_user("new@example.com", "New", "Owner", invited_by_id=admin_id)
    with pytest.raises(ValueError, match="Only the owner"):
        app.invite_staff_user("owner@example.com", "Owner", "Analyst", invited_by_id=admin_id)
    page = client_for("admin@example.com").get("/staff").data
    assert b'<option value="Owner"' not in page
