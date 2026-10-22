import random

import pytest

from paperbot.config import Settings
from paperbot.db import ensure_run, open_db
from paperbot.dexscreener import PairSnapshot
from paperbot.engine import PaperEngine
from paperbot.fx import StaticFx
from paperbot.store import insert_observation, record_discovery

T0 = 1_800_000_000.0  # fixed synthetic clock origin
FX = 1.5  # AUD per USD used in tests


def make_settings(**kw) -> Settings:
    base = dict(db_path=":memory:", fx_mode="static", fx_static_aud_per_usd=FX, latency_seconds=0.0,
                entry_fill_failure_prob=0.0, exit_fill_failure_prob=0.0)
    base.update(kw)
    s = Settings(**base)
    s.validate()
    return s


@pytest.fixture
def settings():
    return make_settings()


def make_snap(token="TOKEN1", pair="PAIR1", price=0.001, liquidity=50_000.0, buys=30, sells=10,
              created=T0 - 5 * 60, quote="SOL", change_m5=5.0, symbol="TKN") -> PairSnapshot:
    raw = {"chainId": "solana", "dexId": "raydium", "pairAddress": pair,
           "baseToken": {"address": token, "symbol": symbol, "name": symbol},
           "quoteToken": {"symbol": quote}, "priceUsd": None if price is None else str(price),
           "liquidity": {"usd": liquidity} if liquidity is not None else {},
           "txns": {"m5": {"buys": buys, "sells": sells}, "h1": {"buys": buys, "sells": sells}},
           "volume": {"m5": 1000, "h1": 5000}, "priceChange": {"m5": change_m5, "h1": change_m5},
           "pairCreatedAt": int(created * 1000) if created else None}
    return PairSnapshot.from_api(raw)


class Harness:
    """Engine + DB with helpers to feed observations at chosen times."""

    def __init__(self, settings, run_id="test", seed=1):
        self.s = settings
        self.conn = open_db(":memory:")
        self.run_id = run_id
        ensure_run(self.conn, run_id, "live", settings)
        self.engine = PaperEngine(self.conn, run_id, settings, StaticFx(settings.fx_static_aud_per_usd),
                                  rng=random.Random(seed))

    def feed(self, t, **snap_kw):
        snap = make_snap(**snap_kw)
        record_discovery(self.conn, snap.token_address, "solana", "test", t)
        obs = insert_observation(self.conn, t, snap=snap, aud_per_usd=self.s.fx_static_aud_per_usd, fx_source="test")
        self.engine.on_observation(obs)
        return obs

    def feed_missing(self, t, token="TOKEN1", pair="PAIR1"):
        record_discovery(self.conn, token, "solana", "test", t)
        obs = insert_observation(self.conn, t, token_address=token, pair_address=pair, missing_reason="gone")
        self.engine.on_observation(obs)
        return obs

    def position(self, token="TOKEN1"):
        return self.conn.execute("SELECT * FROM positions WHERE run_id=? AND token_address=? ORDER BY id DESC",
                                 (self.run_id, token)).fetchone()


@pytest.fixture
def harness(settings):
    return Harness(settings)
