import pytest

import account_cleanup
import app
from test_app import FIXTURE, authorize_account, configure_tmp_paths


@pytest.fixture
def db(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)


def fake_account(number, email, address="33 Test Lane, Candler NC 28715"):
    authorize_account(number, email)
    app.save_household_profile(number, {"address": address, "zip_code": "28715"})


def count(table, where="1=1", params=()):
    with app.get_db_connection() as conn:
        return conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params).fetchone()["n"]


def test_list_shows_what_is_needed_to_spot_fakes(db):
    fake_account("fake-1", "tester@example.com")
    rows = {row["account_number"]: row for row in account_cleanup.list_accounts_for_review(app)}
    assert rows["fake-1"]["address"].startswith("33 Test Lane")
    assert rows["fake-1"]["emails"] == ["tester@example.com"]
    assert "primary" in rows


def test_dry_run_deletes_nothing(db):
    fake_account("fake-1", "tester@example.com")
    summary = account_cleanup.purge_accounts(app, ["fake-1"])
    assert summary["confirmed"] is False and summary["customers_removed"] == ["tester@example.com"]
    assert app.find_account("fake-1") is not None
    assert app.get_customer_user_by_email("tester@example.com") is not None


def test_purge_removes_account_data_files_and_orphan_sign_in(db):
    fake_account("fake-1", "tester@example.com")
    app.import_interval_file_to_db(FIXTURE, account_number="fake-1")
    fake_account("real-1", "owner@example.com", address="1 Real St")
    customer = app.get_customer_user_by_email("tester@example.com")

    summary = account_cleanup.purge_accounts(app, ["fake-1"], confirm=True)

    assert summary["customers_removed"] == ["tester@example.com"]
    assert app.find_account("fake-1") is None
    assert app.get_customer_user_by_email("tester@example.com") is None
    assert count("interval_readings") == 0
    assert count("household_profiles", "address LIKE ?", ("33 Test%",)) == 0
    assert count("account_data_authorizations", "customer_user_id = ?", (customer["id"],)) == 0
    assert count("audit_events", "actor_type = 'customer' AND actor_id = ?", (customer["id"],)) == 0
    # The real account is untouched.
    assert app.find_account("real-1")["address"] == "1 Real St"
    assert app.get_customer_user_by_email("owner@example.com") is not None


def test_sign_in_with_another_account_is_kept(db):
    fake_account("fake-1", "owner@example.com")
    fake_account("real-1", "owner@example.com", address="1 Real St")
    summary = account_cleanup.purge_accounts(app, ["fake-1"], confirm=True)
    assert summary["customers_removed"] == []
    assert app.get_customer_user_by_email("owner@example.com") is not None


def test_primary_placeholder_is_emptied_not_removed(db):
    app.save_household_profile("primary", {"address": "33 Test Lane, Candler NC 28715", "zip_code": "28715"})
    account_cleanup.purge_accounts(app, ["primary"], confirm=True)
    assert app.find_account("primary") is not None
    assert not app.find_account("primary")["address"]


def test_paid_unknown_and_held_accounts_are_refused(db):
    fake_account("paid-1", "payer@example.com")
    customer = app.get_customer_user_by_email("payer@example.com")
    app.record_customer_plan_selection(int(customer["id"]), "home", account_id=int(app.find_account("paid-1")["id"]))
    with app.get_db_connection() as conn:
        conn.execute("UPDATE customer_billing SET subscription_status = 'active'")
        conn.commit()
    with pytest.raises(ValueError, match="Paid subscription"):
        account_cleanup.purge_accounts(app, ["paid-1"], confirm=True)
    with pytest.raises(ValueError, match="No such account"):
        account_cleanup.purge_accounts(app, ["nope"], confirm=True)
    assert app.find_account("paid-1") is not None


