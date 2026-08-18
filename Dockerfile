FROM python:3.11-slim@sha256:9c900dea9e8fb7e16277c179b555cc72d29a352dbc33cff48ad5a0412fd5bfc7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m pip uninstall --yes setuptools wheel pip

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:8000 --workers ${POWER_WEB_CONCURRENCY:-2} --timeout ${POWER_GUNICORN_TIMEOUT:-120} app:web_app"]
