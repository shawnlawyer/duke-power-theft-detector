# Encrypted Multi-AZ RDS Cutover

This runbook replaces the current Home Energy Watch database with an encrypted Multi-AZ restore. It does not delete the current database. Stop before the restore command unless Shawn has approved the added RDS cost and the maintenance window.

## Current state

- Instance: `home-energy-watch-pg`
- Engine: PostgreSQL 18.3
- Class: `db.t3.micro`
- Storage: 20 GB gp3
- Private: yes
- Automated backups: 7 days
- Deletion protection: enabled
- Encryption: no
- Multi-AZ: no
- Existing encrypted checkpoint: `home-energy-watch-pre-hardening-20260721-0700-encrypted`

The cutover must use a new snapshot created after writes are paused. The existing encrypted checkpoint is for rollback evidence only and does not include later account-security migrations.

## Approval gate

At July 2026 on-demand prices for `us-east-1`:

- Current Single-AZ instance: `0.018 USD/hour`
- Current Single-AZ gp3 storage: `0.115 USD/GB-month`
- Target Multi-AZ instance: `0.036 USD/hour`
- Target Multi-AZ gp3 storage: `0.23 USD/GB-month`

At 730 hours and 20 GB, the estimate moves from about `15.44 USD/month` to `30.88 USD/month`. This excludes tax, excess backup storage, data transfer, and burst CPU charges.

Do not continue until Shawn approves:

1. The estimated additional `15.44 USD/month`.
2. A maintenance window. The app remains unavailable while the final snapshot is copied, restored, checked, and selected.

## Fixed values

Fill in the `<PLACEHOLDER>` values from the live AWS account before starting. Do not commit the filled-in values.

```bash
export AWS_PROFILE=shawn-admin
export AWS_REGION=us-east-1
export EC2_HOST=<EC2_HOST>                  # public DNS name or IP of the production instance
export EC2_SSH_KEY=<PATH_TO_EC2_SSH_KEY>    # local path to the instance SSH private key
export CURRENT_INSTANCE=home-energy-watch-pg
export TARGET_INSTANCE=home-energy-watch-pg-encrypted
export DB_SUBNET_GROUP=home-energy-watch-rds-subnets
export DB_SECURITY_GROUP=<DB_SECURITY_GROUP_ID>   # sg-...
export KMS_KEY_ID=<KMS_KEY_ID>                    # KMS key id or ARN for RDS encryption
export CURRENT_ENDPOINT=<CURRENT_RDS_ENDPOINT>    # e.g. home-energy-watch-pg.<id>.us-east-1.rds.amazonaws.com
export STAMP=$(date -u +%Y%m%d-%H%M)
export FINAL_SNAPSHOT="home-energy-watch-cutover-${STAMP}"
export ENCRYPTED_SNAPSHOT="${FINAL_SNAPSHOT}-encrypted"
```

## Cutover

1. Confirm the current app and database are healthy.

```bash
curl -fsS https://app.homeenergywatch.com/health
aws rds describe-db-instances \
  --db-instance-identifier "$CURRENT_INSTANCE" \
  --query 'DBInstances[0].DBInstanceStatus' \
  --output text
```

2. Pause application writes by stopping the web service and the scheduled utility sync. Stop the timer first so it cannot start a new sync run, then stop any sync run already in progress, then stop the web service. Confirm nothing is still running before taking the snapshot.

```bash
ssh -i "$EC2_SSH_KEY" "ubuntu@$EC2_HOST" \
  'sudo systemctl stop home-energy-watch-utility-sync.timer &&
   sudo systemctl stop home-energy-watch-utility-sync.service &&
   sudo systemctl stop home-energy-watch &&
   ! systemctl is-active --quiet home-energy-watch-utility-sync.service &&
   ! systemctl is-active --quiet home-energy-watch &&
   ! sudo docker ps --format "{{.Names}}" | grep -E "^home-energy-watch(-utility-sync)?$"'
```

3. Create a final snapshot and wait for it.

```bash
aws rds create-db-snapshot \
  --db-instance-identifier "$CURRENT_INSTANCE" \
  --db-snapshot-identifier "$FINAL_SNAPSHOT"

aws rds wait db-snapshot-available \
  --db-snapshot-identifier "$FINAL_SNAPSHOT"
```

4. Copy the snapshot with encryption and wait for it.

