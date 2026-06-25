Home Energy Watch on one EC2 instance and one RDS Postgres database.

1. Create a small Linux EC2 instance with Docker and Docker Compose installed.
2. Create a Postgres RDS instance in the same VPC and security group it so EC2 can reach port `5432`.
3. Copy `.env.production.example` to `.env.production` and fill in:
   - `POWER_APP_SECRET`
   - `POWER_DATABASE_URL`
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

The app will create or update its tables on startup. Local file uploads and CSV exports stay on the EC2 host under `/opt/home-energy-watch`.

The same app service can answer both hostnames. The apex domain serves the public pages, and the `app.` subdomain serves the working app.
