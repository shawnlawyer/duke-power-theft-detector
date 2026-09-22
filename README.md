# Home Energy Watch

Home Energy Watch is a Flask web app and CLI for reviewing home electric interval history. It stores customer-scoped accounts, household context, utility imports, notes, reports, billing state, and tamper-evident audit events. It can read Green Button ESPI XML, Duke-style interval XML, and utility interval CSV files, then flag overnight load patterns that deserve review.

The app is a review and recordkeeping tool. A flagged interval is a prompt for investigation, not proof of theft or tampering.

## Repository

Clone with SSH:

```bash
git clone git@github.com:shawnlawyer/duke-power-theft-detector.git
cd duke-power-theft-detector
```

Clone with HTTPS:

```bash
git clone https://github.com/shawnlawyer/duke-power-theft-detector.git
cd duke-power-theft-detector
```

You can also download the repository ZIP from GitHub, unzip it, and run the same setup commands from the extracted folder.

Start with [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) when changing behavior. EC2 production details live under [deploy/ec2](deploy/ec2).

## Stack

| Layer | Actual implementation |
| --- | --- |
| App runtime | Python 3.11, Flask 3 |
| Production WSGI server | Gunicorn |
| Data analysis | pandas, lxml, python-dateutil |
| Authentication | Flask sessions, one-time email sign-in links, legacy operator password hashing, passkeys through `fido2`, staff MFA through `pyotp` |
| Local database | SQLite at `POWER_DB_PATH`, defaulting to `data/output/power-history.db` when configured for local use |
| Production database | Postgres/RDS through `POWER_DATABASE_URL`, with TLS required in production |
| Billing | Backend Stripe Checkout, Billing Portal, and webhooks |
| Email | Disabled or memory backend locally, Amazon SES in production |
| Container | Docker image from [Dockerfile](Dockerfile), Python base image pinned by digest, hashed dependency lock |
| Local orchestration | `docker compose` from [docker-compose.yml](docker-compose.yml) |
| Production target | One EC2 instance running one Docker container supervised by `systemd`, backed by RDS Postgres |
| Tests | `pytest` under [tests](tests) |

There is no `package.json`, `pyproject.toml`, or npm package script in this repository. Useful project scripts are the Python entry point and the shell scripts under [scripts](scripts).

## Prerequisites

Local development:

- Python 3.11
- `pip` and `venv`
- Docker and Docker Compose
- Git
- `curl`, `tar`, and a POSIX shell

Production operations:

- AWS CLI access for the deployment account when discovering EC2/RDS resources
- SSH access to the EC2 instance
- Docker, `rsync`, `tar`, `curl`, `sudo`, and `systemd` on the EC2 host
- RDS Postgres reachable from EC2 on port `5432`
- TLS in front of the container through a reverse proxy or load balancer
- A production env file on the EC2 host at `deploy/ec2/.env.production`

## Local Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create local data folders:

```bash
mkdir -p data/input data/output
```

Use local SQLite paths when running the app directly:

```bash
export POWER_INPUT_DIR="$PWD/data/input"
export POWER_OUTPUT_DIR="$PWD/data/output"
export POWER_DB_PATH="$PWD/data/output/power-history.db"
export POWER_ENV=development
export POWER_APP_SECRET=local-power-data-only
export POWER_EMAIL_BACKEND=disabled
export POWER_BILLING_ENABLED=false
```

Run the web app directly:

