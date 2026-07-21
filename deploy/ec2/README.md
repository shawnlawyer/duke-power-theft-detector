Home Energy Watch on one EC2 instance and one RDS Postgres database.

The live database replacement procedure is documented in `RDS_ENCRYPTED_MULTIAZ_CUTOVER.md`. It is approval-gated because it adds recurring cost and requires a maintenance window.
The current control status and remaining enterprise gates are tracked in `ENTERPRISE_READINESS.md`.

1. Create a small Linux EC2 instance with Docker and Docker Compose installed.
2. Create a Postgres RDS instance in the same VPC and security group it so EC2 can reach port `5432`.
   - Keep it private.
   - Enable storage encryption, automated backups, and deletion protection.
   - Use Multi-AZ for a production service that cannot tolerate a single availability-zone outage.
3. Copy `.env.production.example` to `.env.production` and fill in:
   - `POWER_APP_SECRET`
   - `POWER_AUDIT_SIGNING_KEY`
   - `POWER_DATA_ENCRYPTION_KEY`
   - `POWER_DATABASE_URL`
   - `POWER_EMAIL_BACKEND=ses`
   - `POWER_EMAIL_FROM=support@homeenergywatch.com`
   - `POWER_EMAIL_REGION=us-east-1`
   - `POWER_STAFF_MFA_REQUIRED=true` when commission MFA enforcement is ready
   - `POWER_DATA_DELETION_ENABLED=false` until a retention policy is approved
   - `POWER_DATA_DELETION_POLICY_VERSION` only when that approved policy is ready for use
   - `POWER_BILLING_ENABLED=false` until Home Energy Watch pricing is approved
   - `STRIPE_SECRET_KEY`
   - `STRIPE_WEBHOOK_SECRET`
   - `STRIPE_PRICE_HOME`
   - `STRIPE_PRICE_REVIEW`
4. Create the host folders:
   - `/opt/home-energy-watch/input`
   - `/opt/home-energy-watch/output`
5. From the repo root, launch production:

```bash
docker compose --env-file deploy/ec2/.env.production -f deploy/ec2/docker-compose.prod.yml up -d --build
```

6. Point your DNS at the EC2 public IP:
   - `homeenergywatch.com`
   - `app.homeenergywatch.com`
7. Put TLS in front of the container with your preferred reverse proxy or load balancer.
8. Set the public base URLs so billing and cross-host links stay on the right hostname:
   - `POWER_PUBLIC_BASE_URL=https://app.homeenergywatch.com`
   - `POWER_MARKETING_BASE_URL=https://homeenergywatch.com`

Billing uses direct backend Stripe Checkout. Store `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_HOME`, and `STRIPE_PRICE_REVIEW` only in the ignored production env file or approved secret store. Checkout remains closed unless `POWER_BILLING_ENABLED=true`; do not enable it until the plan prices and matching Stripe Price IDs have been approved for Home Energy Watch. Configure the Stripe webhook endpoint as `https://app.homeenergywatch.com/stripe/webhook`.

Set `POWER_ENV=production`, use a unique `POWER_APP_SECRET` of at least 32 characters, and generate a dedicated Fernet key for `POWER_DATA_ENCRYPTION_KEY`:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Generate a separate random value of at least 32 characters for `POWER_AUDIT_SIGNING_KEY`. Store all three values only in the ignored production env file or approved secret store. The app refuses to start in production when any value is missing or invalid. Keep the audit-signing key stable across deploys; changing it invalidates verification of existing audit records. Set `POWER_TRUST_PROXY=true` only when the container is behind the configured reverse proxy or load balancer.

The production Postgres URL must require TLS with `?sslmode=require` or a stricter certificate-verifying mode. Production startup fails when the URL points anywhere other than Postgres or does not require TLS.

Production email uses Amazon SES through the EC2 instance role. Verify `homeenergywatch.com` in SES, publish the SES DKIM records in DNS, and use a verified sender in `POWER_EMAIL_FROM`. `POWER_EMAIL_REPLY_TO` is optional. Production startup fails when SES delivery or the sender address is missing.

The app will create or update its tables on startup. Local file uploads and CSV exports stay on the EC2 host under `/opt/home-energy-watch`.

The same app service can answer both hostnames. The apex domain serves the public pages, and the `app.` subdomain serves the working app.

## Scheduled utility sync

Run the saved utility-connection sync once from the EC2 checkout:

```bash
docker compose --env-file deploy/ec2/.env.production -f deploy/ec2/docker-compose.prod.yml run --rm power-detector python app.py --sync-utilities
```

To limit the job to one account:

```bash
docker compose --env-file deploy/ec2/.env.production -f deploy/ec2/docker-compose.prod.yml run --rm power-detector python app.py --sync-utilities --account-number primary
```

Cron example for a daily 2:15 a.m. sync:

```cron
15 2 * * * cd /home/ubuntu/home-energy-watch && docker compose --env-file deploy/ec2/.env.production -f deploy/ec2/docker-compose.prod.yml run --rm power-detector python app.py --sync-utilities >> /var/log/home-energy-watch-utility-sync.log 2>&1
```

The command exits `0` when all saved connections sync and exits `1` when any connection fails. The app still records per-connection status, last attempt time, last successful sync time, and the latest error so the account page shows what happened after the cron run.
