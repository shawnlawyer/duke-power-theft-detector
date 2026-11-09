"""Operator tools to review and permanently remove test or fake accounts.

    python app.py --list-accounts
    python app.py --purge-accounts NUMBER [NUMBER ...]            # dry run
    python app.py --purge-accounts NUMBER [NUMBER ...] --confirm-purge

A purge deletes the account and everything tied to it, including uploaded files.
It also deletes any customer sign-in left with no account afterwards. Accounts
with a paid subscription or a legal hold are refused. The built-in "primary"
placeholder is emptied but kept, because the app recreates it anyway.
"""
from __future__ import annotations

ACCOUNT_TABLES = (
    # Children before parents, so foreign keys hold on Postgres too.
    "xcel_meter_bindings",
    "interval_readings",
    "imported_files",
    "account_load_items",
    "household_profiles",
    "weather_daily_cache",
    "account_access_emails",
    "account_commission_access",
    "account_notes",
    "report_artifacts",
    "account_data_authorizations",
    "account_data_requests",
    "customer_billing",
    "affiliate_commissions",
    "account_equipment_history",
    "household_assistant_interactions",
    "audit_events",
)
CUSTOMER_TABLES = (
    "customer_policy_acceptances",
    "customer_auth_tokens",
    "account_data_authorizations",
    "account_data_requests",
    "customer_billing",
    "affiliate_commissions",
    "account_notes",
    "household_assistant_interactions",
)
OAUTH_STATE_TABLES = ("national_grid_oauth_states", "xcel_oauth_states", "coned_oru_oauth_states")
SYNC_LOCK_TABLES = ("national_grid_sync_locks", "xcel_sync_locks", "coned_oru_sync_locks")
PAID_STATUSES = {"active", "trialing", "past_due"}


def _placeholders(values):
    return ", ".join("?" for _ in values)