```bash
PYTHONPATH=. python app.py --serve --host 127.0.0.1 --port 8000
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

Run the Docker stack:

```bash
docker compose up --build
```

Then open [http://localhost:8001](http://localhost:8001).

Local Docker keeps uploaded files in `./data/input`, generated reports in `./data/output`, and SQLite data at `./data/output/power-history.db`.

## Environment Variables

The app reads configuration from environment variables. Do not commit real values, production database URLs, secret keys, Stripe secrets, SES credentials, customer data, or generated production env files.

| Variable | Purpose |
| --- | --- |
| `POWER_ENV` | Runtime mode. Use `development`, `test`, `staging`, or `production`. |
| `POWER_INPUT_DIR` | Uploaded interval file storage. |
| `POWER_OUTPUT_DIR` | Generated CSV, JSON, Markdown, and database output storage. |
| `POWER_DB_PATH` | SQLite path when Postgres is not configured. |
| `POWER_DATABASE_URL` | Postgres URL for production, or optional SQLite URL. Production requires `postgresql://` with TLS such as `?sslmode=require`. |
| `POWER_APP_SECRET` | Flask session secret. Production requires a unique value of at least 32 characters. |
| `POWER_AUDIT_SIGNING_KEY` | Stable signing key for audit-chain integrity. Keep stable across deploys. |
| `POWER_DATA_ENCRYPTION_KEY` | Fernet key for sensitive utility-connection fields. |
| `POWER_PUBLIC_BASE_URL` | App base URL, for example `https://app.homeenergywatch.com`. |
| `POWER_MARKETING_BASE_URL` | Marketing-site base URL, for example `https://homeenergywatch.com`. |
| `POWER_APP_HOSTS` | Optional app host allow-list. |
| `POWER_MARKETING_HOSTS` | Optional marketing host allow-list. |
| `POWER_TRUST_PROXY` | Set `true` only when behind the configured reverse proxy or load balancer. |
| `POWER_LOG_LEVEL` | App log level, commonly `INFO`. |
| `POWER_LOG_FORMAT` | `json` or `text`; production defaults toward JSON logs. |
| `POWER_STAFF_MFA_REQUIRED` | Requires enrolled staff MFA when true. |
| `POWER_DATA_DELETION_ENABLED` | Enables approved deletion execution. Keep false until a retention policy is approved. |
| `POWER_DATA_DELETION_POLICY_VERSION` | Required when deletion execution is enabled. |
| `POWER_EMAIL_BACKEND` | `disabled`, `memory`, or `ses`. Production uses `ses`. |
| `POWER_EMAIL_FROM` | Verified sender for SES. |
| `POWER_EMAIL_REPLY_TO` | Optional reply-to address. |
| `POWER_EMAIL_REGION` | SES region, commonly `us-east-1`. |
| `POWER_BILLING_ENABLED` | Opens Stripe Checkout only when true and all Stripe values are installed. |
| `STRIPE_ACCOUNT_ID` | Stripe account identifier for metadata and operator reference. |
| `STRIPE_SECRET_KEY` | Backend-only Stripe secret key. |
| `STRIPE_WEBHOOK_SECRET` | Backend-only webhook signing secret. |
| `STRIPE_API_VERSION` | Stripe API version. |
| `STRIPE_PRICE_HOME` | Annual Stripe Price ID for one electric account. |
| `POWER_TIMEZONE` | Default analysis timezone. |
| `POWER_NIGHT_START` | Default overnight window start. |
| `POWER_NIGHT_END` | Default overnight window end. |
| `POWER_MIN_NIGHT_KW` | Minimum average overnight kW to flag. |
| `POWER_NIGHT_MULTIPLIER` | Baseline multiplier used for overnight flags. |
| `POWER_ALERT_WINDOW_START` | Alert-event window start. |
| `POWER_ALERT_WINDOW_END` | Alert-event window end. |
| `POWER_ALERT_MIN_KW` | Minimum kW for alert events. |
| `POWER_ALERT_MULTIPLIER` | Alert-event multiplier. |
| `POWER_ALERT_JUMP_KW` | Alert-event jump threshold. |
| `POWER_WEB_PORT` | Host port used by Docker Compose or production launch scripts. |
| `POWER_WEB_CONCURRENCY` | Gunicorn worker count. |
| `POWER_GUNICORN_TIMEOUT` | Gunicorn timeout in seconds. |

Generate a Fernet key for `POWER_DATA_ENCRYPTION_KEY`:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Database

No separate migration CLI exists. `ensure_database()` selects SQLite or Postgres from the environment, and the app creates or migrates its schema at startup through `migrate_database()` and `migrate_database_postgres()`.

Local SQLite:

```bash
mkdir -p data/output
export POWER_DB_PATH="$PWD/data/output/power-history.db"
PYTHONPATH=. python app.py --serve --host 127.0.0.1 --port 8000
```

