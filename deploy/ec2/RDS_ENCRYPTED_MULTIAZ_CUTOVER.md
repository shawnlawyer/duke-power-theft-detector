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

```bash
export AWS_PROFILE=shawn-admin
export AWS_REGION=us-east-1
export CURRENT_INSTANCE=home-energy-watch-pg
export TARGET_INSTANCE=home-energy-watch-pg-encrypted
export DB_SUBNET_GROUP=home-energy-watch-rds-subnets
export DB_SECURITY_GROUP=sg-0177259729ebe79cc
export KMS_KEY_ID=6b78a5e5-8e2f-4452-a376-d4f1e9f71cdf
export CURRENT_ENDPOINT=home-energy-watch-pg.csl6euecwerk.us-east-1.rds.amazonaws.com
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

2. Pause application writes by stopping the production service.

```bash
ssh -i ~/.ssh/shawnlawyer-ec2-key-20260309.pem ubuntu@54.243.191.43 \
  'cd /home/ubuntu/home-energy-watch && sudo docker compose --env-file deploy/ec2/.env.production -f deploy/ec2/docker-compose.prod.yml stop power-detector'
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

ssh -i ~/.ssh/shawnlawyer-ec2-key-20260309.pem ubuntu@54.243.191.43 \
  "sudo sed -i 's|${CURRENT_ENDPOINT}|${TARGET_ENDPOINT}|g' /home/ubuntu/home-energy-watch/deploy/ec2/.env.production"

ssh -i ~/.ssh/shawnlawyer-ec2-key-20260309.pem ubuntu@54.243.191.43 \
  'cd /home/ubuntu/home-energy-watch && sudo docker compose --env-file deploy/ec2/.env.production -f deploy/ec2/docker-compose.prod.yml up -d --force-recreate --no-build'
```

8. Verify the critical path.

```bash
curl -fsS --retry 20 --retry-delay 2 --retry-all-errors \
  https://app.homeenergywatch.com/health

ssh -i ~/.ssh/shawnlawyer-ec2-key-20260309.pem ubuntu@54.243.191.43 \
  "docker exec ec2-power-detector-1 python -c \"import app; app.ensure_database(); print('database_ready=true')\""
```

Check customer sign-in, commissioner sign-in, one saved account, one history report, the Audit page, and the billing page without starting a live charge.

## Rollback

The original database remains available and protected. To roll back, stop the app, replace the target endpoint with the original endpoint in `.env.production`, and recreate the service.

```bash
ssh -i ~/.ssh/shawnlawyer-ec2-key-20260309.pem ubuntu@54.243.191.43 \
  "sudo sed -i 's|${TARGET_ENDPOINT}|${CURRENT_ENDPOINT}|g' /home/ubuntu/home-energy-watch/deploy/ec2/.env.production"

ssh -i ~/.ssh/shawnlawyer-ec2-key-20260309.pem ubuntu@54.243.191.43 \
  'cd /home/ubuntu/home-energy-watch && sudo docker compose --env-file deploy/ec2/.env.production -f deploy/ec2/docker-compose.prod.yml up -d --force-recreate --no-build'

curl -fsS --retry 20 --retry-delay 2 --retry-all-errors \
  https://app.homeenergywatch.com/health
```

Keep the original database for an agreed observation period. Deleting it, deleting snapshots, or changing deletion protection requires a separate explicit approval.
