"""Persistence helpers for tokens, discovery events and immutable observations."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .db import iso
from .dexscreener import PairSnapshot

OBS_FIELDS = (
    "price_usd", "price_native", "liquidity_usd", "fdv_usd", "market_cap_usd", "volume_m5_usd",
    "volume_h1_usd", "buys_m5", "sells_m5", "buys_h1", "sells_h1", "price_change_m5",
    "price_change_h1", "pair_created_at",
)


@dataclass(frozen=True)
class Observation:
    id: int
    token_address: str
    pair_address: str | None
    observed_at: float
    status: str  # 'ok' | 'missing'
    reason: str | None
    symbol: str | None
    dex_id: str | None
    quote_symbol: str | None
    price_usd: float | None
    price_native: float | None
    liquidity_usd: float | None
    fdv_usd: float | None
    market_cap_usd: float | None
    volume_m5_usd: float | None
    volume_h1_usd: float | None
    buys_m5: int | None
    sells_m5: int | None
    buys_h1: int | None
    sells_h1: int | None
    price_change_m5: float | None
    price_change_h1: float | None
    pair_created_at: float | None
    aud_per_usd: float | None = None
    fx_source: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.price_usd is not None and self.price_usd > 0

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Observation":
        return cls(**{k: r[k] for k in cls.__dataclass_fields__})


def record_discovery(conn: sqlite3.Connection, token_address: str, chain_id: str, source: str,
                     seen_at: float, raw: dict | None = None) -> bool:
    """Log a feed sighting and upsert the token. Returns True if the token is new."""
    conn.execute("INSERT INTO discovery_events(token_address, seen_at, source, raw_json) VALUES (?,?,?,?)",
                 (token_address, seen_at, source, json.dumps(raw) if raw is not None else None))
    existed = conn.execute("SELECT 1 FROM tokens WHERE token_address = ?", (token_address,)).fetchone()
    conn.execute(
        "INSERT INTO tokens(token_address, chain_id, first_seen_at, first_seen_source, last_seen_in_feed_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(token_address) DO UPDATE SET last_seen_in_feed_at = excluded.last_seen_in_feed_at",
        (token_address, chain_id, seen_at, source, seen_at),
    )
    return existed is None


def insert_observation(conn: sqlite3.Connection, observed_at: float, *, snap: PairSnapshot | None = None,
                       token_address: str | None = None, pair_address: str | None = None,
                       missing_reason: str | None = None, aud_per_usd: float | None = None,
                       fx_source: str | None = None, store_raw: bool = False) -> Observation:
    """Append one immutable observation. Pass ``snap`` for data, or token/pair + reason for a miss."""
    if snap is not None:
        values = {
            "token_address": snap.token_address, "pair_address": snap.pair_address, "status": "ok",
            "reason": None, "symbol": snap.symbol, "dex_id": snap.dex_id, "quote_symbol": snap.quote_symbol,
            **{k: getattr(snap, k) for k in OBS_FIELDS},
            "raw_json": json.dumps(snap.raw) if store_raw else None,
        }
    else:
        if not token_address:
            raise ValueError("token_address required for a missing observation")
        values = {"token_address": token_address, "pair_address": pair_address, "status": "missing",
                  "reason": missing_reason or "not_returned_by_api", "symbol": None, "dex_id": None,
                  "quote_symbol": None, **{k: None for k in OBS_FIELDS}, "raw_json": None}
    values["observed_at"] = observed_at
    values["aud_per_usd"] = aud_per_usd
    values["fx_source"] = fx_source
    values["observed_at_iso"] = iso(observed_at)
    cols = ",".join(values)
    cur = conn.execute(f"INSERT INTO observations({cols}) VALUES ({','.join('?' * len(values))})",
                       tuple(values.values()))
    conn.execute(
        "UPDATE tokens SET last_observed_at = ?, symbol = COALESCE(?, symbol), name = COALESCE(?, name) "
        "WHERE token_address = ?",
        (observed_at, values["symbol"], snap.name if snap else None, values["token_address"]),
    )
    row = conn.execute("SELECT * FROM observations WHERE id = ?", (cur.lastrowid,)).fetchone()
    return Observation.from_row(row)


def set_token_status(conn: sqlite3.Connection, token_address: str, status: str, rejection: str | None) -> None:
    conn.execute("UPDATE tokens SET last_status = ?, last_rejection = ? WHERE token_address = ?",
                 (status, rejection, token_address))


def tracked_tokens_due(conn: sqlite3.Connection, now: float, s) -> list[str]:
    """Tokens first seen within the tracking window that are due for re-observation,
    whether or not they are still in a discovery feed and whether or not they passed
    filters. Tokens whose pair can still satisfy the age filter (or whose age is
    unknown) use the fast cadence; the rest use the slow cadence, which is enough to
    record disappearance without storing an observation every 30 s."""
    rows = conn.execute(
        "SELECT t.token_address, t.last_observed_at, "
        "(SELECT o.pair_created_at FROM observations o WHERE o.token_address = t.token_address "
        " AND o.status = 'ok' ORDER BY o.id DESC LIMIT 1) AS pair_created_at "
        "FROM tokens t WHERE t.first_seen_at >= ?",
        (now - s.track_window_minutes * 60.0,),
    ).fetchall()
    slack = s.poll_interval_seconds / 2.0  # cycles are not perfectly periodic
    due = []
    for r in rows:
        if r["last_observed_at"] is None:
            due.append(r["token_address"])
            continue
        pc = r["pair_created_at"]
        candidate = pc is None or now - pc <= s.max_pair_age_minutes * 60.0
        interval = s.track_poll_interval_seconds if candidate else s.slow_track_poll_interval_seconds
        if now - r["last_observed_at"] >= interval - slack:
            due.append(r["token_address"])
    return due