Production Postgres:

- Set `POWER_DATABASE_URL` to the RDS Postgres URL.
- Require TLS with `?sslmode=require` or stricter certificate verification.
- Keep RDS private, encrypted at rest, backed up, and deletion-protected.
- Do not point tests at production.

There is no seed command for general app data. The first staff user is created through `/first-run` when no staff users exist. A bounded owner free-play Stripe code can be checked or created from the backend only:

```bash
PYTHONPATH=. python app.py --ensure-owner-free-play-code
```

Do not create live charges, refunds, subscriptions, or provider configuration changes without explicit approval.

## Build, Test, And Verification

Run the full test suite:

```bash
PYTHONPATH=. python -m pytest
```

Run syntax checks:

```bash
PYTHONPATH=. python -m py_compile app.py tests/test_app.py tests/test_omen_deploy_contract.py tests/test_release_security_contract.py tests/test_production_deploy_contract.py
bash -n scripts/deploy-production.sh
```

Run the whitespace diff check:

```bash
git diff --check
```

Build the production Docker image locally:

```bash
docker build -t home-energy-watch:local .
```

Run the release security scan:

```bash
./scripts/security-scan.sh
```

Run the new production package flow without deploying:

```bash
./scripts/deploy-production.sh package
```

The deployment script uses `.venv/bin/python` when that file exists, otherwise `python3`. Override it with `PYTHON_BIN=/path/to/python` if needed.

Skip tests only when you have already run them in the same change window:

```bash
./scripts/deploy-production.sh package --skip-tests
```

## Useful Commands

| Command | Purpose |
| --- | --- |
| `PYTHONPATH=. python app.py --serve --host 127.0.0.1 --port 8000` | Run the Flask development server directly. |
| `docker compose up --build` | Run the app locally in Docker on `localhost:8001`. |
| `PYTHONPATH=. python app.py --input path/to/export.xml --output report.csv` | Analyze one supported utility export from the CLI. |
| `PYTHONPATH=. python app.py --input old.xml --compare-to new.xml --output compare.md` | Compare two supported exports. |
| `PYTHONPATH=. python app.py --sync-utilities` | Run saved utility-connection sync once. |
| `PYTHONPATH=. python app.py --sync-utilities --account-number primary` | Sync one saved account connection set. |
| `PYTHONPATH=. python app.py --ensure-owner-free-play-code` | Reuse or create the bounded owner test Stripe promotion code. |
| `PYTHONPATH=. python -m pytest` | Run tests. |
| `./scripts/security-scan.sh` | Build, test, pip-audit, and Trivy scan the image. |
| `./scripts/omen-deploy.sh all` | Deploy to the local-network Omen staging server. |
| `./scripts/deploy-production.sh check` | Run local release checks, plus remote prerequisite checks if `PRODUCTION_SSH_TARGET` is set. |
| `./scripts/deploy-production.sh package` | Build and archive the clean, merged `main` commit without changing production. |
| `./scripts/deploy-production.sh deploy --confirm-production` | Deploy to the configured EC2 production host. |
| `./scripts/deploy-production.sh rollback --confirm-production` | Roll back production to the latest backup. |

## Supported Utility Feeds

The app normalizes interval history into one internal model through dedicated adapters:

- Green Button ESPI XML
- Duke-style interval XML
- Utility interval CSV

Manual upload remains available for every supported feed. Duke customers can also choose an automatic connection built on `aiodukeenergy` and its open-source Duke Energy OAuth Helper:

1. Download and unzip the helper from the signed-in Utility page.
2. Load the unpacked extension from `chrome://extensions` with Developer mode enabled.
3. Start Duke sign-in from Home Energy Watch and finish Duke's own sign-in and verification steps.
4. Paste the one-time code shown by the helper back into Home Energy Watch.
5. Run **Sync now** once if an immediate refresh is needed. The production utility-sync schedule handles later refreshes.

The app never asks for or stores the customer's Duke password. OAuth tokens are encrypted with the existing data-encryption boundary. Each update imports completed hourly readings, rechecks the most recent 30 days for utility corrections, and uses the same non-destructive reading identity as file uploads. The helper uses Duke's mobile-app OAuth path and is not an official Green Button connection, so the UI must continue to offer manual download and upload and must explain that Duke may require reconnection after sign-in changes.

