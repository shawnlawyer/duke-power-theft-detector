# National Grid connection setup

Home Energy Watch connects through National Grid's utility-hosted Green Button OAuth service. It does not use paid UtilityAPI collection endpoints. National Grid approved Epistemic Cognition's registration on October 13, 2026; its settings showed Live on October 16. Approval is separate from configuring and testing this application.

## Configuration

Keep automatic access disabled until the release, registered callback, credentials, and security/retention requirements are ready. Set these variables in the existing protected production environment, never in Git:

| Variable | Value |
| --- | --- |
| `POWER_NATIONAL_GRID_ENABLED` | `false` until activation is authorized and readiness is verified |
| `POWER_NATIONAL_GRID_CLIENT_ID` | The existing National Grid client ID from its OAuth settings |
| `POWER_NATIONAL_GRID_CLIENT_SECRET` | The existing OAuth client secret, installed through the approved secret-handling process |
| `POWER_NATIONAL_GRID_AUTHORIZE_URL` | `https://greenbuttonconnect.nationalgrid.com/oauth/authorize` |
| `POWER_NATIONAL_GRID_TOKEN_URL` | `https://greenbuttonconnect.nationalgrid.com/oauth/token` |
| `POWER_NATIONAL_GRID_REDIRECT_URI` | `https://app.homeenergywatch.com/utility-connection/national-grid/callback` |

Both endpoints were checked in the signed-in registration settings. Only the National Grid receipt page was registered as a redirect on October 16. Adding the application callback changes the recipient of authorization codes and requires explicit approval. Do not change the existing client, rotate secrets, convert to a general paid UtilityAPI account, or accept new terms as part of routine deployment.

A general UtilityAPI API token is not required: this implementation uses the registered OAuth client and customer-specific access/refresh tokens. It polls through the existing daily sync timer rather than using webhook notifications. No webhook registration is needed for this polling implementation.

## Customer flow

An account manager selects their National Grid account, grants local data permission, and chooses Connect National Grid. A new tab opens National Grid's own sign-in and consent page. National Grid handles passwords and verification codes. The return is bound to the initiating customer, browser, account and a single-use ten-minute state record.

The connector follows customer-account and agreement relationships to select the matching electric meters. It does not choose the first returned account, include gas, or expose tokens in the browser. Ambiguous mappings fail without an import. Tokens are encrypted with the application's existing encryption key.

After connecting, Sync now imports history; the existing daily timer performs subsequent refreshes. Expired access tokens are renewed server-side. Per-connection leases prevent concurrent workers from rotating the same credentials. A final transactional guard prevents deleted, replaced or locally withdrawn connections from importing a completed fetch.

## Data handling

Only completed hourly or sub-hourly forward energy readings with explicit watt-hour units are imported. Reading-type multipliers are applied. Estimated and questionable intervals are omitted, not presented as measured readings. Multiple electric meters are summed only where every meter supplies the same interval. Missing intervals remain gaps. Demand, reverse generation and cumulative registers are not substituted for consumption. Multiple consumption channels and paginated responses fail closed rather than silently mixing or truncating history.

The existing importer keeps identical readings once and leaves conflicting historical values unchanged. Imported rows carry a `national-grid://connection/…` source path. Existing upload behavior is unchanged.

Removing the connection or withdrawing local permission stops future collection. National Grid can also revoke access; an authorization failure removes the saved customer token. Removing a local connection does not revoke the customer's grant at National Grid. Customers must also manage sharing there when they want that grant withdrawn.

## Activation requirements

Before collecting real customer data:

1. Reconcile the accepted National Grid agreement and security attestation with actual controls. Resolve deletion-on-withdrawal, derived reports, shared accounts, backups and incident contacts. Existing deletion policy gates remain in force; this connector does not automatically delete historical data or certify compliance.
2. Confirm the National Grid partnership route is free. Do not purchase credits or enable paid collection/monitoring.
3. Obtain approval to register the exact callback and install the OAuth client secret securely. Keep the connector disabled until those settings are confirmed.
4. Run the full test suite and the existing production release/security checks on the isolated, merged release commit. Deploy using `scripts/deploy-production.sh`; do not bypass its clean-tree or merged-main requirements.
5. With specific authorization, test National Grid's documented residential and commercial test scenarios, including mixed gas/electric services. Verify account mapping and interval units against actual returned payloads. Mock tests are not provider interoperability certification.
6. Verify an explicitly authorized real electric account, successful refresh, local withdrawal, provider withdrawal, and absence of secrets in application/proxy logs before advertising automatic support.

## Verification commands

```bash
PYTHONPATH=. python -m pytest tests/test_national_grid.py -q
PYTHONPATH=. python -m pytest -q
python -m py_compile app.py national_grid.py
bash -n deploy/ec2/home-energy-watch-direct.sh
git diff --check
```

The connector tests cover authorization state, denial, expiry, browser binding, CSRF, manager access, account mismatch, gas exclusion, encrypted storage, refresh, revocation, concurrent sync exclusion, replacement during fetch, units, quality flags, duplicate imports, conflicting history, unsafe URLs, XML entities and redacted transport errors. PostgreSQL and live-provider tests remain separate release checks.

## References

- [National Grid developer guide](https://utilityapi.com/docs/utilities/nationalgrid)
- [Green Button OAuth](https://utilityapi.com/docs/greenbutton/oauth)
- [Green Button API](https://utilityapi.com/docs/greenbutton/api)
- [Green Button XML schema guide](https://utilityapi.com/docs/greenbutton/xml)
