# Enterprise readiness ledger

This ledger separates implemented controls from production proof and approval-gated work. Update it after every infrastructure or security change.

## Application controls verified on 2026-07-21

- Customer and commission accounts use separate sign-in flows and role checks.
- Passwords are hashed; password reset and staff invitation tokens are hashed, expire, and work once.
- Password changes, staff suspension, and role changes revoke existing sessions through `auth_version`.
- Commission sign-in requires TOTP MFA. Secrets are encrypted, codes cannot be replayed, and one-time recovery codes are stored only as hashes.
- Commissioners can invite, suspend, reactivate, and change access for other staff members. They can reset another staff member's authenticator after confirmation; the reset destroys recovery codes, revokes sessions, and requires fresh enrollment without changing the password, role, or active status. Self-reset and self-lockout are blocked.
- Customer account reads and writes are checked against account access records.
- Customer signup records the accepted Terms and Privacy Notice versions, the utility-data authorization version and scope, and hashed request evidence. Authorization grants, withdrawals, superseded grants, and access-removal revocations are retained as an append-only account history.
- History uploads, comparisons, saved utility connections, and utility sync require an active customer authorization. Withdrawal erases saved utility credentials when no active authorization remains.
- Customers can download a ZIP archive of every authorized account, profile, inventory item, interval, weather record, generated report, policy acceptance, and utility-data authorization. Credentials, password fields, Stripe internals, technical request hashes, internal paths, and unauthorized accounts are excluded.
- CSRF protection covers state-changing browser requests. Stripe webhooks require provider signatures instead.
- Production sessions use secure, HTTP-only, same-site cookies and the app emits browser security headers.
- Utility access secrets use authenticated encryption at rest.
- Audit records form a keyed hash chain. Commissioners can verify and export the filtered record; export stops when integrity fails.
- Direct Stripe operations remain backend-only. `POWER_BILLING_ENABLED=false` keeps checkout closed until Home Energy Watch prices and matching Stripe Price IDs are approved, even if stale Stripe configuration remains. No live charge or refund is part of automated verification.
- The release image uses a digest-pinned Python base and a fully hashed dependency lock. Runtime packaging tools are removed after installation.
- The release-security gate runs the complete test suite, `pip-audit`, and a digest-pinned Trivy scan that fails on fixed high or critical findings. The local gate passed all 114 tests with no known dependency vulnerabilities and zero high or critical container findings.
- Local, Omen staging, and EC2 candidate images pass the complete 114-test automated suite.

## Production proof verified on 2026-07-21

- `https://app.homeenergywatch.com/health` returned `{"status":"ok"}` after deployment.
- The EC2 container reported healthy after replacement.
- RDS migrations created staff auth tokens, staff auth versions, hashed staff invitations, and both audit-chain fields.
- The production audit chain passed verification after migration.
- Commission sign-in and password-recovery pages rendered in the browser with the Home Energy Watch name and password visibility control.
- `POWER_STAFF_MFA_REQUIRED=true` is active. All three existing staff accounts are intact and will enroll on their next sign-in.
- The commissioner authenticator-recovery route is live and redirects unauthenticated requests to commission sign-in.
- The customer data archive is live and redirects anonymous requests to customer sign-in.
- Public Terms, Privacy Notice, and utility-data permission pages are live. Signup requires policy acceptance and confirmation that the customer controls the utility account.
- RDS contains `customer_policy_acceptances`, `account_data_authorizations`, the account/status lookup index, and the partial unique active-authorization index.
- The hardened production image `sha256:46a2e518c136409bf7a9bcc699fdd568b52098a848c5fd9f54bde34f67b5cbe2` passed all 114 tests on EC2 before promotion. The running container is healthy, required MFA remains active, its audit chain is valid, existing staff accounts are present, and runtime packaging tools are absent.
- `POWER_BILLING_ENABLED=false` is active on Omen and production. The live pricing and signup pages contain no dollar amount, and the public pricing page exposes no checkout action while pricing is unapproved.
- The architecture-matched x86 Omen image passed the high and critical Trivy gate with zero Debian or Python findings.
- GitHub release-security run `29820086282` passed on pull request 1, and run `29820188538` passed after merge to `main`. Both executed the complete test, dependency-audit, and container-scan gate.
- Customer-consent run `29822323048` passed on pull request 3, and run `29822409301` passed after merge to `main`.
- Pricing-approval run `29823055500` passed on pull request 4. Main run `29823156913` passed on rerun after Docker Hub recovered from a transient 502 while pulling the separate audit helper image; application tests had passed on the initial attempt.
- `main` requires pull requests and a current `verify` check. Enforcement applies to administrators; unresolved review conversations block merging, and force-pushes and branch deletion are disabled.
- The immediately previous production image is retained as `home-energy-watch:rollback-20260721-pricing-predeploy`.

## Last verified infrastructure state

The following AWS state was read directly earlier on 2026-07-21. The current EC2 role can send application email but cannot re-read RDS, EC2, SES account, or CloudWatch inventory, so these entries require an authenticated AWS refresh before any readiness claim or change.

- RDS: PostgreSQL 18.3, `db.t3.micro`, private, 20 GB gp3, seven-day backups, deletion protection enabled.
- RDS storage encryption: not enabled on the current instance.
- RDS availability: Single-AZ.
- Encrypted snapshot retained: `home-energy-watch-pre-hardening-20260721-0700-encrypted`.
- SES domain identity and DKIM: verified. Application delivery through the SES simulator succeeded.
- CloudWatch application alarms: not yet proven.

## Approval-gated work

### Encrypted Multi-AZ RDS cutover

Requires Shawn's approval for the maintenance window and the estimated increase from about `$15.44/month` to `$30.88/month`, before tax, transfer, excess backup, or burst charges. Follow `RDS_ENCRYPTED_MULTIAZ_CUTOVER.md`; do not delete the current database during the cutover.

### Live payment verification

Home Energy Watch pricing and matching Stripe Price IDs must be approved before enabling checkout. A later live test also requires explicit approval for the exact charge and refund. Automated tests use Stripe fakes and do not move money.

### Monitoring and durable storage

CloudWatch alarms, centralized log retention, and S3 versioned storage add AWS resources and possible recurring charges. Confirm the alert destination, retention period, and budget before provisioning them.

## Open enterprise controls

1. Define customer-data retention, deletion, and legal-hold rules; implement those approved rules. Customer export is implemented.
2. Move uploaded history and generated reports to encrypted, versioned object storage with lifecycle policy.
3. Add centralized application logs, availability alarms, database alarms, and an incident-response runbook.
4. Run and record a database restore exercise with measured recovery time and recovery point.
5. Complete privacy, terms, utility authorization, and commission procurement review.
6. Obtain an independent security review before handling commission-wide production data.

The product is not enterprise-ready while the open controls above remain unverified.
