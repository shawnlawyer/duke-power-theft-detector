"""Audit chain: diagnosis, and signed markers for deliberately removed events."""
import json

import pytest

import account_cleanup
import app
import audit_repair
from test_app import authorize_account, configure_tmp_paths


def restart():
    """Forget that the schema is ready, as a fresh process would."""
    app.SCHEMA_READY_TARGETS.clear()


@pytest.fixture
def db(tmp_path, monkeypatch):
    configure_tmp_paths(tmp_path, monkeypatch)
    authorize_account("real-1", "owner@example.com")
    authorize_account("bot-1", "bot@example.com")
    bot = int(app.get_customer_user_by_email("bot@example.com")["id"])
    for i in range(3):
        app.record_audit_event("auth.login_link_requested", actor_type="customer", actor_id=bot,
                               account_number="bot-1", metadata={"n": i})
        app.record_audit_event("auth.login_link_requested", actor_type="customer", account_number="real-1")
    assert app.verify_audit_chain()["valid"]
    yield tmp_path
    restart()


def bot_audit_ids():
    with app.get_db_connection() as conn:
        bot = app.find_account("bot-1")["id"]
        return [r["id"] for r in conn.execute("SELECT id FROM audit_events WHERE account_id = ? ORDER BY id", (bot,))]


def old_purge():
    """What production went through: the account's audit events deleted outright."""
    ids = bot_audit_ids()
    with app.get_db_connection() as conn:
        conn.execute(f"DELETE FROM audit_events WHERE id IN ({', '.join('?' for _ in ids)})", tuple(ids))
        conn.commit()
    return ids


def snapshot_events():
    with app.get_db_connection(ensure_schema=False) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM audit_events ORDER BY id")]


def test_removed_events_break_startup_and_report_names_missing_predecessor(db):
    ids = old_purge()
    restart()
    with pytest.raises(RuntimeError, match="integrity check"):
        app.get_db_connection()
    report = audit_repair.chain_report(app)  # Works while startup is failing.
    assert report["chain_valid"] is False
    assert report["diagnosis"] == "missing_predecessor"
    assert report["failing_event_own_hash_recomputes"] is True
    assert report["rows_whose_own_hash_fails_count"] == 0
    assert report["missing_count"] == len(ids)


def test_marking_removals_restores_startup_without_touching_events(db):
    ids = old_purge()
    before = snapshot_events()
    dry = audit_repair.mark_removals(app, "fake accounts removed")
    assert dry["chain_valid_before"] is False and dry["chain_valid_after"] is True
    assert dry["removed_events_marked"] == len(ids) and dry["confirmed"] is False
    restart()
    with pytest.raises(RuntimeError):
        app.get_db_connection()  # Dry run wrote nothing.

    audit_repair.mark_removals(app, "fake accounts removed", confirm=True)
    restart()
    app.get_db_connection().close()  # Startup passes.
    status = app.verify_audit_chain()
    assert status["valid"] and status["removed_events"] == len(ids)
    assert snapshot_events() == before  # No event rehashed, edited or added back.
    assert all(i not in {e["id"] for e in before} for i in ids)  # Removed events stay gone.
    app.record_audit_event("system.after_repair")
    assert app.verify_audit_chain()["valid"]


def test_markers_do_not_hide_any_other_change(db):
    old_purge()
    audit_repair.mark_removals(app, "fake accounts removed", confirm=True)
    with app.get_db_connection() as conn:
        conn.execute("UPDATE audit_events SET metadata_json = '{\"n\":42}' WHERE id = (SELECT MAX(id) FROM audit_events)")
        conn.commit()
    assert not app.verify_audit_chain()["valid"]


def test_forged_or_edited_marker_is_rejected(db):
    old_purge()
    audit_repair.mark_removals(app, "fake accounts removed", confirm=True)
    with app.get_db_connection() as conn:
        conn.execute("UPDATE audit_chain_removals SET reason = 'something else'")
        conn.commit()
    assert not app.verify_audit_chain()["valid"]


def test_marking_refuses_an_edited_event(db):
    old_purge()
    with app.get_db_connection() as conn:
        conn.execute("UPDATE audit_events SET metadata_json = '{\"n\":42}' WHERE id = (SELECT MAX(id) FROM audit_events)")
        conn.commit()
    with pytest.raises(ValueError, match="changed"):
        audit_repair.mark_removals(app, "fake accounts removed", confirm=True)
    with app.get_db_connection(ensure_schema=False) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_chain_removals").fetchone()["n"] == 0


def test_report_distinguishes_altered_record_and_changed_key(db, monkeypatch):
    with app.get_db_connection() as conn:
        conn.execute("UPDATE audit_events SET metadata_json = '{\"n\":42}' WHERE id = 3")
        conn.commit()
    assert audit_repair.chain_report(app)["diagnosis"] == "record_altered"
    with app.get_db_connection() as conn:
        conn.execute("UPDATE audit_events SET metadata_json = '{\"n\":1}' WHERE id = 3")
        conn.commit()
    assert audit_repair.chain_report(app)["diagnosis"] == "intact"
    monkeypatch.setenv("POWER_AUDIT_SIGNING_KEY", "a-different-key-entirely-000000")
    assert audit_repair.chain_report(app)["diagnosis"] == "signing_key_changed"


def test_account_removal_deletes_its_audit_events_and_keeps_chain_valid(db):
    ids = bot_audit_ids()
    summary = account_cleanup.purge_accounts(app, ["bot-1"], confirm=True)
    assert summary["audit_gaps_marked"] >= 1
    restart()
    app.get_db_connection().close()  # Startup still passes.
    remaining = {e["id"] for e in snapshot_events()}
    assert not set(ids) & remaining
    with app.get_db_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_events WHERE actor_type = 'customer' AND actor_id = ?",
                            (2,)).fetchone()["n"] == 0
    assert app.verify_audit_chain()["valid"]
    app.record_audit_event("system.after_purge")
    assert app.verify_audit_chain()["valid"]


def test_cli_report_and_mark(db, capsys, monkeypatch):
    old_purge()
    monkeypatch.setattr("sys.argv", ["app.py", "--audit-chain-report"])
    app.main()
    assert json.loads(capsys.readouterr().out)["diagnosis"] == "missing_predecessor"
    monkeypatch.setattr("sys.argv", ["app.py", "--audit-mark-removals", "fake accounts removed", "--confirm-audit-removals"])
    app.main()
    assert json.loads(capsys.readouterr().out)["chain_valid_after"] is True
    monkeypatch.setattr("sys.argv", ["app.py", "--audit-chain-report"])
    app.main()
    assert json.loads(capsys.readouterr().out)["diagnosis"] == "intact"
