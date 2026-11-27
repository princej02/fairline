"""Database connection, migrations and config -> reference-table sync."""

from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from fairline.core.config import Config, get_config, repo_root


def database_url() -> str:
    load_dotenv(repo_root() / ".env", override=False)
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or database_url())


def migrate(conn: psycopg.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Apply db/migrations/*.sql in filename order, each in its own transaction."""
    migrations_dir = migrations_dir or repo_root() / "db" / "migrations"
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for path in sorted(migrations_dir.glob("*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        applied.append(path.name)
    return applied


def sync_reference(conn: psycopg.Connection, config: Config | None = None) -> None:
    """Upsert competitions and bookmakers from config. Competitions removed from the
    allowlist are disabled, not deleted, so history stays intact."""
    config = config or get_config()
    with conn.transaction():
        for c in config.competitions.values():
            conn.execute(
                """
                INSERT INTO competition (sport_id, code, name, country, kind, providers, enabled)
                VALUES ((SELECT id FROM sport WHERE code = %s), %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, country = EXCLUDED.country,
                  kind = EXCLUDED.kind, providers = EXCLUDED.providers, enabled = TRUE
                """,
                (c.sport, c.code, c.name, c.country, c.kind, json.dumps(c.providers)),
            )
        conn.execute("UPDATE competition SET enabled = FALSE WHERE code <> ALL(%s)", (list(config.competitions),))
        for b in config.bookmakers.values():
            conn.execute(
                """
                INSERT INTO bookmaker (code, name, kind, is_sharp, providers)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
                  is_sharp = EXCLUDED.is_sharp, providers = EXCLUDED.providers
                """,
                (b.code, b.name, b.kind, b.is_sharp, json.dumps(b.providers)),
            )