def list_accounts_for_review(api):
    """Every account with what is needed to tell real from fake."""
    with api.get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT accounts.id, accounts.account_number, accounts.display_name, accounts.energy_company,
                   accounts.created_at, household_profiles.address
            FROM accounts
            LEFT JOIN household_profiles ON household_profiles.account_id = accounts.id
            ORDER BY accounts.id
            """
        ).fetchall()
        result = []
        for row in rows:
            account_id = int(row["id"])
            emails = [r["email"] for r in conn.execute(
                "SELECT email FROM account_access_emails WHERE account_id = ? ORDER BY email", (account_id,))]
            billing = [str(r["subscription_status"] or "") for r in conn.execute(
                "SELECT subscription_status FROM customer_billing WHERE account_id = ?", (account_id,))]
            readings = conn.execute(
                "SELECT COUNT(*) AS n FROM interval_readings WHERE account_id = ?", (account_id,)).fetchone()["n"]
            result.append({
                "account_number": row["account_number"],
                "display_name": row["display_name"] or "",
                "energy_company": row["energy_company"] or "",
                "address": row["address"] or "",
                "created_at": row["created_at"] or "",
                "emails": emails,
                "billing": billing,
                "readings": int(readings),
            })
    return result


def purge_accounts(api, account_numbers, *, confirm=False):
    numbers = [api.normalize_account_number(n) for n in account_numbers if str(n or "").strip()]
    if not numbers:
        raise ValueError("Name at least one account number to purge.")
    with api.get_db_connection() as conn:
        accounts = conn.execute(
            f"SELECT id, account_number FROM accounts WHERE account_number IN ({_placeholders(numbers)})",
            tuple(numbers),
        ).fetchall()
        found = {row["account_number"]: int(row["id"]) for row in accounts}
        missing = [n for n in numbers if n not in found]
        if missing:
            raise ValueError("No such account: " + ", ".join(missing))
        ids = list(found.values())
        marks = _placeholders(ids)
        held = conn.execute(
            f"SELECT DISTINCT accounts.account_number FROM account_legal_holds JOIN accounts ON accounts.id = account_legal_holds.account_id "
            f"WHERE account_legal_holds.account_id IN ({marks}) AND account_legal_holds.status = 'active'",
            tuple(ids),
        ).fetchall()
        if held:
            raise ValueError("Legal hold, not purged: " + ", ".join(r["account_number"] for r in held))
        paid = conn.execute(
            f"SELECT DISTINCT accounts.account_number FROM customer_billing JOIN accounts ON accounts.id = customer_billing.account_id "
            f"WHERE customer_billing.account_id IN ({marks}) AND customer_billing.subscription_status IN ({_placeholders(PAID_STATUSES)})",
            (*ids, *sorted(PAID_STATUSES)),
        ).fetchall()
        if paid:
            raise ValueError("Paid subscription, cancel it in Stripe first: " + ", ".join(r["account_number"] for r in paid))

        emails = [r["email"] for r in conn.execute(
            f"SELECT DISTINCT email FROM account_access_emails WHERE account_id IN ({marks})", tuple(ids))]
        customer_ids = set()
        if emails:
            customer_ids |= {int(r["id"]) for r in conn.execute(
                f"SELECT id FROM customer_users WHERE email IN ({_placeholders(emails)})", tuple(emails))}
        for table in ("customer_billing", "account_data_authorizations"):
            customer_ids |= {int(r["customer_user_id"]) for r in conn.execute(
                f"SELECT DISTINCT customer_user_id FROM {table} WHERE account_id IN ({marks}) AND customer_user_id IS NOT NULL",
                tuple(ids))}
        files = [path for account_id in ids for path in api.account_managed_files(conn, account_id)]

        summary = {"accounts": sorted(found), "customers_removed": [], "files": len(files), "rows": 0, "confirmed": confirm}
        connection_ids = [int(r["id"]) for r in conn.execute(
            f"SELECT id FROM utility_connections WHERE account_id IN ({marks})", tuple(ids))]
        for table in SYNC_LOCK_TABLES:
            if connection_ids:
                summary["rows"] += conn.execute(
                    f"DELETE FROM {table} WHERE connection_id IN ({_placeholders(connection_ids)})",
                    tuple(connection_ids)).rowcount or 0
        for table in OAUTH_STATE_TABLES:
            summary["rows"] += conn.execute(
                f"DELETE FROM {table} WHERE account_number IN ({_placeholders(numbers)})", tuple(numbers)).rowcount or 0
        for table in ACCOUNT_TABLES + ("utility_connections",):
            summary["rows"] += conn.execute(f"DELETE FROM {table} WHERE account_id IN ({marks})", tuple(ids)).rowcount or 0
        for number, account_id in found.items():
            if number == api.DEFAULT_ACCOUNT_NUMBER:
                conn.execute(
                    "UPDATE accounts SET display_name = 'Primary account', energy_company = NULL, "
                    "baseline_date = NULL, meter_value = NULL WHERE id = ?", (account_id,))
            else:
                summary["rows"] += conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,)).rowcount or 0

        # Remove sign-ins that no longer reach any account.
        for customer_id in sorted(customer_ids):
            customer = conn.execute("SELECT email FROM customer_users WHERE id = ?", (customer_id,)).fetchone()
            if customer is None:
                continue
            still_linked = conn.execute(
                "SELECT 1 FROM account_access_emails WHERE email = ? "
                "UNION SELECT 1 FROM customer_billing WHERE customer_user_id = ? "
                "UNION SELECT 1 FROM account_data_authorizations WHERE customer_user_id = ?",
                (customer["email"], customer_id, customer_id),
            ).fetchone()
            if still_linked:
                continue
            for table in CUSTOMER_TABLES:
                summary["rows"] += conn.execute(
                    f"DELETE FROM {table} WHERE customer_user_id = ?", (customer_id,)).rowcount or 0
            summary["rows"] += conn.execute(
                "DELETE FROM passkey_credentials WHERE actor_kind = 'customer' AND actor_id = ?", (customer_id,)).rowcount or 0
            summary["rows"] += conn.execute(
                "DELETE FROM audit_events WHERE actor_type = 'customer' AND actor_id = ?", (customer_id,)).rowcount or 0
            summary["rows"] += conn.execute("DELETE FROM customer_users WHERE id = ?", (customer_id,)).rowcount or 0
            summary["customers_removed"].append(customer["email"])

        if not confirm:
            conn.rollback()
            return summary
        conn.commit()

    for path in files:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    api.record_audit_event(
        "maintenance.accounts_purged",
        actor_type="system",
        metadata={"accounts": len(found), "customers": len(summary["customers_removed"]), "files": len(files)},
    )
    return summary
