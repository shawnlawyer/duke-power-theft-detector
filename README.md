# PowerPattern

Home Energy Watch is a home energy audit and review tool. It keeps customer history in one place, compares meter readings against household load expectations, and helps surface spikes that do not fit the home.

## Supported utility feeds

The app normalizes interval history into one internal model, then lets each utility feed plug into that model through a dedicated adapter.

Current adapters:

- Green Button ESPI XML
- Duke-style interval XML
- Utility interval CSV

That keeps the alerting, reporting, household baseline, and load-test logic shared across feeds instead of baking Duke-specific parsing rules into the rest of the app.

## Duke and Green Button access

Home Energy Watch supports three utility-data paths:

- Duke file download: the customer signs in at `https://www.duke-energy.com/my-account/sign-in`, downloads detailed usage history, and uploads the file on the History page.
- Green Button Connect: the app is ready for a customer-approved utility feed when Duke or another utility provides an official third-party connection path.
- North Carolina data access: the app tracks the NCUC data-access order and keeps direct sync separate from manual upload until the utility registration process is available.

Do not use browser extensions, password scraping, or mobile-app callback capture for Duke sync. Those approaches are research-only and are not part of the customer-facing product.

## Local Docker

```bash
docker compose up --build
```

Then open [http://localhost:8001](http://localhost:8001).

Local Docker keeps uploaded history in `./data/input`, generated CSV, JSON, and comparison exports in `./data/output`, and uses SQLite by default at `./data/output/power-history.db`.

Run a scheduled utility sync job against the same Docker volumes and database with:

```bash
docker compose run --rm power-detector python app.py --sync-utilities
```

To sync one saved account connection set, add the account number:

```bash
docker compose run --rm power-detector python app.py --sync-utilities --account-number primary
```

The command exits `0` when every saved connection syncs cleanly and exits `1` when one or more connections fail. Each connection keeps its own latest success or failure status in the app.

## Production on EC2 and RDS

The app now supports either:

- local SQLite for one-machine use
- Postgres through `POWER_DATABASE_URL` for RDS-backed production

Production deployment files live in `deploy/ec2/README.md`, `deploy/ec2/docker-compose.prod.yml`, and `deploy/ec2/.env.production.example`.

The straight-to-production shape is:

- one EC2 instance running Docker
- one RDS Postgres database
- `homeenergywatch.com` for the public marketing pages
- `app.homeenergywatch.com` for the working app

## Key environment settings

- `POWER_DATABASE_URL`
- `POWER_APP_SECRET`
- `POWER_PUBLIC_BASE_URL`
- `POWER_MARKETING_BASE_URL`
- `POWER_APP_HOSTS`
- `POWER_MARKETING_HOSTS`
- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_PRICE_HOME`
- `STRIPE_PRICE_REVIEW`
- `STRIPE_PRICE_AGENCY`
- `POWER_WEB_PORT`
- `POWER_TIMEZONE`
- `POWER_WEB_CONCURRENCY`
- `POWER_GUNICORN_TIMEOUT`

When `POWER_DATABASE_URL` is not set, the app uses the local SQLite file path from `POWER_DB_PATH`.

For production, set:

- `POWER_PUBLIC_BASE_URL=https://app.homeenergywatch.com`
- `POWER_MARKETING_BASE_URL=https://homeenergywatch.com`

The same Flask service can answer both hostnames. When the request host is `homeenergywatch.com`, the app serves the public marketing pages. When the request host is `app.homeenergywatch.com`, it serves the working app.

## Hosted compare workflow

Sign in at `https://app.homeenergywatch.com`, open History for the account, and use Compare two exports.

Upload the earlier export on the left and the later export on the right. The existing Add history form stays on the same page for single-file analysis, so a customer can either add one more file to the saved history or compare two files without changing the account record.

After the upload, the comparison page shows:

- matched month count
- total kWh change
- overnight baseline shift
- flagged-night change
- months that were left out because the other file did not have a matching month
- the matched month rows used for the packet

The page creates two downloads from the same comparison:

- Markdown for a readable review packet
- CSV for the aligned monthly rows

## Billing

Home Energy Watch is wired for Stripe subscriptions through hosted Checkout.

Current plans:

- Home Watch: $19/mo for one household
- Review Desk: $99/mo for up to 20 electric accounts
- Agency Pilot: custom commission or agency review workspace

Create matching Stripe Products and recurring Prices, then set the Price IDs in `STRIPE_PRICE_HOME`, `STRIPE_PRICE_REVIEW`, and `STRIPE_PRICE_AGENCY`. Set `STRIPE_SECRET_KEY` to enable Checkout and `STRIPE_WEBHOOK_SECRET` after registering `/stripe/webhook` in Stripe. The checkout flow uses Stripe subscriptions and the customer portal manages payment method changes, plan changes, and cancellation.

## CLI

```bash
python3 app.py --input path/to/utility-export.xml --output report.csv
```

Single-history analysis writes the CSV you name plus a same-stem JSON case file. The JSON artifact includes:

- the input file name
- the thresholds and timezone used
- the overnight baseline
- ranked suspicious days with severity scores
- weather context for flagged days when a saved account has a service address
- alert events and the full per-day summary

To compare two supported utility exports in one pass:

```bash
python3 app.py \
  --input path/to/earlier-export.xml \
  --compare-to path/to/later-export.xml \
  --output duke-compare.md
```

Compare mode keeps the single-file detector flow unchanged and adds a second path that:

- analyzes both files with the same overnight thresholds
- aligns the closest equivalent months as year-over-year when calendar months line up across years
- falls back to month-over-month matching when the files are offset by one month
- leaves unmatched months out of the side-by-side totals and lists them in the artifact

The comparison artifact is Markdown by default. It includes:

- matched monthly totals in kWh
- overnight baseline changes
- flagged-night counts
- the largest month-level deltas worth a regulator follow-up

If you point `--output` at a `.csv` file in compare mode, the tool saves the aligned monthly comparison rows as CSV instead.

To sync saved utility connections without starting the web server:

```bash
python3 app.py --sync-utilities
```

This command uses each saved utility connection, fetches the export, imports it through the same interval-history pipeline as a manual sync, and records the latest success or failure for that connection. It keeps going if one connection fails so the remaining accounts still refresh.

Optional flags:

- `--tz America/New_York`
- `--night-start 02:00`
- `--night-end 04:00`
- `--min-night-kw 1.0`
- `--night-multiplier 2.0`
- `--account-number primary` to add weather context from a saved account profile

## Health and tests

- `GET /health`
- `GET /api/files`
- `GET /api/supported-feeds`
- `POST /api/analyze`

Run tests with:

```bash
python3 -m pytest
```
