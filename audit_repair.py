"""Read-only diagnosis of the audit integrity chain, and the one supported repair.

    python app.py --audit-chain-report
        Evidence report (JSON). Read-only; skips schema migration, so it works
        while startup is failing the integrity check.

    python app.py --audit-export-ids 250-257,300
        Print those audit rows as JSON lines, verbatim. Run it against a copy of
        the database from before the break (POWER_DATABASE_URL pointing at a
        snapshot or point-in-time restore), never to change anything.

    python app.py --audit-restore-detached rows.jsonl [--confirm-audit-restore]
        Put original rows that are missing from the chain back into
        audit_events_detached, unchanged. Dry run by default. The rows are
        accepted only if every hash recomputes with the current signing key and
        the whole chain then verifies; otherwise nothing is written.

Nothing here rehashes, edits or deletes an audit row.
"""
from __future__ import annotations

import hmac
import json


def _compress(ids):
    ranges, start, prev = [], None, None
    for value in sorted(ids):
        if start is None:
            start = prev = value
        elif value == prev + 1:
            prev = value
        else:
            ranges.append((start, prev))
            start = prev = value
    if start is not None:
        ranges.append((start, prev))
    return [f"{a}" if a == b else f"{a}-{b}" for a, b in ranges]


def parse_ids(text):
    ids = set()
    for part in str(text or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            if b < a or b - a > 100000:
                raise ValueError("Bad id range: " + part)
            ids.update(range(a, b + 1))
        else:
            ids.add(int(part))
    if not ids:
        raise ValueError("Name at least one audit event id.")
    return sorted(ids)


def _summary(row):
    if row is None:
        return None
    return {key: row.get(key) for key in ("id", "occurred_at", "action", "actor_type", "actor_id",
                                          "account_id", "target_type", "target_id", "previous_hash", "event_hash")}


def chain_report(api):
    with api.get_db_connection(ensure_schema=False) as conn:
        rows = api.load_audit_chain_rows(conn)
        detached = api.table_exists(conn, "audit_events_detached")
        purge_rows = [dict(r) for r in conn.execute(
            """SELECT id, occurred_at, action, actor_type, actor_id, metadata_json FROM audit_events
               WHERE action IN ('maintenance.accounts_purged', 'account.deleted') ORDER BY id""").fetchall()]
    status = api.audit_chain_status_for_rows(rows)
    report = {
        "checked_events": len(rows),
        "first_id": int(rows[0]["id"]) if rows else None,
        "last_id": int(rows[-1]["id"]) if rows else None,
        "detached_table_present": detached,
        "chain_valid": bool(status["valid"]),
        "failure_event_id": status["failure_event_id"],
        "account_removal_events": purge_rows,
    }
    present = {int(r["id"]) for r in rows}
    if rows:
        # Ids start at 1 in both SQLite and Postgres, so count from there.
        missing = sorted(set(range(1, int(rows[-1]["id"]) + 1)) - present)
        report["missing_ids"] = _compress(missing)
        report["missing_count"] = len(missing)
    # Does each row's own hash recompute from its own stored previous_hash?
    # If almost none do, the signing key changed; if only some, those rows changed.
    self_mismatch = [int(r["id"]) for r in rows
                     if not hmac.compare_digest(str(r.get("event_hash") or ""),
                                                api.audit_event_hash(r, str(r.get("previous_hash") or "")))]
    report["rows_whose_own_hash_fails"] = _compress(self_mismatch)
    report["rows_whose_own_hash_fails_count"] = len(self_mismatch)
    if status["valid"]:
        report["diagnosis"] = "intact"
        return report
    index = next(i for i, r in enumerate(rows) if int(r["id"]) == int(status["failure_event_id"]))
    failing = rows[index]
    before = rows[index - 1] if index else None
    expected_previous = str(before["event_hash"]) if before else ""
    stored_previous = str(failing.get("previous_hash") or "")
    own_ok = int(failing["id"]) not in self_mismatch
    gap = list(range(int(before["id"]) + 1 if before else 1, int(failing["id"])))
    links_elsewhere = [int(r["id"]) for r in rows if str(r.get("event_hash") or "") == stored_previous]
    report.update({
        "failing_event": _summary(failing),
        "surviving_predecessor": _summary(before),
        "expected_previous_hash": expected_previous,
        "stored_previous_hash": stored_previous,
        "failing_event_own_hash_recomputes": own_ok,
        "ids_missing_immediately_before_failure": _compress(gap),
        "stored_previous_hash_matches_other_row": links_elsewhere,
    })
    if len(self_mismatch) >= max(1, int(0.9 * len(rows))):
        diagnosis = "signing_key_changed"
    elif not own_ok:
        diagnosis = "record_altered"
    elif gap and not links_elsewhere:
        diagnosis = "missing_predecessor"
    elif links_elsewhere:
        diagnosis = "rows_reordered_or_restored_out_of_sequence"
    else:
        diagnosis = "predecessor_altered_or_restored_state"
    report["diagnosis"] = diagnosis
    return report


def export_rows(api, ids):
    columns = ", ".join(api.AUDIT_EVENT_COLUMNS)
    marks = ", ".join("?" for _ in ids)
    with api.get_db_connection(ensure_schema=False) as conn:
        found = [dict(r) for r in conn.execute(
            f"SELECT {columns} FROM audit_events WHERE id IN ({marks}) ORDER BY id", tuple(ids)).fetchall()]
    return found


def read_rows(path, columns):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if set(row) != set(columns):
                    raise ValueError("Each line must hold exactly the audit event columns.")
                rows.append(row)
    if not rows:
        raise ValueError("No rows to restore.")
    return rows


def restore_detached(api, rows, *, confirm=False, reason="restored from backup after account removal"):
    """Return original rows to the chain, unchanged, only if the chain then verifies."""
    for row in rows:
        if not row.get("event_hash") or not hmac.compare_digest(
                str(row["event_hash"]), api.audit_event_hash(row, str(row.get("previous_hash") or ""))):
            raise ValueError(f"Audit row {row.get('id')} does not match its own hash; it is not an original record.")
    with api.get_db_connection(ensure_schema=False) as conn:
        api.ensure_audit_detached_table(conn)
        current = api.load_audit_chain_rows(conn)
        present = {int(r["id"]) for r in current}
        clash = sorted(int(r["id"]) for r in rows if int(r["id"]) in present)
        if clash:
            raise ValueError("These audit ids already exist and are never overwritten: " + ", ".join(_compress(clash)))
        before = api.audit_chain_status_for_rows(current)
        merged = sorted(current + [dict(r) for r in rows], key=lambda r: int(r["id"]))
        after = api.audit_chain_status_for_rows(merged)
        result = {"rows": len(rows), "ids": _compress(int(r["id"]) for r in rows),
                  "chain_valid_before": bool(before["valid"]), "chain_valid_after": bool(after["valid"]),
                  "failure_event_id_after": after["failure_event_id"], "confirmed": confirm}
        if not after["valid"]:
            conn.rollback()
            raise ValueError("The chain still does not verify with these rows. Nothing was written. "
                             f"First failure: event {after['failure_event_id']}.")
        if not confirm:
            conn.rollback()
            return result
        columns = list(api.AUDIT_EVENT_COLUMNS)
        timestamp = api.timestamp_now()
        for row in sorted(rows, key=lambda r: int(r["id"])):
            conn.execute(
                f"INSERT INTO audit_events_detached ({', '.join(columns)}, detached_at, detached_reason) "
                f"VALUES ({', '.join('?' for _ in columns)}, ?, ?)",
                (*[row.get(c) for c in columns], timestamp, reason),
            )
        if not api.audit_chain_status(conn)["valid"]:
            conn.rollback()
            raise ValueError("Verification inside the transaction failed. Nothing was written.")
        conn.commit()
    return result
