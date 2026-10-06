# Home Energy Watch Developer Guide

This is the shortest path to making a safe change in Home Energy Watch. The application is concentrated in one Flask service, but its responsibilities have stable boundaries. Start here before reading all of `app.py`.

## Product boundary

Home Energy Watch stores account-scoped electricity history, household context, notes, weather, reports, and review activity. It supports manual utility exports, customer-approved utility connections, and an optional Duke automatic connection.

It does **not** collect a Duke password or imply that automatic utility refresh is available nationwide. Duke customers complete Duke's own interactive sign-in with the open-source OAuth helper; the backend stores the resulting tokens in encrypted form. Manual setup and file upload remain supported for every U.S. household.

## Five-minute start

```bash
uv sync
PYTHONPATH=. uv run pytest
PYTHONPATH=. uv run python -m py_compile app.py
git diff --check
```

Run the local Docker stack with `docker compose up --build`, then open `http://localhost:8001`.

Local Docker uses SQLite by default:

- uploaded files: `./data/input`
- generated reports: `./data/output`
- local database: `./data/output/power-history.db`

Tests use temporary paths. Never point tests at the production database.

## Repository map

| Area | Files | Responsibility |
| --- | --- | --- |
| Flask service | `app.py` | routes, auth, persistence, import orchestration, analysis, reports, billing, API |
| Shared shell | `templates/base.html`, `static/styles.css`, `static/app.js` | signed-in layout, navigation, shared behavior |
| Customer pages | `templates/customer_*.html`, `templates/history_page.html`, `templates/_account_panel.html` | homeowner account, history, notes, household setup, billing |
| Review pages | `templates/index.html`, `templates/_analysis_surface.html`, `templates/_staff_panel.html` | commission/reviewer dashboard and drilldowns |
| Public pages | `templates/marketing_*.html`, `templates/marketing_base.html`, `static/marketing.css` | public website and product copy |
| Database setup | `migrate_database_postgres()` and `migrate_database()` in `app.py` | Postgres and SQLite schema creation/migrations |
| Utility adapters | parser classes near `parse_interval_file()` | Green Button ESPI XML, Duke-style XML, and interval CSV |
| Tests | `tests/test_app.py`, `tests/test_omen_deploy_contract.py`, `tests/test_release_security_contract.py` | behavior, import, security, and deployment contracts |
| Production | `deploy/ec2/README.md`, `deploy/ec2/home-energy-watch.service`, `deploy/ec2/home-energy-watch-direct.sh` | EC2 Docker launch and operations |

## Runtime and data flow

`web_app = create_app()` builds Flask. `ensure_database()` selects the backend from `POWER_DATABASE_URL`:

- without a Postgres URL: SQLite at `POWER_DB_PATH` or the default output path;
- with `postgresql://...`: RDS/Postgres through `psycopg`.

The schema is created or migrated at startup. When adding a table or column, update **both** `migrate_database_postgres()` and `migrate_database()` and add a migration regression test.

### Customer history upload

The normal upload path is:

1. `POST /analyze` receives the file and account fields.
2. `save_uploaded_file()` stores the upload under the configured input directory.
3. `parse_interval_file()` selects a utility adapter and returns the canonical interval frame.
4. `import_interval_file_to_db()` and `import_interval_frame_to_db()` append readings to the selected account.
5. `analyze_history_store()` reads saved history and applies alert rules.
6. `build_report_context()` supplies the dashboard and report templates.

Reading identity is account plus `start_epoch` plus `duration_s`. A matching `wh` value is already present. A different `wh` value is a conflict and is skipped; existing history is never silently overwritten. Preserve the added/already-present/conflict-skipped result when changing imports.

`POST /compare` uses the same adapters, aligns comparable periods, and writes Markdown plus CSV artifacts under the output directory.

### Analysis and reports

The main analysis functions are `compute_daily_summary()`, `flag_suspicious_days()`, `compute_alert_events()`, `analyze_history_store()`, and `analyze_interval_file_comparison()`. Report persistence is handled by `save_json_report()` and `save_comparison_artifact()`.

Keep measurement separate from interpretation. A flagged interval is a review prompt, not proof of theft or tampering.

## Authentication and authorization

Customer and commission identities use separate database records, but the public `/login` flow resolves both through shared, one-time email-link sign-in. Legacy password-reset endpoints remain available only for migration and older platform-operator records; new customer and commission records do not store passwords.

Use the existing helpers inside `create_app()`:

- `current_customer_user()` and `current_staff_user()` validate sessions;
- `require_customer_user()` and `require_staff_user()` protect page/API access;
- `require_account_actor()` checks account access level and write permission;
- `require_commissioner()` protects commissioner-only operations.