## API And Route Documentation

Most state-changing routes are browser form endpoints protected by session auth and CSRF. JSON API routes also use the current session. Stripe webhooks are the intentional CSRF exception and require Stripe signature verification.

Auth labels:

- `Public`: no sign-in required.
- `Customer`: signed-in customer session.
- `Staff`: signed-in staff session.
- `Commissioner`: signed-in staff user with the `Commissioner` role.
- `Account actor`: staff session, or a signed-in customer with access to the requested account. Write operations require customer `Manager` access.
- `Stripe`: Stripe webhook signature using `STRIPE_WEBHOOK_SECRET`.

### Public And Operations

| Method | Path | Purpose | Auth | Request | Response |
| --- | --- | --- | --- | --- | --- |
| `GET` | `/health` | Confirms the app can initialize the database. | Public | None | JSON: `{"status":"ok"}` |
| `GET` | `/robots.txt` | Allows marketing pages and blocks app pages based on host. | Public | None | `text/plain` robots file |
| `GET` | `/sitemap.xml` | Marketing sitemap. | Public | None | XML sitemap |
| `GET` | `/`, `/pricing`, `/how-it-works`, `/for-homeowners`, `/for-commissions`, `/terms`, `/privacy`, `/utility-data-authorization` | Public marketing and policy pages. | Public | None | HTML |
| `GET` | `/first-run` | First staff setup screen when no staff user exists. | Public until bootstrap | None | HTML or redirect |
| `POST` | `/first-run` | Creates the first staff user. | Public until bootstrap, CSRF | Form: `email`, `full_name`, `password` | Redirect |

### Auth And Identity

| Method | Path | Purpose | Auth | Request | Response |
| --- | --- | --- | --- | --- | --- |
| `GET` | `/signup` | Customer signup page. | Public | Optional plan/account query values | HTML or redirect |
| `POST` | `/signup` | Creates a customer identity, one electric account, its annual billing record, and utility data authorization. | Public, CSRF | Form includes `email`, `full_name`, `account_number`, household fields, policy confirmations, and plan fields | HTML verification notice or redirect |
| `GET` | `/login` | Unified staff/customer login page. | Public | Optional `next` | HTML or redirect |
| `POST` | `/login` | Sends a one-time sign-in link for a customer or eligible commission email. | Public, CSRF | Form: `email`, optional `next` | HTML notice, or `429` on rate limit |
| `GET` | `/login/token` | Consumes a one-time sign-in link. | Public with token | Query: `token` | Redirect |
| `POST` | `/login/passkey/start`, `/customer/login/passkey/start` | Starts passkey sign-in. | Public, CSRF | Form: `email`, optional `next` | JSON: `{"publicKey": ...}` or `{"error": ...}` |
| `POST` | `/login/passkey/finish`, `/customer/login/passkey/finish` | Completes passkey sign-in. | Public, CSRF | JSON passkey assertion payload | JSON: `{"redirect": "/..."}` or `{"error": ...}` |
| `POST` | `/logout` | Ends current staff or customer session. | Signed-in user, CSRF | Form with CSRF token | Redirect |
| `GET` | `/forgot-password`, `/customer/forgot-password`, `/staff/forgot-password` | Password reset request pages. | Public | None | HTML |
| `POST` | `/forgot-password`, `/customer/forgot-password`, `/staff/forgot-password` | Sends password reset email when available. | Public, CSRF | Form: `email` | HTML, with `429` on rate limit |
| `GET` | `/customer/verify-email`, `/customer/reset-password`, `/staff/reset-password` | Token verification/reset screens. | Public with token | Query: `token` | HTML |
| `POST` | `/customer/verify-email` | Confirms customer email. | Public with token, CSRF | Form: `token` | Redirect |
| `POST` | `/customer/reset-password`, `/staff/reset-password` | Changes password with a valid token. | Public with token, CSRF | Form: `token`, `password`, `password_confirm` | Redirect or HTML error |

### Customer Account Routes

