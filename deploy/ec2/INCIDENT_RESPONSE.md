# Incident Response Runbook

Use this when Home Energy Watch is unhealthy, logging stops, a database check fails, or the app starts returning 500s.

## What to check first

1. `https://app.homeenergywatch.com/health`
2. The EC2 container health status. The image defines a Docker `HEALTHCHECK` that calls `/health` inside the container every 30 seconds:
   `sudo docker inspect --format '{{.State.Health.Status}}' home-energy-watch` (`healthy`, `starting`, or `unhealthy`), and
   `sudo docker inspect --format '{{json .State.Health.Log}}' home-energy-watch` for the last probe results.
   Docker only reports this status; it does not restart the container. systemd (`home-energy-watch.service`, `Restart=always`) restarts the container only when it exits.
3. The latest request logs for `request.failed` or repeated `request.completed` failures (`journalctl -u home-energy-watch`)
4. Which image is running and which images are available for rollback:
   `sudo docker inspect --format '{{.Image}} {{.Config.Image}}' home-energy-watch` and
   `sudo docker images home-energy-watch`. Tags: `:current` is what systemd runs, `:previous` is the release that was running before the latest deploy, and `:<git-sha>` is each built release.
5. The RDS connection and recent migration history

## What the request logs should show

- A `request_id` for every request
- The request method
- The route template, not the raw query string
- The response status code
- The elapsed time in milliseconds
- A small actor label such as `anonymous`, `customer`, or `staff`

The logs must not include passwords, email addresses, account numbers, file contents, access tokens, Stripe secrets, or raw utility credentials.

## When the app is down

1. Confirm whether the failure is in the web process, the database, or the container health check.
2. Check the latest `request.failed` entries and copy the `request_id` before making changes.
3. If the issue is a bad deploy, roll back to the last known good image. `scripts/deploy-production.sh deploy` already does this automatically when the restart or `/health` verification fails: it retags `home-energy-watch:previous` as `:current`, restarts the service, re-verifies `/health`, and exits non-zero. To do the same by hand on the EC2 host:

   ```bash
   sudo docker tag home-energy-watch:previous home-energy-watch:current
   sudo systemctl restart home-energy-watch
   curl -fsS https://app.homeenergywatch.com/health
   ```

   To run a specific older release instead, retag its `home-energy-watch:<git-sha>` image as `:current` and restart. Restarting the service never rebuilds the image. To also restore the previous application source tree, run `scripts/deploy-production.sh rollback --confirm-production` from a workstation; it restores the latest source backup, rebuilds it, tags it `:current`, and restarts.
4. If the issue is database-related, stop and preserve the current instance and recent logs before any migration work.
5. If the issue is request-volume related, reduce traffic at the load balancer or reverse proxy before changing app code.

## When the database is unhappy

1. Confirm whether the problem is connectivity, authentication, migration drift, or a lock contention issue.
2. Check whether the app still returns `{"status":"ok"}` on `/health`.
3. Preserve the current RDS state before any destructive fix.
4. Use `home-energy-watch:previous` (or a known-good `home-energy-watch:<git-sha>`) if the web tier must be recovered quickly. If the problem involves a migration, an image rollback does not undo schema changes; restore from the pre-deploy RDS snapshot only with the database owner's approval. `scripts/deploy-production.sh deploy` takes that snapshot first when `HEW_RDS_INSTANCE_ID` is set.

## Evidence to keep

- The failing request IDs
- The exact deploy image ID and tag (`home-energy-watch:<git-sha>`) and the image that `:previous` pointed to
- The container health log (`docker inspect --format '{{json .State.Health}}' home-energy-watch`)
- The health check output
- The database error message or migration step that failed
- The time the issue started and the time it was contained

## Recovery closeout

1. Confirm the health endpoint is green.
2. Confirm a normal customer sign-in still works.
3. Confirm a customer can load the dashboard and the account pages.
4. Record what failed, what was changed, and what needs follow-up.

## Assign before launch

These owners must be named before this runbook is treated as final:

- Primary incident lead
- Backup incident lead
- Database owner
- Customer communications owner
- Security escalation contact
