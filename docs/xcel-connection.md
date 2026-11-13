# Xcel connection implementation

Prepared October 24, 2026. Live collection must remain disabled. No application
submission, agreement acceptance, customer authorization or vendor approval is
implied by deploying this connector.

## Registration addresses

| Field | URL |
|---|---|
| Redirect URL | `https://homeenergywatch.com/api/integrations/xcel/callback` |
| Notification URL | `https://homeenergywatch.com/api/integrations/xcel/notifications` |

Both routes work with collection disabled.

- **Callback** (`GET`). On the public domain it forwards to the same path on
  `app.homeenergywatch.com`, query intact, so the sign-in session is available.
  There it validates the single-use OAuth `state`, exchanges the code at
  `.../oauth/token` with `client_secret_basic`, and holds the tokens encrypted
  while the manager confirms their electric meters. With collection off it
  returns to the utility page without exchanging anything.
- **Notification** (`POST`). Served directly on the public domain, because
  POSTs cannot be redirected. Xcel sends `application/atom+xml` with no
  Authorization header. Valid notifications always get HTTP 202
  `{"status": "accepted"}`, whether or not the subscription is known.
  - If collection is on and the subscription matches a saved connection, a
    fetch for that connection is queued, and a worker processes it at once.
    The daily `--sync-utilities` run also drains the queue, and
    `python app.py --xcel-process-queue` drains it on demand.
  - The fetch is built from our stored bindings:
    `resource/Batch/Subscription/{subscription}/UsagePoint/{usagepoint}` on the
    configured Xcel host, with our own bearer token. The URL, date range and
    `file_id` in the notification are never fetched or stored.
- Malformed, oversized and unsupported notifications get 4xx responses.

`POWER_XCEL_REDIRECT_URI` must be exactly the registered redirect URL. It may
use the public domain (`POWER_MARKETING_HOSTS`) or the app domain
(`POWER_PUBLIC_BASE_URL`), with the path `/api/integrations/xcel/callback`.
After approval, Xcel lets the notification URL be changed by sending
`notification_url` as a header on an `ApplicationInformation/{application_id}`
request.

## Controls

`POWER_XCEL_ENABLED` defaults off. Enabling requires `true`, an issued client ID
and secret, and the registered HTTPS redirect. Do not enable as part
of this release. The guard covers sign-in, callback, meter selection, transport,
manual/scheduled sync and the final import check. Notification acknowledgment is
independent of this guard and cannot enable collection.

OAuth state is single-use, expires in ten minutes and is tied to the initiating
customer, account and browser. Pending tokens and saved tokens are encrypted.
Only an account manager with active data permission can connect. Xcel supplies
no account number, so a manager must explicitly identify and confirm electric
UsagePoints before saving. Unidentifiable meters must not be selected. The same
subscription UsagePoint URL cannot be attached to a second account. This does not
prove a physical meter has the same identifier across separate authorizations;
that remains a provider verification item before activation.

Notifications are unauthenticated hints. The handler bounds and validates XML,
returns a uniform acknowledgment, and never trusts a URL for network access or
changes permissions. A known subscription only queues a fetch; the fetch runs the
normal sync, which rechecks the switch, the customer's permission, the stored
token and the meter bindings. Repeated notifications collapse into one queued
fetch per connection.

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

## Validating after approval

Run these steps before onboarding any customer. Neither validation step touches
customer data.

1. Set the issued `POWER_XCEL_CLIENT_ID`, `POWER_XCEL_CLIENT_SECRET` and
   `POWER_XCEL_REDIRECT_URI`. Leave `POWER_XCEL_ENABLED=false`.
2. Run `python app.py --xcel-check-service`. It gets a client-credentials
   token with scope `FB=34 35`, then reads `ApplicationInformation` and
   `ReadServiceStatus`. It prints:
   - the application status
   - the registered redirect and notification URLs, as Xcel reports them
   - the service status
3. Switch on `POWER_XCEL_ENABLED=true` and complete one authorization end to end
   with an internal or friendly account, including a notification-triggered
   fetch.
4. Only then allow the first real customer.

`Batch/Bulk` (501) and `RetailCustomerInfo` (405) are not used.

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
