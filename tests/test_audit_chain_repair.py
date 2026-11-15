"""The audit chain: diagnosis, the detached table, and restoring removed rows."""
import json
import shutil

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
    for i in range(3):
        app.record_audit_event("auth.login_link_requested", actor_type="customer",
                               actor_id=int(app.get_customer_user_by_email("bot@example.com")["id"]),
                               account_number="bot-1", metadata={"n": i})
        app.record_audit_event("auth.login_link_requested", actor_type="customer", account_number="real-1")
    assert app.verify_audit_chain()["valid"]
    yield tmp_path
    restart()


def bot_audit_ids():
    with app.get_db_connection() as conn:
        bot = app.find_account("bot-1")["id"]
        return [r["id"] for r in conn.execute("SELECT id FROM audit_events WHERE account_id = ? ORDER BY id", (bot,))]


def old_buggy_purge():
    """What the earlier purge did: delete the account's audit rows outright."""
    ids = bot_audit_ids()
    with app.get_db_connection() as conn:
        conn.execute(f"DELETE FROM audit_events WHERE id IN ({', '.join('?' for _ in ids)})", tuple(ids))
        conn.commit()
    return ids


def test_deleting_audit_rows_breaks_startup_and_report_names_missing_predecessor(db):
    ids = old_buggy_purge()
    restart()
    with pytest.raises(RuntimeError, match="integrity check"):
        app.get_db_connection()
    report = audit_repair.chain_report(app)  # Works while startup is failing.
    assert report["chain_valid"] is False
    assert report["diagnosis"] == "missing_predecessor"
    assert report["failure_event_id"] == ids[0] + 1
    assert report["ids_missing_immediately_before_failure"] == [str(ids[0])]
    assert report["failing_event_own_hash_recomputes"] is True
    assert report["rows_whose_own_hash_fails_count"] == 0
    assert report["missing_count"] == len(ids)


def test_restore_from_backup_copy_repairs_chain_without_changing_any_row(db, monkeypatch):
    backup = db / "backup.db"
    shutil.copy(app.DB_PATH, backup)
    ids = old_buggy_purge()
    with app.get_db_connection(ensure_schema=False) as conn:
        survivors_before = [dict(r) for r in conn.execute("SELECT * FROM audit_events ORDER BY id")]

    live = app.DB_PATH
    monkeypatch.setattr(app, "DB_PATH", backup)  # Read the originals from the copy.
    restart()
    original = audit_repair.export_rows(app, ids)
    monkeypatch.setattr(app, "DB_PATH", live)
    restart()
    assert [r["id"] for r in original] == ids

    dry = audit_repair.restore_detached(app, original)
    assert dry == {"rows": len(ids), "ids": audit_repair._compress(ids), "chain_valid_before": False,
                   "chain_valid_after": True, "failure_event_id_after": None, "confirmed": False}
    with app.get_db_connection(ensure_schema=False) as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_events_detached").fetchone()["n"] == 0

    audit_repair.restore_detached(app, original, confirm=True)
    restart()
    with app.get_db_connection() as conn:  # Startup passes again.
        survivors_after = [dict(r) for r in conn.execute("SELECT * FROM audit_events ORDER BY id")]
        restored = [dict(r) for r in conn.execute("SELECT * FROM audit_events_detached ORDER BY id")]
    assert survivors_after == survivors_before
    assert [{k: r[k] for k in app.AUDIT_EVENT_COLUMNS} for r in restored] == original
    assert app.verify_audit_chain()["valid"]
    # New events chain on correctly afterwards.
    app.record_audit_event("system.check")
    assert app.verify_audit_chain()["valid"]


def test_restore_refuses_altered_unbridging_or_duplicate_rows(db, monkeypatch):
    backup = db / "backup.db"
    shutil.copy(app.DB_PATH, backup)
    ids = old_buggy_purge()
    live = app.DB_PATH
    monkeypatch.setattr(app, "DB_PATH", backup)
    original = audit_repair.export_rows(app, ids)
    monkeypatch.setattr(app, "DB_PATH", live)

    altered = [dict(r) for r in original]
    altered[0]["metadata_json"] = '{"n":99}'
    with pytest.raises(ValueError, match="does not match its own hash"):
        audit_repair.restore_detached(app, altered, confirm=True)
    with pytest.raises(ValueError, match="still does not verify"):
        audit_repair.restore_detached(app, original[:-1], confirm=True)
    audit_repair.restore_detached(app, original, confirm=True)
    with pytest.raises(ValueError, match="already exist"):
        audit_repair.restore_detached(app, original, confirm=True)


def test_report_distinguishes_altered_record_and_changed_key(db, monkeypatch):
    with app.get_db_connection() as conn:
        conn.execute("UPDATE audit_events SET metadata_json = '{\"n\":42}' WHERE id = 3")
        conn.commit()
    report = audit_repair.chain_report(app)
    assert report["diagnosis"] == "record_altered" and report["failure_event_id"] == 3
    with app.get_db_connection() as conn:
        conn.execute("UPDATE audit_events SET metadata_json = '{\"n\":1}' WHERE id = 3")
        conn.commit()
    assert audit_repair.chain_report(app)["diagnosis"] == "intact"
    monkeypatch.setenv("POWER_AUDIT_SIGNING_KEY", "a-different-key-entirely-000000")
    assert audit_repair.chain_report(app)["diagnosis"] == "signing_key_changed"


def test_account_removal_now_keeps_audit_history_and_chain(db):
    ids = bot_audit_ids()
    summary = account_cleanup.purge_accounts(app, ["bot-1"], confirm=True)
    assert summary["audit_events_detached"] == len(ids)
    restart()
    with app.get_db_connection() as conn:  # Startup still passes.
        moved = [r["id"] for r in conn.execute("SELECT id FROM audit_events_detached ORDER BY id")]
    assert moved == ids
    assert app.verify_audit_chain()["valid"]
    app.record_audit_event("system.after_purge")
    assert app.verify_audit_chain()["valid"]


def test_cli_report_and_export(db, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["app.py", "--audit-chain-report"])
    app.main()
    assert json.loads(capsys.readouterr().out)["diagnosis"] == "intact"
    monkeypatch.setattr("sys.argv", ["app.py", "--audit-export-ids", "1-2"])
    app.main()
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["id"] for r in lines] == [1, 2]
