"""Tiny SQLite access layer.

Deliberately simple: one connection-per-call, rows returned as dicts. For a
single-user local app this is more than fast enough and easy to follow.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import config


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,              -- file on disk under data/images/
    original_name TEXT,                       -- name from the upload, if any
    mime_type     TEXT NOT NULL DEFAULT 'image/png',
    kind          TEXT NOT NULL,              -- 'upload' | 'generated'
    in_library    INTEGER NOT NULL DEFAULT 0, -- 1 if saved to the image library
    library_name  TEXT,                       -- custom @Name (unique when set)
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Enforce unique @library names, but only for rows that actually have one.
CREATE UNIQUE INDEX IF NOT EXISTS idx_images_library_name
    ON images(library_name) WHERE library_name IS NOT NULL;

CREATE TABLE IF NOT EXISTS generations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT,                 -- optional custom label (defaults to prompt in UI)
    prompt              TEXT NOT NULL,
    system_instruction  TEXT,                 -- resolved system instruction, or NULL if disabled
    model               TEXT NOT NULL,
    resolution          TEXT NOT NULL,
    num_images          INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending|running|succeeded|failed
    error               TEXT,
    batch_job_name      TEXT,                 -- Google batch job resource name
    reference_image_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array of image ids
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which generated images belong to which generation.
CREATE TABLE IF NOT EXISTS generation_images (
    generation_id INTEGER NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
    image_id      INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    PRIMARY KEY (generation_id, image_id)
);
"""


def init_db() -> None:
    """Create tables (if needed), run tiny migrations, seed default settings."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        existing = {row["key"] for row in conn.execute("SELECT key FROM settings")}
        for key, value in config.DEFAULT_SETTINGS.items():
            if key not in existing:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?)",
                    (key, _dump(value)),
                )
        conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive schema migrations for databases created by older versions."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(generations)")}
    if "name" not in cols:
        conn.execute("ALTER TABLE generations ADD COLUMN name TEXT")
    if "system_instruction" not in cols:
        conn.execute("ALTER TABLE generations ADD COLUMN system_instruction TEXT")


# --- settings helpers -------------------------------------------------------

def _dump(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _load(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def get_settings() -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: _load(row["value"]) for row in rows}


def update_settings(values: dict[str, Any]) -> None:
    with connect() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, _dump(value)),
            )
        conn.commit()