| Method | Path | Purpose | Auth | Request | Response |
| --- | --- | --- | --- | --- | --- |
| `GET` | `/customer`, `/customer/account`, `/customer/utility`, `/customer/inventory`, `/customer/history`, `/customer/billing` | Customer dashboard and setup sections. | Customer | Optional `account_number`, paging/search query values | HTML |
| `POST` | `/customer/passkeys/start` | Starts customer passkey enrollment. | Customer, CSRF | Form: optional `nickname` | JSON: `{"publicKey": ...}` |
| `POST` | `/customer/passkeys/finish` | Completes customer passkey enrollment. | Customer, CSRF | JSON passkey attestation payload | JSON: `{"saved": true, "credential": {...}}` |
| `POST` | `/customer/passkeys/<credential_id>/delete` | Deletes a customer passkey. | Customer, CSRF | Form with CSRF token | Redirect |
| `GET` | `/customer/data-export.zip` | Downloads a tenant-bounded ZIP archive of the customer's account data. | Customer | None | ZIP file, `Cache-Control: no-store` |
| `GET` | `/customer/data-requests` | Lists customer deletion requests. | Customer | None | HTML |
| `POST` | `/customer/data-requests` | Requests deletion review for an account. | Customer, CSRF | Form: `account_number`, `confirm_deletion_request` | Redirect |
| `POST` | `/customer/data-requests/<request_id>/cancel` | Cancels a pending customer deletion request. | Customer, CSRF | Form with CSRF token | Redirect |

### Staff And Audit Routes

| Method | Path | Purpose | Auth | Request | Response |
| --- | --- | --- | --- | --- | --- |
| `GET` | `/`, `/account`, `/people`, `/utility`, `/inventory`, `/history`, `/staff` | Staff review and setup pages. | Staff | Optional account/search query values | HTML |
| `GET` | `/audit`, `/data-requests` | Audit and data request operations pages. | Commissioner | Optional filters | HTML |
| `GET` | `/audit/export.csv` | Exports audit events after integrity verification. | Commissioner | Optional `account_number`, `action` | CSV, or `409` text if integrity fails |
| `POST` | `/staff/invite` | Invites a staff user. | Commissioner, CSRF | Form: `email`, `full_name`, `role`, optional `return_to` | Redirect |
| `GET`/`POST` | `/staff/setup/<token>` | Accepts a staff invitation. | Public with invite token, CSRF on POST | Form: `password`, optional `full_name` | HTML or redirect |
| `POST` | `/staff/<staff_user_id>/access` | Updates role or active status. | Commissioner, CSRF | Form: `role`, `status` | Redirect |
| `GET`/`POST` | `/staff/<staff_user_id>/mfa/reset` | Resets another staff user's MFA. | Commissioner, CSRF on POST | Form with CSRF token | HTML or redirect |
| `GET` | `/staff/security` | Staff MFA and passkey management. | Staff | None | HTML |
| `POST` | `/staff/security/mfa/start`, `/staff/security/mfa/confirm`, `/staff/security/mfa/cancel`, `/staff/security/mfa/recovery-codes`, `/staff/security/mfa/disable` | Manages staff MFA. | Staff, CSRF | Forms include `code` or `password` where required | Redirect or HTML recovery-code page |
| `POST` | `/staff/security/passkeys/start` | Starts staff passkey enrollment. | Staff, CSRF | Form: optional `nickname` | JSON: `{"publicKey": ...}` |
| `POST` | `/staff/security/passkeys/finish` | Completes staff passkey enrollment. | Staff, CSRF | JSON passkey attestation payload | JSON: `{"saved": true, "credential": {...}}` |
| `POST` | `/staff/security/passkeys/<credential_id>/delete` | Deletes a staff passkey. | Staff, CSRF | Form with CSRF token | Redirect |
| `POST` | `/data-requests/<request_id>/review` | Approves or rejects an account deletion request. | Commissioner, CSRF | Form: `decision`, optional `review_note` | Redirect |
| `POST` | `/data-requests/<request_id>/execute` | Executes an approved deletion request when deletion execution is enabled. | Commissioner, CSRF | Form: `password` | Redirect |
| `POST` | `/data-holds`, `/data-holds/<hold_id>/release` | Places or releases legal holds. | Commissioner, CSRF | Form: `account_number`, `reason` for create | Redirect |

