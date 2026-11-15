# 2026-11-15: production down, audit integrity check fails at startup

## Symptom
- `/health` returns 500, and sign-in shows "An unexpected error occurred."
- Startup stops in `backfill_audit_event_hashes()`. Event 258 (2026-10-01, `auth.login_link_requested`) does not match its expected predecessor hash.

## Probable cause (to be confirmed by the report below)
- **The removal tool deleted audit rows.** `account_cleanup.purge_accounts`, behind the Delete account buttons from #58, deleted the removed account's rows in `audit_events`. Two things made it do that:
  - `audit_events.account_id` must reference a live account.
  - It also deleted the rows whose actor was a removed customer.
- **The chain then breaks at the next survivor.** The first surviving event after a deleted one still carries the deleted event's hash as `previous_hash`, so it no longer links to the row before it.
- **That fits the symptom exactly.** Event 258's own hash recomputes correctly. Only its link to its predecessor fails.
- **Rehearsed:** reproduced on a Postgres copy seeded by the production code, with byte-identical startup errors.

## Decision
The owner wants the removed audit records to stay gone. They will not be restored from backup.

## Fix
- **Signed removal markers.** A new table, `audit_chain_removals`, holds one marker per gap left by removed events. Each marker records:
  - the surviving event before the gap and its hash
  - the first surviving event after it, and the hash that event links to
  - how many ids were removed, and which
  - the reason and the time

  Each marker is HMAC-signed with the audit key.
- **The integrity check only bridges marked gaps.** It accepts a gap only where a correctly signed marker matches exactly. Every surviving event is still recomputed. An edited event, a forged or edited marker, or an unmarked gap still fails.
- **Markers are only written for pure gaps:** ids missing, and the next event's own hash recomputes. Anything else is refused and nothing is written.
- **Account removal** deletes the account's audit events and writes the markers in the same transaction, so the chain is never left broken again.
- **Nothing is rehashed or edited, removed events stay removed, and the startup check is not relaxed.**

## Runbook (production)
`S=scripts/audit_chain_standalone.py`. It works with the running image. `report` is read-only, and `mark-removals` is a dry run unless `--confirm` is given.

1. **Snapshot** `home-energy-watch-pg` and wait until it shows `available`.
2. **Report:**
   ```
   docker exec -i home-energy-watch python - report < $S
   ```
   Continue only if the diagnosis is `missing_predecessor` and `rows_whose_own_hash_fails_count` is 0.
3. **Dry run:**
   ```
   docker exec -i home-energy-watch python - mark-removals "fake accounts removed by owner" < $S
   ```
   `chain_valid_after` must be `true`.
4. **Write the markers:** repeat step 3 with `--confirm`.
5. **Deploy `main`.** The old image does not read the markers, so it still fails until the new code is deployed.
6. **Verify:**
   - `/health` returns 200
   - `/login` renders
   - `docker exec home-energy-watch python app.py --audit-chain-report` shows `intact`

## Rehearsal (Postgres copy)
1. Seeded the copy with the production code, and removed events the way the old removal did.
2. Old code at startup: `RuntimeError: The audit record did not pass its integrity check.`, the same as production.
3. Standalone report: `missing_predecessor`. The failing event's own hash recomputes.
4. `mark-removals`, dry run, then confirm: `chain_valid_after: true`.
5. `audit_events` is byte-identical before and after (md5 of all rows).
6. Old code still fails, which is why step 5 of the runbook is required. New code starts, and its integrity check reports `valid` with `removed_events: 2`.
7. New events chain on correctly after the repair.

## Evidence
(fill in from runbook step 2)