Customer writes must be scoped to an account access record. Account notes, inventory, utility connections, imports, and reports must never be loaded by account number alone without an authorization check. Keep CSRF protection on state-changing forms; Stripe webhooks are the intentional CSRF exception.

Email tokens, passkeys, password resets, email verification, rate limits, staff MFA, and audit events already have persistence and tests. Extend those paths rather than creating a second authentication mechanism.

## Account-scoped records

- `accounts`: account number, display name, utility, baseline, and report identity
- `household_profiles`: address, ZIP, occupants, heating/cooling, water heating, weather location
- `account_load_items`: household inventory and wattage assumptions
- `interval_readings`: canonical meter readings
- `imported_files`: raw upload provenance
- `account_notes`: dated household observations with author and timestamp
- `utility_connections`: customer-approved feed configuration and sync status
- `weather_daily_cache`: weather context for an account/date
- `report_artifacts`: generated report references
- `audit_events`: tamper-evident activity history

Customer exports are built by `build_customer_data_archive()` and must remain tenant-bounded. Never add account numbers, emails, meter identifiers, or notes to public URLs.

## Utility adapters

Adapters are selected by `select_utility_feed_adapter()` and return a common interval frame. Add a feed by:

1. implementing the adapter near the existing XML/CSV adapters;
2. returning the canonical columns used by `build_interval_frame()`;
3. registering it in the selector;
4. adding fixtures for valid, empty, malformed, and timezone-sensitive input;
5. testing incremental, idempotent import behavior.

Keep utility-specific parsing out of route handlers and analysis functions.

### Duke automatic connection

`POST /utility-connection/duke/start` creates a short-lived PKCE flow for an account manager. `POST /utility-connection/duke/complete` exchanges the one-time code, confirms that Duke returned the selected account and an electric meter, and stores only encrypted OAuth material. Never log the authorization code, verifier, access token, refresh token, or ID token.

Connections use `access_method=duke_oauth`. `sync_utility_connection()` dispatches those records to `sync_duke_oauth_connection()`; all other saved export connections keep the existing fetch-and-parse path. Duke sync fetches completed hourly readings for a rolling 30-day window, combines multiple electric meters at the same timestamp, refreshes the encrypted token bundle, and passes the frame through `import_interval_frame_to_db()`. This overlap is intentional: identical readings are idempotent and conflicts preserve the stored value.

Production must run `python app.py --sync-utilities` daily. A failed or expired Duke token should mark the connection failed and tell the customer to connect again; it must not remove existing readings. Keep the manual Duke Usage Details link and History upload UI available even when a Duke automatic connection exists.

### Con Edison and Orange & Rockland connector draft

This connector is **unfinished and disabled**. `GREEN_BUTTON_IMPLEMENTATION_READY`
is deliberately false in source, independent of deployment settings or utility
approval. The same check gates the displayed connection controls, authorization
start/callback, connection save, and manual/scheduled fetch. Do not remove it to
make a registration or demonstration appear complete. Customer uploads remain
the supported launch path.

The draft uses provider-returned resource URIs, encrypted token storage, bounded
HTTPS requests with no redirects, preserved customer-granted refresh scope, and
a stored account/subscription binding. Consent and credential checks guard
refresh and import; conditional credential updates cannot restore a connection
that was revoked or replaced. Imports acquire consent and connection locks in
the same transaction as the interval writes. SQLite regression tests exercise
revocation before import and during refresh/download; PostgreSQL concurrency
behavior still needs integration testing.

The account check currently accepts only the explicit `AuthorizationModel`
shape in the published utility Swagger: the selected account, returned resource,
retail customer, and one matching subscription must agree. Unknown wrappers or
XML identity responses fail closed. Fixtures are synthetic and do **not** prove
compatibility with the utility's actual responses. The existing configured
`resource_url` value is not used to override the customer-specific token response.

Before activation, finish and verify with utility-issued test credentials:

1. Exact authorization/customer/usage-point relationships, scopes, expiry,
   revocation responses, and the supported JSON/XML response shapes.
2. The documented asynchronous batch lifecycle: persist pending requests,
   authenticate and correlate notifications, fetch every authorized chunk,
   validate each file's account identity, retry idempotently without duplicate
   pending requests, and handle expiry. HTTP 202 currently raises an error; it
   is never recorded as a completed empty export.
3. Any separately hosted batch-download origins and their credential rules.
   The draft currently allows only the token endpoint's full HTTPS origin.
4. Real PostgreSQL concurrent revocation/reconnect/import tests, utility
   acceptance, documented no-cost access, and explicit approval to activate.

The current unit/SQLite tests are in `tests/test_green_button.py`. They do not
replace the utility's acceptance tests or establish provider certification.

