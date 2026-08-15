# Home Energy Watch Developer Guide

This is the shortest path to making a safe change in Home Energy Watch. The application is concentrated in one Flask service, but its responsibilities have stable boundaries. Start here before reading all of `app.py`.

## Product boundary

Home Energy Watch stores account-scoped electricity history, household context, notes, weather, reports, and review activity. It supports manual utility exports and customer-approved utility connections where an official feed exists.

It does **not** log into Duke on behalf of a customer, scrape passwords, capture mobile-app callbacks, or imply that automatic utility refresh is available nationwide. Manual setup is the supported path for every U.S. household.

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

Customer and staff identities use separate database records, but the public `/login` flow resolves both through shared sign-in logic. Password reset starts at `/forgot-password`; the legacy `/customer/forgot-password` and `/staff/forgot-password` endpoints remain available for direct links.

Use the existing helpers inside `create_app()`:

- `current_customer_user()` and `current_staff_user()` validate sessions;
- `require_customer_user()` and `require_staff_user()` protect page/API access;
- `require_account_actor()` checks account access level and write permission;
- `require_commissioner()` protects commissioner-only operations.

Customer writes must be scoped to an account access record. Account notes, inventory, utility connections, imports, and reports must never be loaded by account number alone without an authorization check. Keep CSRF protection on state-changing forms; Stripe webhooks are the intentional CSRF exception.

Passkeys, password resets, email verification, rate limits, staff MFA, and audit events already have persistence and tests. Extend those paths rather than creating a second authentication mechanism.

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

- Home Watch: `$19.99/month`
- Review Desk: `$99/month`

Subscribers can create unlimited reports while active. Required production variables include `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_HOME`, `STRIPE_PRICE_REVIEW`, and `POWER_BILLING_ENABLED`. Keep billing closed until matching Price IDs are installed and verified. Do not create live charges, subscriptions, refunds, or provider configuration changes without explicit approval.

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
