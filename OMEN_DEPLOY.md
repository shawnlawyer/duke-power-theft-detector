# Omen Deploy

Omen is the local-network staging server for Home Energy Watch.

The deploy keeps the app in Docker, syncs this repo to Omen over SSH, rebuilds the image on Omen, restarts the container, and checks `/health` before reporting success. Runtime input and output data stay outside the synced app tree so a deploy does not wipe staging history.

## Current target

- Host: `192.168.1.120`
- SSH user: `shawn`
- SSH key: `/Users/shawnlawyer/.ssh/omen_id_ed25519`
- Remote repo: `/home/shawn/projects/home-energy-watch`
- Remote runtime: `/home/shawn/projects/home-energy-watch/runtime`
- Live app: `http://192.168.1.120:8089/`

## Deploy

```bash
./scripts/omen-deploy.sh all
```

Useful commands:

```bash
./scripts/omen-deploy.sh sync
./scripts/omen-deploy.sh deploy
./scripts/omen-deploy.sh check
./scripts/omen-deploy.sh logs
./scripts/omen-deploy.sh url
```

Runtime data persists on Omen under `runtime/input` and `runtime/output`. The sync step preserves `runtime/`, and the Docker build context ignores it.

Omen runs with `POWER_ENV=staging`. Set a unique `POWER_APP_SECRET` and, when saved utility connections are being tested, a dedicated Fernet key in `POWER_DATA_ENCRYPTION_KEY`. Keep both values only in the ignored `.env.omen` file.

Omen defaults to `POWER_EMAIL_BACKEND=disabled`, so staging accounts are immediately usable and no email is sent. Set `POWER_EMAIL_BACKEND=memory` only when testing confirmation or password-reset flows inside the app test suite.

Account-data deletion also defaults to paused. Leave `POWER_DATA_DELETION_ENABLED=false` until an approved retention policy exists. Enabling it requires a matching `POWER_DATA_DELETION_POLICY_VERSION`; the app refuses to start when the switch is on without a policy version.

## Billing on Omen

Pricing has not been approved for Home Energy Watch, so Omen defaults to `POWER_BILLING_ENABLED=false`. Leave it off while evaluating the product. When pricing is approved, set the approved Stripe Price IDs and then explicitly enable checkout:

```bash
POWER_BILLING_ENABLED=true
STRIPE_ACCOUNT_ID=acct_1TEP6v39IosmExPF
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
STRIPE_API_VERSION=2026-02-25.clover
STRIPE_PRICE_HOME=...
STRIPE_PRICE_REVIEW=...
```

Keep Stripe secret values out of git and chat. A leftover Price ID cannot open checkout while `POWER_BILLING_ENABLED=false`. Configure the Stripe webhook endpoint as `https://app.homeenergywatch.com/stripe/webhook` for production.

Then redeploy:

```bash
./scripts/omen-deploy.sh all
```
