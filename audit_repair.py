"""Read-only diagnosis of the audit integrity chain, and marking removed events.

    python app.py --audit-chain-report
        Evidence report (JSON). Read-only; skips schema migration, so it works
        while startup is failing the integrity check.

    python app.py --audit-mark-removals "reason" [--confirm-audit-removals]
        For audit events that were deliberately removed, record a signed marker
        at each gap so the remaining record verifies. Dry run by default. Only
        pure gaps qualify: any edited event or changed key is refused and
        nothing is written. No event is rehashed or edited.
"""
from __future__ import annotations

import hmac


def _compress(ids):
    ranges, start, prev = [], None, None
    for value in sorted(ids):
        if start is None:
            start = prev = value
        elif value == prev + 1:
            prev = value
        else:
            ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = value
    if start is not None:
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ranges


def _summary(row):
    if row is None:
        return None
    return {key: row.get(key) for key in ("id", "occurred_at", "action", "actor_type", "actor_id",
                                          "account_id", "target_type", "target_id", "previous_hash", "event_hash")}


def chain_report(api):
    with api.get_db_connection(ensure_schema=False) as conn:
        rows = api.load_audit_chain_rows(conn)
        removals = api.load_audit_removals(conn)
        removal_events = [dict(r) for r in conn.execute(
            """SELECT id, occurred_at, action, actor_type, actor_id, metadata_json FROM audit_events
               WHERE action IN ('maintenance.accounts_purged', 'account.deleted') ORDER BY id""").fetchall()]
    status = api.audit_chain_status_for_rows(rows, removals)
    report = {
        "checked_events": len(rows),
        "last_id": int(rows[-1]["id"]) if rows else None,
        "chain_valid": bool(status["valid"]),
        "failure_event_id": status["failure_event_id"],
        "removal_markers": len(removals),
        "account_removal_events": removal_events,
    }
    present = {int(r["id"]) for r in rows}
    missing = sorted(set(range(1, int(rows[-1]["id"]) + 1)) - present) if rows else []
    report["missing_ids"] = _compress(missing)
    report["missing_count"] = len(missing)
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
    stored_previous = str(failing.get("previous_hash") or "")
    own_ok = int(failing["id"]) not in self_mismatch
    gap = list(range(int(before["id"]) + 1 if before else 1, int(failing["id"])))
    links_elsewhere = [int(r["id"]) for r in rows if str(r.get("event_hash") or "") == stored_previous]
    report.update({
        "failing_event": _summary(failing),
        "surviving_predecessor": _summary(before),
        "expected_previous_hash": str(before["event_hash"]) if before else "",
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


def mark_removals(api, reason, *, confirm=False):
    with api.get_db_connection(ensure_schema=False) as conn:
        before = api.audit_chain_status(conn) if api.table_exists(conn, "audit_chain_removals") else \
            api.audit_chain_status_for_rows(api.load_audit_chain_rows(conn))
        created = api.record_audit_removals(conn, reason)
        after = api.audit_chain_status(conn)
        result = {
            "chain_valid_before": bool(before["valid"]),
            "chain_valid_after": bool(after["valid"]),
            "gaps_marked": len(created),
            "removed_events_marked": sum(int(m["removed_count"]) for m in created),
            "gaps": [{"after_event_id": m["after_event_id"], "before_event_id": m["before_event_id"],
                      "removed_ids": m["removed_ids"]} for m in created],
            "confirmed": confirm,
        }
        if confirm:
            conn.commit()
        else:
            conn.rollback()
    return result
