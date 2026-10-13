"""Optional: run the whole suite against a real Postgres server.

Set POWER_TEST_POSTGRES_ADMIN_URL to a superuser URL (for example
postgresql://hew:secret@127.0.0.1:5432/postgres). Every test that calls
configure_tmp_paths then gets its own freshly created database, which is
dropped afterwards. Without the variable, tests use SQLite as before.
"""

import itertools
import os
from urllib.parse import urlsplit, urlunsplit

import pytest

ADMIN_URL = os.environ.get("POWER_TEST_POSTGRES_ADMIN_URL", "").strip()
_database_counter = itertools.count()


def _database_url(name: str) -> str:
    parts = urlsplit(ADMIN_URL)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


@pytest.fixture(autouse=True)
def _postgres_per_test(request, monkeypatch):
    module = request.module
    if not ADMIN_URL or not hasattr(module, "configure_tmp_paths"):
        yield
        return
    import psycopg

    name = f"hew_test_{os.getpid()}_{next(_database_counter)}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}"')
    url = _database_url(name)
    original = module.configure_tmp_paths

    def configure_with_postgres(tmp_path, mp):
        original(tmp_path, mp)
        mp.setattr(module.app, "DATABASE_URL", url)
        mp.setenv("POWER_DATABASE_URL", url)

    monkeypatch.setattr(module, "configure_tmp_paths", configure_with_postgres)
    try:
        yield
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
