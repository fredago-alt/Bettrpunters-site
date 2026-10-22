"""SQLite connection and forward-only migrations."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def available_migrations() -> list[tuple[int, str, Path]]:
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(p.name.split("_", 1)[0])
        out.append((version, p.stem, p))
    versions = [v for v, _, _ in out]
    if len(versions) != len(set(versions)):
        raise RuntimeError("duplicate migration version numbers")
    return out


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at REAL NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return int(row["v"] or 0)


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns names applied."""
    applied = []
    have = current_version(conn)
    for version, name, path in available_migrations():
        if version <= have:
            continue
        sql = path.read_text()
        try:
            conn.execute("BEGIN")
            # executescript would COMMIT implicitly; split on ';' at statement ends instead.
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                         (version, name, time.time()))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        applied.append(name)
    return applied


def _split_sql(sql: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies (BEGIN ... END;) intact."""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    stmts, buf = [], ""
    for ln in lines:
        buf += ln + "\n"
        if sqlite3.complete_statement(buf):
            if buf.strip():
                stmts.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError("incomplete SQL statement at end of migration")
    return stmts


def open_db(path: str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


def ensure_run(conn: sqlite3.Connection, run_id: str, kind: str, settings, notes: str | None = None) -> None:
    """Create the run and its AUD account if missing. An existing live run keeps its
    balance; a mismatched rules fingerprint is recorded in notes, not silently ignored."""
    from dataclasses import asdict

    row = conn.execute("SELECT rules_fingerprint FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    now = time.time()
    if row is None:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO runs(run_id, kind, created_at, rules_fingerprint, settings_json, notes) VALUES (?,?,?,?,?,?)",
            (run_id, kind, now, settings.rules_fingerprint(), json.dumps(asdict(settings), default=list), notes),
        )
        conn.execute(
            "INSERT INTO account(run_id, starting_cash_aud, cash_aud, updated_at) VALUES (?,?,?,?)",
            (run_id, settings.starting_cash_aud, settings.starting_cash_aud, now),
        )
        conn.execute("COMMIT")
    elif row["rules_fingerprint"] != settings.rules_fingerprint():
        raise RuntimeError(
            f"Run {run_id!r} was created with rules {row['rules_fingerprint']} but current rules are "
            f"{settings.rules_fingerprint()}. Use a new PAPERBOT run id (--run-id) when changing rules, "
            "so results from different rule sets are never mixed."
        )



@contextmanager
def transaction(conn: sqlite3.Connection):
    """Atomic block; nests safely via SAVEPOINTs."""
    name = f"sp_{id(object())}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield conn
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")
