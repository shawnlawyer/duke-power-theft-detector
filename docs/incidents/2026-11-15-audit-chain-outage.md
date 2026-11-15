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

## Fix (PR "Keep audit history when accounts are removed")
- **Account removal now moves audit rows, unchanged,** to `audit_events_detached`. It never deletes them.
- **One chain across both tables:** the integrity check, startup and new events all treat the two tables as one chain, recomputing every hash.
- **Rows already deleted are restored from a backup copy** into `audit_events_detached`:
  - They go in verbatim, with the same ids and hashes, after each one is checked against its own hash.
  - Nothing is written unless the whole chain then verifies.
- **Nothing is rehashed, edited or deleted, and the startup check is not relaxed.**

## Runbook (production)
`S=scripts/audit_chain_standalone.py`. It works with the running image, `report`/`export` are read-only, and `restore` is a dry run without `--confirm`.

1. **Snapshot** `home-energy-watch-pg` and wait until it shows `available`.
2. **Report:**
   ```
   docker exec -i home-energy-watch python - report < $S
   ```
   Continue only if the diagnosis is `missing_predecessor` and `rows_whose_own_hash_fails_count` is 0. The `account_removal_events` times show when the deletion happened.
3. **Backup copy:** point-in-time restore to a temporary instance, at a time a few minutes before the first removal event. Confirm its chain is `intact`:
   ```
   docker exec -i -e POWER_DATABASE_URL=<copy DSN> home-energy-watch python - report < $S
   ```
4. **Export** the missing ids from the copy:
   ```
   docker exec -i -e POWER_DATABASE_URL=<copy DSN> home-energy-watch python - export <missing_ids> < $S > missing.jsonl
   ```
5. **Staging test:** restore the step-1 snapshot to a second temporary instance and run `restore missing.jsonl --confirm` against it. Then run the new image's startup and `--audit-chain-report` against it. Both must pass.
6. **Production (after approval):**
   1. Run `restore missing.jsonl` (dry run), then `--confirm`.
   2. Deploy `main`. The old image does not read `audit_events_detached`, so it still fails until the new code is deployed.
7. **Verify:**
   - `/health` returns 200
   - `/login` renders
   - `python app.py --audit-chain-report` shows `intact`
   - `/audit` shows "Integrity verified"

   Delete the temporary instances only after this.

## Evidence
(fill in from step 2 and step 4 outputs)
