"""Historical replay over recorded observations, plus frozen-rule out-of-sample tests.

Bias controls
* No look-ahead: observations are fed strictly in (observed_at, id) order and
  the engine only sees the current observation. Fills use the first observation
  at/after signal + latency, i.e. a price that would genuinely have been later.
  The FX rate is the one recorded with each observation, not today's rate.
* No survivorship bias: the universe is every observation recorded in the
  period, including tokens that were rejected, vanished from the feed or
  stopped returning pair data ('missing' observations). Nothing is filtered on
  information from after the observation time.
* Frozen rules: ``freeze_rules`` writes a fingerprint of the strategy rules and
  a timestamp; ``replay_out_of_sample`` refuses to run if the rules changed and
  only uses data observed after the freeze.

Data limitation: replay resolution equals the recording cadence (by default
15 s for positions, 30 s for tracked tokens). DEX Screener's public API offers
no historical tick data, so replay can only use data this bot recorded itself.
"""

from __future__ import annotations

import json
import random
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Iterator

from .db import ensure_run, open_db
from .engine import PaperEngine
from .fx import FxQuote
from .metrics import Metrics, compute
from .store import Observation


class ReplayFx:
    """FX provider that returns the rate recorded alongside each observation."""

    def __init__(self, fallback_aud_per_usd: float):
        self._q = FxQuote(fallback_aud_per_usd, "static-fallback-placeholder", 0.0)

    def update(self, obs: Observation) -> None:
        if obs.aud_per_usd:
            self._q = FxQuote(obs.aud_per_usd, obs.fx_source or "recorded", obs.observed_at)

    def get(self) -> FxQuote:
        return self._q


def iter_observations(src: sqlite3.Connection, start: float | None = None, end: float | None = None,
                      batch: int = 5000) -> Iterator[Observation]:
    where, params = [], []
    if start is not None:
        where.append("observed_at >= ?")
        params.append(start)
    if end is not None:
        where.append("observed_at < ?")
        params.append(end)
    sql = "SELECT * FROM observations" + (" WHERE " + " AND ".join(where) if where else "") + \
        " ORDER BY observed_at, id"
    cur = src.execute(sql, params)
    while True:
        rows = cur.fetchmany(batch)
        if not rows:
            return
        for r in rows:
            yield Observation.from_row(r)


@dataclass
class ReplayResult:
    run_id: str
    conn: sqlite3.Connection
    metrics: Metrics
    observations: int
    first_obs_at: float | None
    last_obs_at: float | None
    open_at_end: int


def replay(src: sqlite3.Connection, settings, start: float | None = None, end: float | None = None,
           out_path: str = ":memory:", run_id: str | None = None, label: str = "replay",
           snapshot_every_seconds: float = 60.0) -> ReplayResult:
    run_id = run_id or f"{label}-{uuid.uuid4().hex[:8]}"
    conn = open_db(out_path)
    ensure_run(conn, run_id, "replay", settings,
               notes=json.dumps({"start": start, "end": end, "label": label, "created": time.time()}))
    fx = ReplayFx(settings.fx_static_aud_per_usd)
    engine = PaperEngine(conn, run_id, settings, fx, rng=random.Random(settings.random_seed),
                         track_token_status=False)
    n, next_snap, first_t, last_t = 0, None, None, None
    conn.execute("BEGIN")
    try:
        for obs in iter_observations(src, start, end):
            fx.update(obs)
            engine.on_clock(obs.observed_at)
            engine.on_observation(obs)
            n += 1
            first_t = obs.observed_at if first_t is None else first_t
            last_t = obs.observed_at
            if next_snap is None or obs.observed_at >= next_snap:
                engine.snapshot(obs.observed_at)
                next_snap = obs.observed_at + snapshot_every_seconds
        if last_t is not None:
            engine.snapshot(last_t)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return ReplayResult(run_id, conn, compute(conn, run_id), n, first_t, last_t, engine.summary().open_positions)


def observation_span(src: sqlite3.Connection) -> tuple[float | None, float | None, int]:
    r = src.execute("SELECT MIN(observed_at), MAX(observed_at), COUNT(*) FROM observations").fetchone()
    return r[0], r[1], r[2]


def time_split(src: sqlite3.Connection, fraction: float) -> float:
    """Timestamp splitting the recorded period chronologically (never shuffled)."""
    if not 0 < fraction < 1:
        raise ValueError("fraction must be in (0, 1)")
    lo, hi, n = observation_span(src)
    if not n:
        raise ValueError("no observations recorded yet")
    return lo + (hi - lo) * fraction


def freeze_rules(settings, path: str, frozen_at: float | None = None) -> dict:
    doc = {
        "frozen_at": frozen_at if frozen_at is not None else time.time(),
        "rules_fingerprint": settings.rules_fingerprint(),
        "rules": settings.strategy_rules(),
        "note": "Out-of-sample evaluation may only use data observed after frozen_at with these exact rules.",
    }
    with open(path, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    return doc


class FrozenRulesMismatch(RuntimeError):
    pass


def replay_out_of_sample(src: sqlite3.Connection, settings, frozen_path: str, out_path: str = ":memory:",
                         end: float | None = None) -> ReplayResult:
    with open(frozen_path) as fh:
        frozen = json.load(fh)
    if frozen["rules_fingerprint"] != settings.rules_fingerprint():
        raise FrozenRulesMismatch(
            f"Current rules {settings.rules_fingerprint()} != frozen rules {frozen['rules_fingerprint']}. "
            "Out-of-sample testing must use the frozen rules unchanged.")
    return replay(src, settings, start=frozen["frozen_at"], end=end, out_path=out_path, label="oos")


DEFAULT_FEE_GRID = (0.25, 0.5, 1.0, 2.0)
DEFAULT_SLIPPAGE_GRID = (0.5, 1.5, 3.0, 5.0)


def cost_scenarios(src: sqlite3.Connection, settings, start: float | None = None, end: float | None = None,
                   fees=DEFAULT_FEE_GRID, slippages=DEFAULT_SLIPPAGE_GRID) -> list[dict]:
    rows = []
    for fee in fees:
        for slip in slippages:
            s = settings.replace(fee_pct_per_side=fee, slippage_pct_per_side=slip)
            res = replay(src, s, start, end, label=f"scenario-f{fee}-s{slip}")
            m = res.metrics
            rows.append({"fee_pct": fee, "slippage_pct": slip, "trades": m.trades, "win_rate": m.win_rate,
                         "net_pnl_aud": m.net_pnl_aud, "expectancy_aud": m.expectancy_aud,
                         "profit_factor": m.profit_factor, "max_dd_aud": m.max_drawdown_aud,
                         "fees_aud": m.fees_aud, "slippage_aud": m.slippage_aud})
            res.conn.close()
    return rows