```bash
aws rds copy-db-snapshot \
  --source-db-snapshot-identifier "$FINAL_SNAPSHOT" \
  --target-db-snapshot-identifier "$ENCRYPTED_SNAPSHOT" \
  --kms-key-id "$KMS_KEY_ID" \
  --copy-tags

aws rds wait db-snapshot-available \
  --db-snapshot-identifier "$ENCRYPTED_SNAPSHOT"
```

5. Restore the encrypted snapshot as a private Multi-AZ instance.

```bash
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier "$TARGET_INSTANCE" \
  --db-snapshot-identifier "$ENCRYPTED_SNAPSHOT" \
  --db-instance-class db.t3.micro \
  --db-subnet-group-name "$DB_SUBNET_GROUP" \
  --vpc-security-group-ids "$DB_SECURITY_GROUP" \
  --multi-az \
  --no-publicly-accessible \
  --deletion-protection \
  --copy-tags-to-snapshot \
  --auto-minor-version-upgrade \
  --tags Key=Project,Value=HomeEnergyWatch Key=Environment,Value=production

aws rds wait db-instance-available \
  --db-instance-identifier "$TARGET_INSTANCE"
```

6. Confirm the replacement before selecting it.

```bash
aws rds describe-db-instances \
  --db-instance-identifier "$TARGET_INSTANCE" \
  --query 'DBInstances[0].{Status:DBInstanceStatus,Encrypted:StorageEncrypted,MultiAZ:MultiAZ,Private:PubliclyAccessible,DeletionProtection:DeletionProtection,Endpoint:Endpoint.Address}'
```

Every value must be healthy: `available`, encrypted `true`, Multi-AZ `true`, publicly accessible `false`, and deletion protection `true`.

7. Replace only the database hostname in the root-owned production env file, then recreate the app.

```bash
export TARGET_ENDPOINT=$(aws rds describe-db-instances \
  --db-instance-identifier "$TARGET_INSTANCE" \
  --query 'DBInstances[0].Endpoint.Address' \
  --output text)

ssh -i "$EC2_SSH_KEY" "ubuntu@$EC2_HOST" \
  "sudo sed -i 's|${CURRENT_ENDPOINT}|${TARGET_ENDPOINT}|g' /home/ubuntu/home-energy-watch/deploy/ec2/.env.production"

ssh -i "$EC2_SSH_KEY" "ubuntu@$EC2_HOST" \
  'sudo systemctl start home-energy-watch && sudo systemctl start home-energy-watch-utility-sync.timer'
```

The service restart reuses the already-built `home-energy-watch:current` image; it does not rebuild.

8. Verify the critical path.

```bash
curl -fsS --retry 20 --retry-delay 2 --retry-all-errors \
  https://app.homeenergywatch.com/health

ssh -i "$EC2_SSH_KEY" "ubuntu@$EC2_HOST" \
  "sudo docker exec home-energy-watch python -c \"import app; app.ensure_database(); print('database_ready=true')\""

ssh -i "$EC2_SSH_KEY" "ubuntu@$EC2_HOST" \
  "sudo docker inspect --format '{{.State.Health.Status}}' home-energy-watch && systemctl list-timers home-energy-watch-utility-sync.timer"
```

The container health status must be `healthy` and the utility-sync timer must show a next run.

Check customer sign-in, commissioner sign-in, one saved account, one history report, the Audit page, and the billing page without starting a live charge.

## Rollback

The original database remains available and protected. To roll back, stop the utility-sync timer, any running sync, and the web service; replace the target endpoint with the original endpoint in `.env.production`; then start the service and timer again. Writes made to the target database after cutover are not copied back; record the rollback time.

```bash
ssh -i "$EC2_SSH_KEY" "ubuntu@$EC2_HOST" \
  'sudo systemctl stop home-energy-watch-utility-sync.timer &&
   sudo systemctl stop home-energy-watch-utility-sync.service &&
   sudo systemctl stop home-energy-watch'

ssh -i "$EC2_SSH_KEY" "ubuntu@$EC2_HOST" \
  "sudo sed -i 's|${TARGET_ENDPOINT}|${CURRENT_ENDPOINT}|g' /home/ubuntu/home-energy-watch/deploy/ec2/.env.production"

ssh -i "$EC2_SSH_KEY" "ubuntu@$EC2_HOST" \
  'sudo systemctl start home-energy-watch && sudo systemctl start home-energy-watch-utility-sync.timer'

curl -fsS --retry 20 --retry-delay 2 --retry-all-errors \
  https://app.homeenergywatch.com/health
```

Keep the original database for an agreed observation period. Deleting it, deleting snapshots, or changing deletion protection requires a separate explicit approval.
