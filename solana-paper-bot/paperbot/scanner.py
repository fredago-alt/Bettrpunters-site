"""Live market-data scanner driving the paper engine. PAPER TRADING ONLY.

Each cycle:
  1. Refresh OPEN/PENDING positions by pair address (independent of discovery).
  2. Pull the latest token-profiles feed and log every Solana token seen.
  3. Observe new tokens immediately and keep re-observing every tracked token
     (fast cadence while it could still qualify, slow cadence afterwards),
     recording a 'missing' observation when the API no longer returns a pair,
     so tokens that vanish from feeds or fail filters are still tracked.
  4. If an order is waiting out its simulated latency, sleep that long and
     re-fetch those pairs so the fill uses a genuinely later price.
  5. Housekeeping (stale write-offs) and an equity snapshot.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from . import BANNER
from .db import transaction
from .dexscreener import ApiError, DexScreenerClient, PairSnapshot, best_pair_per_token
from .engine import PaperEngine
from .store import insert_observation, record_discovery, tracked_tokens_due

log = logging.getLogger(__name__)

CHAIN = "solana"


class Scanner:
    def __init__(self, conn, settings, client: DexScreenerClient, engine: PaperEngine,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.conn = conn
        self.s = settings
        self.client = client
        self.engine = engine
        self.clock = clock
        self.sleep = sleep
        self.cycle_errors: list[str] = []

    # ------------------------------------------------------------------
    def _observe(self, snap: PairSnapshot | None, *, token: str | None = None, pair: str | None = None,
                 reason: str | None = None, evaluate_entry: bool = True) -> None:
        fx = self.engine.fx.get()
        with transaction(self.conn):
            obs = insert_observation(self.conn, self.clock(), snap=snap, token_address=token, pair_address=pair,
                                     missing_reason=reason, aud_per_usd=fx.aud_per_usd, fx_source=fx.source,
                                     store_raw=self.s.store_raw_json)
        self.engine.on_observation(obs, evaluate_entry=evaluate_entry)

    def refresh_positions(self, pair_addresses: list[str]) -> int:
        if not pair_addresses:
            return 0
        rows = {r["pair_address"]: r["token_address"] for r in self.engine.active_positions()}
        snaps = {p.pair_address: p for p in self.client.pairs_by_address(pair_addresses, CHAIN)}
        for pair in pair_addresses:
            if pair in snaps:
                self._observe(snaps[pair], evaluate_entry=False)
            elif pair in rows:
                self._observe(None, token=rows[pair], pair=pair, reason="pair_not_returned", evaluate_entry=False)
        return len(pair_addresses)

    def discover(self) -> list[str]:
        now = self.clock()
        new = []
        for feed in self.s.discovery_feeds:
            items = self.client.latest_feed(feed, CHAIN)
            with transaction(self.conn):
                for p in items:
                    if record_discovery(self.conn, p["tokenAddress"], CHAIN, feed.lower(), now,
                                        p if self.s.store_raw_json else None):
                        new.append(p["tokenAddress"])
        return new

    def observe_tokens(self, tokens: list[str]) -> int:
        active = {r["token_address"] for r in self.engine.active_positions()}
        tokens = [t for t in tokens if t not in active]  # positions are tracked by pair in refresh_positions
        if not tokens:
            return 0
        best = best_pair_per_token(self.client.pairs_for_tokens(tokens, CHAIN))
        for t in tokens:
            if t in best:
                self._observe(best[t])
            else:
                self._observe(None, token=t, reason="no_pair_returned")
        return len(tokens)

    def run_cycle(self) -> dict:
        self.cycle_errors = []
        stats = {"positions": 0, "new_tokens": 0, "observed": 0}

        def guarded(name, fn):
            try:
                return fn()
            except ApiError as exc:
                log.error("%s failed: %s", name, exc)
                self.cycle_errors.append(f"{name}: {exc}")
                return None

        stats["positions"] = guarded("positions", lambda: self.refresh_positions(self.engine.open_pair_addresses())) or 0
        new = guarded("discovery", self.discover) or []
        stats["new_tokens"] = len(new)
        due = tracked_tokens_due(self.conn, self.clock(), self.s)  # includes never-observed (new) tokens
        stats["observed"] = guarded("tracking", lambda: self.observe_tokens(due)) or 0

        # Fill orders waiting out simulated latency with a freshly fetched price.
        # Stop once a re-fetch makes no progress (e.g. a failing exit retries next cycle).
        pending = self.engine.pending_pair_addresses()
        for _ in range(3):
            if not pending:
                break
            self.sleep(self.s.latency_seconds)
            guarded("pending-fills", lambda: self.refresh_positions(pending))
            before, pending = pending, self.engine.pending_pair_addresses()
            if pending == before:
                break

        self.engine.on_clock(self.clock())
        acct = self.engine.snapshot(self.clock())
        stats.update(cash_aud=round(acct.cash_aud, 4), equity_aud=round(acct.equity_aud, 4),
                     open=acct.open_positions, closed=acct.closed_trades, errors=len(self.cycle_errors),
                     api=dict(self.client.stats))
        return stats

    def run_forever(self, max_cycles: int | None = None) -> None:
        log.warning(BANNER)
        n = 0
        while max_cycles is None or n < max_cycles:
            started = self.clock()
            try:
                stats = self.run_cycle()
                log.info("cycle %d: %s", n + 1, stats)
            except Exception:  # never let one bad cycle kill a multi-week run
                log.exception("cycle %d crashed; continuing", n + 1)
            n += 1
            if max_cycles is not None and n >= max_cycles:
                break
            self.sleep(max(0.0, self.s.poll_interval_seconds - (self.clock() - started)))
