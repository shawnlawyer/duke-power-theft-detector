# Utility onboarding register

The internal status area is at `/staff/utilities`. It is available to existing internal Commissioner-role staff, not customers, Analysts, Investigators or staff linked to an external commission. It uses the normal sign-in, session expiry, MFA and CSRF protections. This feature does not grant anyone a role.

Each utility has separate registration, connection, cost and work statuses, plus outstanding requirements, next action, blocker, owner, follow-up date and evidence. Search and filters find ready work, waiting responses, blocked work and due follow-ups. A blocked utility does not block the rest of the list. Countries and territories are free-form so new worldwide programs can be added.

## Initial coverage and evidence

The initial 72 records comprise 30 U.S. provider records from the private project approval register and 42 Canadian entries from the saved Green Button Alliance directory review. This is an initial inventory, not a complete global census. The Canadian entries are unreviewed; directory appearance does not establish membership, eligibility, electricity support, free access or approval.

The imported U.S. summaries are dated research snapshots, not a new provider check. Only National Grid has recorded registration approval. National Grid and Xcel are marked implemented with collection disabled. No initial record claims a verified working connection. Customer-upload status describes the chosen route, not proof that every provider's files are compatible.

Sensitive correspondence, credentials, company tax identifiers and private addresses are not copied. Evidence can be a summary, a reference to a privately retained original, or an HTTPS link. The app does not fetch evidence links. Keep sensitive documents in their controlled evidence store; this is not a credential vault.

## Persistence and safeguards

`utility_onboarding.py` creates two additive tables in the existing SQLite or PostgreSQL database. The seed is inserted once by stable identifier; subsequent migrations do not replace edits. Each save and its full revision snapshot commit together. A revision check rejects stale browser forms instead of overwriting a newer edit. History displays the latest 50 versions; older versions remain stored.

Submitted and approved registrations need evidence and a reviewed date. A recorded working connection additionally requires approved registration, a confirmed free route, a verification date and successful-import evidence. Complete work cannot have outstanding requirements or blockers. Paid routes must remain blocked. These are recordkeeping checks; a staff assertion is not independent proof.

No status edit sends messages, submits forms, accepts terms, creates credentials, changes billing, grants customer access, schedules collection or alters connector enable flags. Existing authorization and free-services gates remain separate. National Grid and Xcel must remain disabled until specifically authorized.

## Verification

Run `python -m pytest tests/test_utility_onboarding.py` and the full test suite. Coverage includes all three routes' authorization, external commission exclusion, CSRF, validation, worldwide additions, filters, seed preservation, transactional history, stale revisions and separation from live collection.

Use the existing merged-main production workflow. The new tables are additive; rolling back the application leaves their records intact. A live smoke check should confirm authentication on the route, schema/seed counts, and unchanged National Grid and Xcel collection gates without creating synthetic production records.