def test_purge_all_except_keeps_named_and_real_looking_accounts(db):
    fake_account("910046647361", "owner@example.com", address="33 Real Lane")
    fake_account("bot-1", "bot1@example.com", address="Svtpe")
    fake_account("bot-2", "bot2@example.com", address="Anpvyww")
    fake_account("has-data", "maybe@example.com", address="9 Data St")
    app.import_interval_file_to_db(FIXTURE, account_number="has-data")
    app.save_household_profile("primary", {"address": "33 Real Lane", "zip_code": "28715"})

    dry = account_cleanup.purge_all_except(app, ["910046647361"])
    assert sorted(dry["accounts"]) == ["bot-1", "bot-2", "primary"]
    assert dry["skipped_for_review"] == [{"account_number": "has-data", "reasons": ["has meter readings"]}]
    assert app.find_account("bot-1") is not None

    account_cleanup.purge_all_except(app, ["910046647361"], confirm=True)
    assert app.find_account("bot-1") is None and app.find_account("bot-2") is None
    assert app.get_customer_user_by_email("bot1@example.com") is None
    assert app.find_account("910046647361")["address"] == "33 Real Lane"
    assert app.find_account("has-data") is not None
    assert not app.find_account("primary")["address"]


def test_purge_all_except_requires_a_real_keep(db):
    with pytest.raises(ValueError, match="not found"):
        account_cleanup.purge_all_except(app, ["typo"], confirm=True)


# --- Commissioner buttons -----------------------------------------------------

from test_app import customer_sign_in, sign_in  # noqa: E402


@pytest.fixture
def commissioner(db):
    app.web_app.config["TESTING"] = True
    client = app.web_app.test_client()
    sign_in(client)
    return client


def manage(client, action, numbers, **extra):
    return client.post("/accounts/manage", data={"action": action, "account_numbers": numbers,
                                                  "return_to": "/account", **extra})


def test_account_page_shows_buttons_to_commissioner(commissioner):
    fake_account("bot-1", "bot1@example.com")
    page = commissioner.get("/account?account_number=bot-1").data
    assert b"Disable account" in page and b"Delete account" in page
    assert b'name="account_numbers" value="bot-1" form="account-bulk-actions"' in page


def test_disable_blocks_customer_and_enable_restores(commissioner):
    fake_account("bot-1", "bot1@example.com")
    manage(commissioner, "disable", ["bot-1"])
    assert app.find_account("bot-1")["disabled_at"]
    assert b"Disabled" in commissioner.get("/account?account_number=bot-1").data

    customer = app.web_app.test_client()
    customer_sign_in(customer, "bot1@example.com")
    blocked = customer.get("/customer/account?account_number=bot-1")
    assert b"This account has been disabled" in blocked.data
    assert not app.customer_has_account_access("bot1@example.com", "bot-1")

    manage(commissioner, "enable", ["bot-1"])
    assert not app.find_account("bot-1")["disabled_at"]
    assert customer.get("/customer/account?account_number=bot-1").status_code == 200


def test_disabled_accounts_do_not_sync(db):
    fake_account("bot-1", "bot1@example.com")
    with app.get_db_connection() as conn:
        account_id = app.find_account("bot-1")["id"]
        conn.execute(
            "INSERT INTO utility_connections (account_id, provider_name, connection_label, access_method, status, created_at, updated_at) "
            "VALUES (?, 'Duke', 'Feed', 'manual_feed', 'Ready', 'now', 'now')", (account_id,))
        conn.commit()
    assert len(app.list_saved_utility_connections_for_sync()) == 1
    app.set_account_disabled("bot-1", True)
    assert app.list_saved_utility_connections_for_sync() == []


def test_bulk_delete_needs_typed_confirmation(commissioner):
    fake_account("bot-1", "bot1@example.com")
    fake_account("bot-2", "bot2@example.com")
    manage(commissioner, "delete", ["bot-1", "bot-2"])
    assert app.find_account("bot-1") is not None
    response = manage(commissioner, "delete", ["bot-1", "bot-2"], confirm_delete="DELETE")
    assert response.status_code == 302
    assert app.find_account("bot-1") is None and app.find_account("bot-2") is None
    assert app.get_customer_user_by_email("bot1@example.com") is None


def test_only_commissioners_can_manage(db):
    fake_account("bot-1", "bot1@example.com")
    app.web_app.config["TESTING"] = True
    customer = app.web_app.test_client()
    customer_sign_in(customer, "bot1@example.com")
    manage(customer, "delete", ["bot-1"], confirm_delete="DELETE")
    manage(customer, "disable", ["bot-1"])
    assert app.find_account("bot-1") is not None and not app.find_account("bot-1")["disabled_at"]
    anonymous = app.web_app.test_client()
    manage(anonymous, "delete", ["bot-1"], confirm_delete="DELETE")
    assert app.find_account("bot-1") is not None