Source: [Con Edison Share My Data onboarding guide](https://www.coned.com/-/media/files/coned/documents/accountandbilling/share-my-data/onboarding-doc.pdf),
including the Swagger and Postman downloads linked on pages 22–23. Do not commit
signed download URLs, utility credentials, or customer response payloads.

## Weather, inventory, and baselines

Household inventory is stored through `account_load_items`; the all-on check compares inventory wattage assumptions with measured load. Weather is fetched and cached through the account weather helpers, then added to suspicious-day and hourly views.

Baseline behavior is controlled by the selected baseline date and night settings. If thresholds change, update defaults, UI controls, report text, and tests together. A baseline is a comparison reference, not a claim that every home should have zero overnight use.

## Email

Production email uses Amazon SES from the backend only:

- `POWER_EMAIL_BACKEND=ses`
- `POWER_EMAIL_FROM` must be a verified SES sender/domain
- `POWER_EMAIL_REGION` must match the SES identity region
- `POWER_EMAIL_REPLY_TO` is optional

Never put SES credentials in templates or browser JavaScript. Do not log tokens, passwords, full reset URLs, or email contents. When troubleshooting, check the request audit event, application logs, container environment names without values, and SES identity/DKIM status in the configured region.

## Billing

Billing calls belong in the backend. Plans are defined by `BILLING_PLAN_DEFINITIONS` and are subscription-only:

- Home Watch: `$239.88/year` per electric account

Subscribers can create unlimited reports while active. Commission access is free and read-only. Required production variables include `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_HOME`, and `POWER_BILLING_ENABLED`. Keep billing closed until the annual Price ID is installed and verified. Do not create live charges, subscriptions, refunds, or provider configuration changes without explicit approval.

## Route map

| Surface | Main routes |
| --- | --- |
| Customer | `/login`, `/signup`, `/customer`, `/customer/account`, `/customer/history`, `/customer/utility`, `/customer/inventory`, `/customer/billing` |
| Password/email | `/forgot-password`, `/customer/verify-email`, `/customer/reset-password` |
| Staff | `/staff`, `/people`, `/utility`, `/inventory`, `/history`, `/audit`, `/data-requests` |
| Imports/reports | `POST /analyze`, `POST /compare`, `GET /reports/<filename>` |
| API | `/api/files`, `/api/supported-feeds`, `/api/utility-by-zip`, `/api/day-detail`, `POST /api/analyze` |
| Billing | `POST /billing/checkout`, `/billing/success`, `/billing/cancel`, `POST /billing/portal`, `POST /stripe/webhook` |
| Operations | `/health`, `/robots.txt`, `/sitemap.xml` |

Routes are declared inside `create_app()`. Use the route-level authorization helper before loading account data or changing state.

## Safe change recipes

### Customer-facing copy or layout

Update the relevant template and stylesheet. Keep public marketing copy and signed-in app copy aligned when they describe the same workflow. Add a route-render assertion in `tests/test_app.py` for important labels or links.

### Account field or table

Update both database migration functions, the save/load serializer, the form, authorization checks, and the customer/staff view. Test valid updates, invalid input, unauthorized access, and preservation of existing values.

### State-changing route

Use `POST`, keep CSRF enabled, validate input server-side, call `require_account_actor(..., write=True)` or the appropriate staff helper, write an audit event, then redirect through `redirect_back_or_account()` where appropriate.

### Import behavior

Test first upload, overlapping upload, exact re-upload, and conflicting reading preservation. Never key deduplication only on filename.

## Production deployment

Production is **one EC2 instance running one Docker container supervised by systemd**, backed by RDS Postgres. It is not ECS/Fargate.

Production packages must come from a clean local `main` branch whose commit exactly matches `origin/main`. The deployment helper archives the Git commit itself, so local uncommitted and untracked files are never deployed.

Read `deploy/ec2/README.md` before changing deployment. The canonical runtime files are:

- `deploy/ec2/home-energy-watch.service`
- `deploy/ec2/home-energy-watch-direct.sh`
- `deploy/ec2/.env.production` on the host, never committed

The authorized AWS profile is `shawn-admin`:

```bash
AWS_PROFILE=shawn-admin AWS_REGION=us-east-1 \
  aws ec2 describe-instances \
  --filters 'Name=tag:Name,Values=home-energy-watch*' \
  'Name=instance-state-name,Values=running'
```

After deployment, verify `curl -fsS https://app.homeenergywatch.com/health` and the changed user flow. Preserve the production env file, RDS data, input volume, and output volume. Do not deploy a local env file, database, customer export, or secret-bearing archive.

## Before opening a change

```bash
PYTHONPATH=. uv run pytest
PYTHONPATH=. uv run python -m py_compile app.py tests/test_app.py
git diff --check
```

For import changes, include fixture tests. For auth, billing, email, database, or deployment changes, run the narrow regression test and then the full suite. Report separately what is locally verified, deployed to staging, and publicly live.