### Account, Utility, History, And Reports

| Method | Path | Purpose | Auth | Request | Response |
| --- | --- | --- | --- | --- | --- |
| `POST` | `/account` | Creates or updates account and household profile data. | Account actor, write, CSRF | Form: `account_number`, optional `original_account_number`, `display_name`, service address/ZIP, baseline and household fields | Redirect |
| `POST` | `/account-access` | Grants customer access to an account. | Staff, CSRF | Form: `account_number`, `email`, optional `full_name`, `access_level` | Redirect |
| `POST` | `/account-access/<access_id>/delete` | Revokes account access. | Staff, CSRF | Form: `account_number` | Redirect |
| `POST` | `/account/data-authorization` | Grants customer utility data authorization. | Customer manager, CSRF | Form: `account_number`, `confirm_data_authorization` | Redirect or `403` |
| `POST` | `/account/data-authorization/revoke` | Revokes customer utility data authorization and clears saved utility access. | Customer manager, CSRF | Form: `account_number` | Redirect or `403` |
| `POST` | `/account/notes` | Adds a customer note to an account. | Customer manager, CSRF | Form: `account_number`, `note_date`, `body` | Redirect |
| `POST` | `/account/notes/<note_id>/delete` | Deletes a customer note. | Customer manager, CSRF | Form: `account_number` | Redirect |
| `POST` | `/utility-connection` | Saves a customer-approved utility connection record. | Account actor, write, CSRF | Form: `account_number`, `connection_label`, `access_method`; provider is resolved from the account | Redirect |
| `POST` | `/utility-connection/<connection_id>/delete` | Deletes a utility connection. | Account actor, write, CSRF | Form: `account_number` | Redirect |
| `POST` | `/utility-connection/<connection_id>/sync` | Syncs one saved utility connection. | Account actor, write, CSRF | Form: `account_number` | Redirect |
| `POST` | `/load-items` | Adds an inventory/load item. | Account actor, write, CSRF | Form: `account_number`, `label`, `quantity`, `watts_each`, optional `include_when_off`, `notes` | Redirect |
| `POST` | `/load-items/<item_id>/delete` | Deletes a load item. | Account actor, write, CSRF | Form: `account_number` | Redirect |
| `GET` | `/analyze` | Legacy navigation route to the correct history page. | Staff or customer | Optional `account_number` | Redirect |
| `POST` | `/analyze` | Uploads one supported interval file, imports it, analyzes saved history, and renders a report. | Account actor, write, CSRF | Multipart form: `xml_file`, `account_number`, optional display/profile/settings fields | HTML report |
| `POST` | `/compare` | Uploads two supported interval files and creates Markdown plus CSV comparison artifacts. | Account actor, write, CSRF | Multipart form: `left_file`, `right_file`, `account_number`, optional settings | HTML comparison report |
| `GET` | `/reports/<filename>` | Downloads generated `.csv`, `.json`, or `.md` reports. | Staff, or customer with access to the artifact account | Path filename only | Attachment download or `404` |

### JSON API

| Method | Path | Purpose | Auth | Request | Response |
| --- | --- | --- | --- | --- | --- |
| `GET` | `/api/files` | Lists uploaded input files and generated reports. | Staff | None | JSON: `{"input_files": [...], "report_files": [...]}` |
| `GET` | `/api/supported-feeds` | Lists utility feed adapters. | Public | None | JSON: `{"supported_feeds": [{"adapter_id", "display_name", "provider_label", "standard_label", "format_label", "file_types", "customer_label", "customer_note", "status"}]}` |
| `GET` | `/api/utility-by-zip` | Looks up the electric provider for a ZIP/address. | Public | Query: `zip_code`, optional `address` | JSON provider match, usually with `energy_company`, `eia_utility_id`, `zip_code`, `match_address`, `match_basis`; `400` or `503` on failure |
| `GET` | `/api/day-detail` | Returns analysis detail for one account day. | Account actor | Query: `account_number`, `date`, optional `tz`, `night_start`, `night_end`, `min_night_kw`, `night_multiplier` | JSON with `date`, `label`, `current_day`, `previous_day`, `baseline_day`, delta summaries, `series`, `alert_events`, `top_jumps`, `notes`, `load_summary`, `load_overview`, `inventory_alignment`, `baseline_kw`, and `weather`; `404` when unavailable |
| `POST` | `/api/analyze` | Imports a mounted input file or uploaded file, analyzes saved history, and returns the report context. | Account actor, write, CSRF | JSON: `account_number`, optional `input_file`, display/profile/settings fields; or multipart form with `xml_file` | JSON report context including summary rows, suspicious rows, downloads, account, load inventory, import result, and initial day detail; `403` when data authorization is missing |

