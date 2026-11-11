FROM python:3.11-slim@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get upgrade --yes \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m pip uninstall --yes setuptools wheel pip

# Unprivileged runtime user. The UID/GID are fixed (10001) so host bind mounts
# can be chowned to a stable owner; see deploy/ec2/README.md.
# Only the data directories are writable by the app user. The application
# source under /app stays root-owned and read-only for the runtime user.
#   /data/{input,output}                    default POWER_INPUT_DIR/POWER_OUTPUT_DIR
#                                           (docker-compose.yml and Omen staging)
#   /srv/home-energy-watch/{input,output}   production EC2 bind mounts
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /home/app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/input /data/output /srv/home-energy-watch/input /srv/home-energy-watch/output \
    && chown -R 10001:10001 /data /srv/home-energy-watch

COPY . .

USER 10001:10001

EXPOSE 8000

# python:3.11-slim ships without curl, so the probe uses the standard library.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).read()"]

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:8000 --workers ${POWER_WEB_CONCURRENCY:-2} --timeout ${POWER_GUNICORN_TIMEOUT:-120} --graceful-timeout 30 --max-requests 1000 --max-requests-jitter 100 app:web_app"]
