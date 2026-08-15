# Incident Response Runbook

Use this when Home Energy Watch is unhealthy, logging stops, a database check fails, or the app starts returning 500s.

## What to check first

1. `https://app.homeenergywatch.com/health`
2. The EC2 container health status
3. The latest request logs for `request.failed` or repeated `request.completed` failures
4. The current production image digest and the last known good rollback image
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
3. If the issue is a bad deploy, roll back to the last known good image.
4. If the issue is database-related, stop and preserve the current instance and recent logs before any migration work.
5. If the issue is request-volume related, reduce traffic at the load balancer or reverse proxy before changing app code.

## When the database is unhappy

1. Confirm whether the problem is connectivity, authentication, migration drift, or a lock contention issue.
2. Check whether the app still returns `{"status":"ok"}` on `/health`.
3. Preserve the current RDS state before any destructive fix.
4. Use the verified rollback image or the latest known-good container if the web tier must be recovered quickly.

## Evidence to keep

- The failing request IDs
- The exact deploy image digest
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