### Billing

| Method | Path | Purpose | Auth | Request | Response |
| --- | --- | --- | --- | --- | --- |
| `POST` | `/billing/checkout` | Creates a Stripe Checkout session for the selected plan. | Customer, CSRF | Form: `plan_id` | Redirect to Stripe Checkout or pricing page |
| `GET` | `/billing/success` | Refreshes billing from Stripe after Checkout. | Customer | Query: `session_id` | Redirect to customer dashboard |
| `GET` | `/billing/cancel` | Handles canceled Checkout. | Public or customer | None | Redirect |
| `POST` | `/billing/portal` | Creates a Stripe Billing Portal session. | Customer, CSRF | Form with CSRF token | Redirect to Stripe Billing Portal |
| `POST` | `/stripe/webhook` | Processes Stripe billing events. | Stripe signature | Raw Stripe event body with `Stripe-Signature` header | JSON: `{"received": true}` or `{"error": "..."}` with `400` |

## CLI

Single-history analysis:

```bash
PYTHONPATH=. python app.py --input path/to/utility-export.xml --output report.csv
```

The command writes a CSV plus a same-stem JSON case file. The JSON artifact includes the input file name, settings, baseline, suspicious-day ranking, weather context when available, alert events, and per-day summaries.

Compare two exports:

```bash
PYTHONPATH=. python app.py \
  --input path/to/earlier-export.xml \
  --compare-to path/to/later-export.xml \
  --output compare.md
```

Use `.csv` as the output suffix when you want aligned comparison rows instead of Markdown.

Utility sync:

```bash
PYTHONPATH=. python app.py --sync-utilities
PYTHONPATH=. python app.py --sync-utilities --account-number primary
```

The sync command exits `0` when every saved connection succeeds and exits `1` when any connection fails. Each connection records its own latest status in the app.

Useful analysis flags:

- `--tz America/New_York`
- `--night-start 02:00`
- `--night-end 04:00`
- `--min-night-kw 1.0`
- `--night-multiplier 2.0`

## Production Deployment

Do not deploy to production unless the person responsible for production has explicitly approved that production-changing action.

Production uses:

- EC2 host checkout at `/home/ubuntu/home-energy-watch`
- production env file at `/home/ubuntu/home-energy-watch/deploy/ec2/.env.production`
- Docker image tag `home-energy-watch:latest`
- systemd service `home-energy-watch`
- public health check `https://app.homeenergywatch.com/health`

The new deployment script is [scripts/deploy-production.sh](scripts/deploy-production.sh). It does not print production env values. It excludes data directories, local env files, `deploy/ec2/.env.production`, git metadata, virtualenvs, and temporary files from the archive.

Configure the remote target in your shell:

```bash
export PRODUCTION_SSH_TARGET=ubuntu@your-ec2-hostname-or-ip
export PRODUCTION_SSH_KEY="$HOME/.ssh/your-production-key"
```

Optional overrides:

```bash
export PRODUCTION_REMOTE_ROOT=/home/ubuntu/home-energy-watch
export PRODUCTION_REMOTE_BACKUP_DIR=/home/ubuntu/home-energy-watch-backups
export PRODUCTION_REMOTE_RELEASE_DIR=/home/ubuntu/home-energy-watch-releases
export PRODUCTION_SERVICE=home-energy-watch
export PRODUCTION_RELEASE_BRANCH=main
export PRODUCTION_RELEASE_REMOTE_REF=origin/main
export PRODUCTION_HEALTH_URL=https://app.homeenergywatch.com/health
export PYTHON_BIN="$PWD/.venv/bin/python"
```

