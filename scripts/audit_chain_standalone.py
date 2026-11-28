#!/usr/bin/env python3
"""Audit-chain evidence and removal markers for a running production container.

Works with any app image (it does not import app.py), so it can be piped into
the current container while startup is failing:

    docker exec -i home-energy-watch python - report < scripts/audit_chain_standalone.py
    docker exec -i home-energy-watch python - mark-removals "reason" [--confirm] < scripts/audit_chain_standalone.py

report is READ ONLY. mark-removals writes one thing: a signed marker in
audit_chain_removals for each gap left by deliberately removed audit events,
identical to app.record_audit_removals. Only pure gaps qualify (ids missing,
the next event's own hash recomputes); anything else is refused and nothing is
written. No event is rehashed, edited or deleted. Dry run unless --confirm.

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


def chain_valid(rows, key, removals=()):
    bridges = {(str(m["after_hash"]), int(m["before_event_id"]), str(m["bridge_hash"]))
               for m in removals if hmac.compare_digest(str(m["signature"]), removal_signature(m, key))}
    previous = ""
    for row in rows:
        stored = str(row.get("previous_hash") or "")
        if row.get("event_hash") and stored != previous and (previous, int(row["id"]), stored) in bridges:
            previous = stored
        if not row.get("event_hash") or not hmac.compare_digest(stored, previous) \
                or not hmac.compare_digest(str(row["event_hash"]), event_hash(row, previous, key)):
            return False, int(row["id"])
        previous = str(row["event_hash"])
    return True, None


def mark_removals(reason, confirm):
    key = os.environ.get("POWER_AUDIT_SIGNING_KEY", "").strip()
    reason = (reason or "").strip()[:200]
    if not key or not reason:
        sys.exit("POWER_AUDIT_SIGNING_KEY and a reason are required.")
    with connect(read_only=not confirm) as conn:
        if confirm:
            conn.execute("LOCK TABLE audit_events IN SHARE ROW EXCLUSIVE MODE")
            conn.execute(REMOVALS_DDL)
        rows, removals = load(conn)
        bridges = {(str(m["after_hash"]), int(m["before_event_id"]), str(m["bridge_hash"])) for m in removals}
        stamp = __import__("datetime").datetime.now().isoformat(timespec="seconds")
        created, previous, previous_id = [], "", 0
        for row in rows:
            stored = str(row.get("previous_hash") or "")
            own_ok = hmac.compare_digest(str(row.get("event_hash") or ""), event_hash(row, stored, key))
            if not own_ok:
                conn.rollback()
                sys.exit(f"Refused: audit event {row['id']} was changed. Nothing was written.")
            if stored != previous and (previous, int(row["id"]), stored) not in bridges:
                missing = list(range(previous_id + 1, int(row["id"])))
                if not missing:
                    conn.rollback()
                    sys.exit(f"Refused: audit event {row['id']} does not follow a removal gap. Nothing was written.")
                marker = {"after_event_id": previous_id or None, "after_hash": previous, "before_event_id": int(row["id"]),
                          "bridge_hash": stored, "removed_count": len(missing), "removed_ids": ",".join(compress(missing)),
                          "reason": reason, "created_at": stamp}
                marker["signature"] = removal_signature(marker, key)
                created.append(marker)
            previous, previous_id = str(row["event_hash"]), int(row["id"])
        before = chain_valid(rows, key, removals)
        after = chain_valid(rows, key, removals + created)
        result = {"chain_valid_before": before[0], "chain_valid_after": after[0], "failure_event_id_after": after[1],
                  "gaps_marked": len(created), "removed_events_marked": sum(m["removed_count"] for m in created),
                  "gaps": [{k: m[k] for k in ("after_event_id", "before_event_id", "removed_ids")} for m in created],
                  "confirmed": confirm}
        if not after[0]:
            conn.rollback()
            print(json.dumps(result))
            sys.exit("Refused: the record still does not verify. Nothing was written.")
        if confirm:
            for m in created:
                conn.execute(f"INSERT INTO audit_chain_removals ({', '.join(REMOVAL_FIELDS)}, signature) "
                             f"VALUES ({', '.join(['%s'] * (len(REMOVAL_FIELDS) + 1))})",
                             (*[m[f] for f in REMOVAL_FIELDS], m["signature"]))
            rows, removals = load(conn)
            if not chain_valid(rows, key, removals)[0]:
                conn.rollback()
                sys.exit("Refused: verification inside the transaction failed. Nothing was written.")
            conn.commit()
        else:
            conn.rollback()
    print(json.dumps(result))
    if not confirm:
        print("Dry run only. Nothing was written. Add --confirm to write.")


REMOVAL_FIELDS = ("after_event_id", "after_hash", "before_event_id", "bridge_hash",
                  "removed_count", "removed_ids", "reason", "created_at")
REMOVALS_DDL = """CREATE TABLE IF NOT EXISTS audit_chain_removals (
    id BIGSERIAL PRIMARY KEY, after_event_id BIGINT, after_hash TEXT NOT NULL,
    before_event_id BIGINT NOT NULL, bridge_hash TEXT NOT NULL, removed_count INTEGER NOT NULL,
    removed_ids TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
    signature TEXT NOT NULL)"""  # Same as app.ensure_audit_removals_table.


def removal_signature(marker, key):
    # Must match app.audit_removal_signature exactly.
    canonical = json.dumps({f: marker.get(f) for f in REMOVAL_FIELDS}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=True)
    return hmac.new(("audit-removal:" + key).encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def load(conn):
    cols = ", ".join(COLUMNS)
    rows = [dict(r) for r in conn.execute(f"SELECT {cols} FROM audit_events ORDER BY id")]
    exists = conn.execute("SELECT to_regclass('public.audit_chain_removals') IS NOT NULL AS e").fetchone()["e"]
    removals = [dict(r) for r in conn.execute("SELECT * FROM audit_chain_removals ORDER BY id")] if exists else []
    return rows, removals


def summary(row):
    return None if row is None else {k: row.get(k) for k in ("id", "occurred_at", "action", "actor_type", "actor_id",
                                                             "account_id", "target_type", "target_id",
                                                             "previous_hash", "event_hash")}


def report():
    key = os.environ.get("POWER_AUDIT_SIGNING_KEY", "").strip()
    if not key:
        sys.exit("POWER_AUDIT_SIGNING_KEY is not set in this environment.")
    with connect() as conn:
        rows, removals = load(conn)
        removal_events = [dict(r) for r in conn.execute(
            "SELECT id, occurred_at, action, actor_type, actor_id, metadata_json FROM audit_events "
            "WHERE action IN ('maintenance.accounts_purged', 'account.deleted', 'account.disabled') ORDER BY id")]
        accounts = conn.execute("SELECT COUNT(*) AS n FROM accounts").fetchone()["n"]
        seq = conn.execute("SELECT last_value FROM audit_events_id_seq").fetchone()["last_value"]
    ok, failure_id = chain_valid(rows, key, removals)
    failure = None if ok else next(r for r in rows if int(r["id"]) == failure_id)
    present = {int(r["id"]) for r in rows}
    missing = sorted(set(range(1, int(rows[-1]["id"]) + 1)) - present) if rows else []
    self_fail = [int(r["id"]) for r in rows
                 if not hmac.compare_digest(str(r.get("event_hash") or ""), event_hash(r, str(r.get("previous_hash") or ""), key))]
    out = {
        "checked_events": len(rows), "last_id": rows[-1]["id"] if rows else None, "audit_id_sequence_last": seq,
        "accounts_now": accounts, "removal_markers": len(removals), "chain_valid": failure is None,
        "missing_ids": compress(missing), "missing_count": len(missing),
        "rows_whose_own_hash_fails": compress(self_fail), "rows_whose_own_hash_fails_count": len(self_fail),
        "account_removal_events": removal_events,
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



if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "report"
    if command == "report":
        report()
    elif command == "mark-removals" and len(sys.argv) > 2:
        mark_removals(sys.argv[2], "--confirm" in sys.argv[3:])
    else:
        sys.exit('Usage: report | mark-removals "reason" [--confirm]')
