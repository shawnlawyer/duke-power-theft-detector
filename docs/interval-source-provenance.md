# Interval source records

Each import retains the existing account/start/duration reading and records every source and exact asserted value in `interval_reading_sources`, including duplicates and conflicts. `matches_retained_value` uses the existing comparison rounded to nine decimal places; it does not imply exact binary equality. Conflicting readings remain excluded from analysis.

Repeated observations of the same source/value update the last observation time without replacing the first time, original adapter or original guarded connection. A source that supplies both agreeing and conflicting values retains both assertions. These records describe import observations, not an authenticated statement by a utility. Adapter IDs identify parsers or connectors, not necessarily the delivery channel. `guarded_connection_id` is populated only when the existing transactional connection/authorization guard was used; NULL means no such guard was supplied. It deliberately has no foreign key to the connection, so removing a connection does not silently remove data lineage. Account-scoped deletion inventories include the records.

The additive schema migration backfills only each canonical reading's recorded source/value, marked `retained_before_tracking`. Its times are copied from the existing import timestamp. Historical duplicate relationships are unknown. Backfill occurs only when creating the table, in the schema transaction, and does not recreate removed assertions on restart. Normal observations are marked `observed_import`.

Readings, assertions and the file summary commit or roll back together. Non-finite usage values are rejected. Existing connection guards, account isolation, canonical conflict behavior and provider activation gates remain in force.

Customer export format 4 adds `interval-sources.csv` for authorized accounts, with sanitized source basenames and CSV formula protection. Local paths, connection identifiers and credentials are not exported. Basenames are display labels and need not uniquely identify different sources.

This adds no provider-specific deletion operation, retention decision or connector activation. It does not reconstruct unknown source history or prove a live utility feed. Any later operation that changes a retained canonical value must also recompute the agreement flags. Duke and generic-download imports now use the same transactional authorization guard as the other connectors; this does not establish provider approval or a verified feed.


## In-flight connection changes

Imports lock the account and connection before checking active authorization. Sync attempts carry the original encrypted-token snapshot, access method and download target; a reconnect that saves the same plaintext credentials still invalidates the old attempt. Duke refreshes verify the account identity and authorization before saving tokens, then use the refreshed snapshot for import. Other connectors update the shared attempt snapshot only after their own conditional refresh succeeds.

Success and failure status changes use the same guard. The web route and scheduled runner no longer perform an unconditional second status write. Replaced, withdrawn, disabled or deleted connections keep their current state. A download that fails parsing or is refused before import loses only its newly created file; an import committed before a later reconnect retains its readings and file. Network fetches occur outside database locks. Permission remains valid when another manager still has an active authorization, matching existing account policy.
