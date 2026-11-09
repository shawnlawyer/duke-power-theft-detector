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
