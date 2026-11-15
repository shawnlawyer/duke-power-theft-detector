#!/usr/bin/env python3
"""Read-only audit-chain evidence for a running production container.

Works with any app image (it does not import app.py), so it can be piped into
the current container while startup is failing:

    docker exec -i home-energy-watch python - report < scripts/audit_chain_standalone.py
    docker exec -i home-energy-watch python - export 255-260 < scripts/audit_chain_standalone.py
    docker exec -i home-energy-watch python - restore /tmp/missing.jsonl [--confirm] < scripts/audit_chain_standalone.py

report and export are READ ONLY. restore is the one write: it puts original
rows (exported from a backup copy) into audit_events_detached, unchanged, the
same table the app uses for events of removed accounts. It refuses rows that do
not match their own hashes, never overwrites an id, and writes nothing unless
the whole chain then verifies. Dry run unless --confirm is given.

Uses POWER_DATABASE_URL (or DATABASE_URL) and POWER_AUDIT_SIGNING_KEY from the
container's environment. Output never includes the signing key or credentials.
"""
import hashlib
import hmac
import json
import os
import sys

import psycopg
from psycopg.rows import dict_row

COLUMNS = ("id", "occurred_at", "actor_type", "actor_id", "action", "account_id", "target_type",
           "target_id", "request_id", "remote_hash", "metadata_json", "previous_hash", "event_hash")


def event_hash(event, previous_hash, key):
    # Must match app.audit_event_hash exactly.
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
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def compress(ids):
    out, start, prev = [], None, None
    for v in sorted(ids):
        if start is None:
            start = prev = v
        elif v == prev + 1:
            prev = v
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = v
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return out


