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

## Billing on Omen

Set these in `.env.omen` before deploying if you want live Stripe checkout on staging:

```bash
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_HOME=price_...
STRIPE_PRICE_REVIEW=price_...
STRIPE_PRICE_AGENCY=price_...
```

Then redeploy:

```bash
./scripts/omen-deploy.sh all
```
