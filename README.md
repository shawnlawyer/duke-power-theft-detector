# PowerPattern

Home Energy Watch is a home energy audit and review tool. It keeps customer history in one place, compares meter readings against household load expectations, and helps surface spikes that do not fit the home.

## Supported utility feeds

The app normalizes interval history into one internal model, then lets each utility feed plug into that model through a dedicated adapter.

Current adapters:

- Green Button ESPI XML
- Duke-style interval XML
- Utility interval CSV

That keeps the alerting, reporting, household baseline, and load-test logic shared across feeds instead of baking Duke-specific parsing rules into the rest of the app.

## Local Docker

```bash
docker compose up --build
```

Then open [http://localhost:8001](http://localhost:8001).

Local Docker keeps uploaded history in `./data/input`, generated CSV, JSON, and comparison exports in `./data/output`, and uses SQLite by default at `./data/output/power-history.db`.

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

## Billing

Home Energy Watch is wired for Stripe subscriptions through hosted Checkout.

Current plans:

- Home Watch: $19/mo for one household
- Review Desk: $99/mo for up to 20 electric accounts
- Agency Pilot: custom commission or agency review workspace

Create matching Stripe Products and recurring Prices, then set the Price IDs in `STRIPE_PRICE_HOME`, `STRIPE_PRICE_REVIEW`, and `STRIPE_PRICE_AGENCY`. Set `STRIPE_SECRET_KEY` to enable Checkout and `STRIPE_WEBHOOK_SECRET` after registering `/stripe/webhook` in Stripe.

## CLI

```bash
python3 app.py --input path/to/utility-export.xml --output report.csv
```

Single-history analysis writes the CSV you name plus a same-stem JSON case file. The JSON artifact includes:

- the input file name
- the thresholds and timezone used
- the overnight baseline
- ranked suspicious days with severity scores
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

Optional flags:

- `--tz America/New_York`
- `--night-start 02:00`
- `--night-end 04:00`
- `--min-night-kw 1.0`
- `--night-multiplier 2.0`

## Health and tests

- `GET /health`
- `GET /api/files`
- `GET /api/supported-feeds`
- `POST /api/analyze`

Run tests with:

```bash
python3 -m pytest
```