def connect(read_only=True):
    dsn = os.environ.get("POWER_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("POWER_DATABASE_URL is not set in this environment.")
    conn = psycopg.connect(dsn, row_factory=dict_row)
    if read_only:
        conn.execute("SET TRANSACTION READ ONLY")
    return conn


def chain_valid(rows, key):
    previous = ""
    for row in rows:
        if not row.get("event_hash") or not hmac.compare_digest(str(row.get("previous_hash") or ""), previous) \
                or not hmac.compare_digest(str(row["event_hash"]), event_hash(row, previous, key)):
            return False, int(row["id"])
        previous = str(row["event_hash"])
    return True, None


DETACHED_DDL = """CREATE TABLE IF NOT EXISTS audit_events_detached (
    id BIGINT PRIMARY KEY, occurred_at TEXT NOT NULL, actor_type TEXT NOT NULL, actor_id BIGINT,
    action TEXT NOT NULL, account_id BIGINT, target_type TEXT, target_id TEXT, request_id TEXT NOT NULL,
    remote_hash TEXT, metadata_json TEXT NOT NULL DEFAULT '{}', previous_hash TEXT, event_hash TEXT NOT NULL,
    detached_at TEXT NOT NULL, detached_reason TEXT NOT NULL)"""  # Same as app.ensure_audit_detached_table.


def restore(path, confirm):
    key = os.environ.get("POWER_AUDIT_SIGNING_KEY", "").strip()
    if not key:
        sys.exit("POWER_AUDIT_SIGNING_KEY is not set in this environment.")
    with open(path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    if not rows or any(set(r) != set(COLUMNS) for r in rows):
        sys.exit("Each line must hold exactly the audit event columns.")
    for r in rows:
        if not hmac.compare_digest(str(r.get("event_hash") or ""), event_hash(r, str(r.get("previous_hash") or ""), key)):
            sys.exit(f"Refused: row {r['id']} does not match its own hash; it is not an original record.")
    with connect(read_only=not confirm) as conn:
        if confirm:
            conn.execute("LOCK TABLE audit_events IN SHARE ROW EXCLUSIVE MODE")
            conn.execute(DETACHED_DDL)
        current, _ = load(conn)
        present = {int(r["id"]) for r in current}
        clash = [int(r["id"]) for r in rows if int(r["id"]) in present]
        if clash:
            sys.exit("Refused: these ids already exist and are never overwritten: " + ", ".join(compress(clash)))
        before = chain_valid(current, key)
        after = chain_valid(sorted(current + rows, key=lambda r: int(r["id"])), key)
        result = {"rows": len(rows), "ids": compress(int(r["id"]) for r in rows), "chain_valid_before": before[0],
                  "chain_valid_after": after[0], "failure_event_id_after": after[1], "confirmed": confirm}
        if not after[0]:
            conn.rollback()
            print(json.dumps(result))
            sys.exit("Refused: the chain still does not verify with these rows. Nothing was written.")
        if confirm:
            stamp = __import__("datetime").datetime.now().isoformat(timespec="seconds")
            for r in sorted(rows, key=lambda r: int(r["id"])):
                conn.execute(
                    f"INSERT INTO audit_events_detached ({', '.join(COLUMNS)}, detached_at, detached_reason) "
                    f"VALUES ({', '.join(['%s'] * len(COLUMNS))}, %s, %s)",
                    (*[r[c] for c in COLUMNS], stamp, "restored from backup after account removal"))
            check, _ = load(conn)
            if not chain_valid(check, key)[0]:
                conn.rollback()
                sys.exit("Refused: verification inside the transaction failed. Nothing was written.")
            conn.commit()
        else:
            conn.rollback()
    print(json.dumps(result))
    if not confirm:
        print("Dry run only. Nothing was written. Add --confirm to write.")


def load(conn):
    cols = ", ".join(COLUMNS)
    rows = [dict(r) for r in conn.execute(f"SELECT {cols} FROM audit_events")]
    exists = conn.execute("SELECT to_regclass('public.audit_events_detached') IS NOT NULL AS e").fetchone()["e"]
    if exists:
        rows += [dict(r) for r in conn.execute(f"SELECT {cols} FROM audit_events_detached")]
    rows.sort(key=lambda r: int(r["id"]))
    return rows, bool(exists)


def summary(row):
    return None if row is None else {k: row.get(k) for k in ("id", "occurred_at", "action", "actor_type", "actor_id",
                                                             "account_id", "target_type", "target_id",
                                                             "previous_hash", "event_hash")}


def report():
    key = os.environ.get("POWER_AUDIT_SIGNING_KEY", "").strip()
    if not key:
        sys.exit("POWER_AUDIT_SIGNING_KEY is not set in this environment.")
    with connect() as conn:
        rows, detached = load(conn)
        removals = [dict(r) for r in conn.execute(
            "SELECT id, occurred_at, action, actor_type, actor_id, metadata_json FROM audit_events "
            "WHERE action IN ('maintenance.accounts_purged', 'account.deleted', 'account.disabled') ORDER BY id")]
        accounts = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        seq = conn.execute("SELECT last_value FROM audit_events_id_seq").fetchone()["last_value"]
    ok, failure_id = chain_valid(rows, key)
    failure = None if ok else next(r for r in rows if int(r["id"]) == failure_id)
    present = {int(r["id"]) for r in rows}
    missing = sorted(set(range(1, int(rows[-1]["id"]) + 1)) - present) if rows else []
    self_fail = [int(r["id"]) for r in rows
                 if not hmac.compare_digest(str(r.get("event_hash") or ""), event_hash(r, str(r.get("previous_hash") or ""), key))]
    out = {
        "checked_events": len(rows), "last_id": rows[-1]["id"] if rows else None, "audit_id_sequence_last": seq,
        "accounts_now": accounts, "detached_table_present": detached, "chain_valid": failure is None,
        "missing_ids": compress(missing), "missing_count": len(missing),
        "rows_whose_own_hash_fails": compress(self_fail), "rows_whose_own_hash_fails_count": len(self_fail),
        "account_removal_events": removals,
    }
    if failure is not None:
        i = rows.index(failure)
        before = rows[i - 1] if i else None
        stored = str(failure.get("previous_hash") or "")
        gap = list(range(int(before["id"]) + 1 if before else 1, int(failure["id"])))
        own_ok = int(failure["id"]) not in self_fail
        elsewhere = [int(r["id"]) for r in rows if str(r.get("event_hash") or "") == stored]
        if len(self_fail) >= max(1, int(0.9 * len(rows))):
            diagnosis = "signing_key_changed"
        elif not own_ok:
            diagnosis = "record_altered"
        elif gap and not elsewhere:
            diagnosis = "missing_predecessor"
        elif elsewhere:
            diagnosis = "rows_reordered_or_restored_out_of_sequence"
        else:
            diagnosis = "predecessor_altered_or_restored_state"
        out.update({"failure_event": summary(failure), "surviving_predecessor": summary(before),
                    "expected_previous_hash": str(before["event_hash"]) if before else "",
                    "stored_previous_hash": stored, "failing_event_own_hash_recomputes": own_ok,
                    "ids_missing_immediately_before_failure": compress(gap),
                    "stored_previous_hash_matches_other_row": elsewhere, "diagnosis": diagnosis})
    else:
        out["diagnosis"] = "intact"
    print(json.dumps(out, indent=2, default=str))


def export(spec):
    ids = set()
    for part in spec.split(","):
        a, _, b = part.partition("-")
        ids.update(range(int(a), int(b or a) + 1))
    with connect() as conn:
        cols = ", ".join(COLUMNS)
        for row in conn.execute(f"SELECT {cols} FROM audit_events WHERE id = ANY(%s) ORDER BY id", (sorted(ids),)):
            print(json.dumps(dict(row), sort_keys=True, default=str))


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command == "report":
        report()
    elif command == "export" and len(sys.argv) > 2:
        export(sys.argv[2])
    elif command == "restore" and len(sys.argv) > 2:
        restore(sys.argv[2], "--confirm" in sys.argv[3:])
    else:
        sys.exit("Usage: report | export IDS | restore FILE [--confirm]")