Check locally without changing production:

```bash
./scripts/deploy-production.sh check
```

Create a deployable package without changing production:

```bash
./scripts/deploy-production.sh package
```

Packaging and deployment require a clean local `main` branch whose commit exactly matches `origin/main`. The archive is created from that merged commit, so uncommitted and untracked files cannot enter production.

Deploy only after explicit production approval:

```bash
./scripts/deploy-production.sh deploy --confirm-production
```

Deploy flow:

1. Runs syntax checks.
2. Runs tests unless `--skip-tests` is supplied.
3. Runs `git diff --check`.
4. Builds the Docker image from the repo Dockerfile.
5. Creates a source archive from the merged Git commit under `.tmp-deploy`.
6. Copies the archive to the EC2 host over SSH.
7. Backs up the current production release to the remote backup directory.
8. Replaces application source while preserving `deploy/ec2/.env.production`.
9. Builds `home-energy-watch:latest` on EC2.
10. Restarts only the `home-energy-watch` systemd service.
11. Verifies `PRODUCTION_HEALTH_URL`.

Use `--skip-tests` only when tests were already run for the exact source being deployed:

```bash
./scripts/deploy-production.sh deploy --skip-tests --confirm-production
```

## Rollback

Roll back to the latest backup:

```bash
./scripts/deploy-production.sh rollback --confirm-production
```

Roll back to a named backup:

```bash
./scripts/deploy-production.sh rollback --confirm-production --backup home-energy-watch-YYYYMMDDTHHMMSSZ.tgz
```

Rollback flow:

1. Confirms the production env file is present.
2. Selects the requested backup, or the newest backup when none is provided.
3. Creates a pre-rollback backup of the current release.
4. Restores source from the selected backup while preserving `deploy/ec2/.env.production`.
5. Rebuilds `home-energy-watch:latest`.
6. Restarts only the `home-energy-watch` systemd service.
7. Verifies `PRODUCTION_HEALTH_URL`.

Rollback does not restore RDS data, input uploads, or output reports. Those are separate operational assets and need their own recovery procedure.

## Staging On Omen

Omen is the local-network staging server documented in [OMEN_DEPLOY.md](OMEN_DEPLOY.md).

```bash
./scripts/omen-deploy.sh all
./scripts/omen-deploy.sh check
./scripts/omen-deploy.sh logs
```

Omen uses `POWER_ENV=staging`, local Docker, and its own ignored `.env.omen` file. Omen validation does not prove production auth, RDS, SES, Stripe, DNS, TLS, or public URL health.

## Security And Operations Notes

- Do not commit secrets, production database URLs, customer exports, uploaded utility files, generated reports containing customer data, Stripe secrets, SES credentials, or production env files.
- Do not print production env values while debugging. Print variable names, service status, request IDs, and redacted URLs instead.
- Do not run `deploy` or `rollback` without `--confirm-production`.
- Keep local validation, package creation, deployed runtime health, database migrations/writes, and public URL verification separate when reporting status.
- Local tests prove local behavior only. A Docker build proves the image builds. A successful deploy script proves the EC2 service restarted and `/health` answered. It does not prove billing, email delivery, DNS propagation, or every user flow.
- Production startup intentionally fails closed when required production secrets, HTTPS base URLs, data encryption, SES settings, or Postgres TLS requirements are unsafe.
- Keep `POWER_DATA_DELETION_ENABLED=false` until an approved retention policy exists. Enabling deletion execution also requires `POWER_DATA_DELETION_POLICY_VERSION`.
- Keep `POWER_BILLING_ENABLED=false` until matching Stripe Price IDs and webhook settings are installed and verified.
- Preserve the audit-signing key across deploys so existing audit records remain verifiable.
- Keep RDS private, encrypted, backed up, and protected from deletion.
- Use [deploy/ec2/INCIDENT_RESPONSE.md](deploy/ec2/INCIDENT_RESPONSE.md) during incidents and capture evidence before changing production state.
