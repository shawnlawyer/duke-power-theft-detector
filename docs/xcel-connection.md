# Xcel connection implementation

Prepared October 24, 2026. Live collection must remain disabled. No application
submission, agreement acceptance, customer authorization or vendor approval is
implied by deploying this connector.

## Registration addresses

- Redirect: `https://app.homeenergywatch.com/utility-connection/xcel/callback`
- Notification: `https://app.homeenergywatch.com/utility-connection/xcel/notifications`

Both routes exist with collection disabled. The callback safely returns to the
utility page without exchanging credentials; the notification route returns 503
while disabled. Do not describe disabled endpoints as passing Xcel onboarding tests.

## Controls

`POWER_XCEL_ENABLED` defaults off. Enabling requires `true`, an issued client ID
and secret, and an HTTPS redirect on `POWER_PUBLIC_BASE_URL`. Do not enable as part
of this release. The guard covers sign-in, callback, meter selection, transport,
manual/scheduled sync, notification handling and the final import check.

OAuth state is single-use, expires in ten minutes and is tied to the initiating
customer, account and browser. Pending tokens and saved tokens are encrypted.
Only an account manager with active data permission can connect. Xcel supplies
no account number, so a manager must explicitly identify and confirm electric
UsagePoints before saving. Unidentifiable meters must not be selected. The same
subscription UsagePoint URL cannot be attached to a second account. This does not
prove a physical meter has the same identifier across separate authorizations;
that remains a provider verification item before activation.

Notifications are unauthenticated hints. The handler bounds and validates XML,
returns a uniform acknowledgment, and never trusts a URL for network access,
changes permissions, or immediately runs a sync. The existing daily scheduler
fetches through stored account bindings and verifies the grant with Xcel.
Notification date ranges and internal file_id values are not retrieval authority.

Requests go only to the documented Xcel host and paths, without following
redirects. Tokens are not logged. Refresh rotates saved tokens under a per-
connection lease. Revoked/expired access clears the matching credential without
clearing a newer connection. Account permission and credential identity are
rechecked before imported rows are committed.

Import preserves intervals and gaps. It accepts forward interval energy in Wh,
scales units, excludes estimates/questionable readings and rejects ambiguous
channels, cross-meter data, pagination and overlapping/conflicting intervals.
Multiple selected meters are summed only over intervals present for every meter.
Existing readings remain append-only: corrections are reported as conflicts.

## Evidence and outstanding activation checks

Implementation is based on Xcel support's October 15 email, saved in the main
workspace under `approvals/pending/xcel-2026-10-15-technical-response.json`.
Several sentences in that source are incomplete; fixtures are synthetic and are
not represented as utility interoperability proof.

Before any activation: obtain registration approval and record accepted terms;
confirm the issued endpoints, Authorization URI identity, UsagePoint listing and
stable meter identity with Xcel; verify scope/consent, revocation and retention
rules; provision credentials securely; then test non-customer resources and one
expressly authorized customer's flow. Check available history, payload size and
pagination with actual data. No sandbox or paid service is assumed.

Run `PYTHONPATH=. python -m pytest tests/test_xcel.py` plus the full suite. Tests
must use isolated temporary databases and mocked Xcel traffic, not production
accounts or credentials.
