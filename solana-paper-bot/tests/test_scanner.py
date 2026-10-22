import random

from conftest import T0, make_settings, make_snap
from paperbot.db import ensure_run, open_db
from paperbot.dexscreener import ApiError
from paperbot.engine import PaperEngine
from paperbot.fx import StaticFx
from paperbot.scanner import Scanner


class FakeClient:
    def __init__(self):
        self.profiles: list[str] = []
        self.token_pairs: dict[str, object] = {}
        self.pair_snaps: dict[str, object] = {}
        self.fail_discovery = False
        self.stats = {}
        self.pair_calls = 0

    def latest_feed(self, feed, chain):
        if self.fail_discovery:
            raise ApiError("HTTP 503")
        return [{"chainId": "solana", "tokenAddress": t} for t in self.profiles]

    def pairs_for_tokens(self, tokens, chain):
        return [self.token_pairs[t] for t in tokens if t in self.token_pairs]

    def pairs_by_address(self, pairs, chain):
        self.pair_calls += 1
        return [self.pair_snaps[p] for p in pairs if p in self.pair_snaps]


def setup(**kw):
    s = make_settings(poll_interval_seconds=15, track_poll_interval_seconds=30, **kw)
    conn = open_db(":memory:")
    ensure_run(conn, "live", "live", s)
    clock = [T0]
    engine = PaperEngine(conn, "live", s, StaticFx(1.5), rng=random.Random(0))
    client = FakeClient()
    sc = Scanner(conn, s, client, engine, clock=lambda: clock[0], sleep=lambda d: clock.__setitem__(0, clock[0] + d))
    return sc, client, conn, clock


def test_records_every_token_including_rejected():
    sc, client, conn, clock = setup()
    client.profiles = ["GOOD", "BAD"]
    client.token_pairs = {"GOOD": make_snap(token="GOOD", pair="PG"),
                          "BAD": make_snap(token="BAD", pair="PB", liquidity=100)}
    sc.run_cycle()
    rows = {r["token_address"]: r for r in conn.execute("SELECT * FROM tokens")}
    assert set(rows) == {"GOOD", "BAD"}
    assert rows["BAD"]["last_status"] == "rejected" and "liquidity_below_min" in rows["BAD"]["last_rejection"]
    assert conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM filter_decisions").fetchone()[0] == 2


def test_tracks_tokens_after_they_leave_feed_and_records_disappearance():
    sc, client, conn, clock = setup()
    client.profiles = ["X"]
    client.token_pairs = {"X": make_snap(token="X", pair="PX", liquidity=100)}
    sc.run_cycle()
    client.profiles = []  # gone from discovery feed
    del client.token_pairs["X"]  # and the API no longer returns a pair
    clock[0] += 31
    sc.run_cycle()
    statuses = [r[0] for r in conn.execute("SELECT status FROM observations WHERE token_address='X' ORDER BY id")]
    assert statuses == ["ok", "missing"]


def test_positions_update_even_when_discovery_fails():
    sc, client, conn, clock = setup()
    client.profiles = ["A"]
    client.token_pairs = {"A": make_snap(token="A", pair="PA", price=0.001)}
    sc.run_cycle()
    assert conn.execute("SELECT status FROM positions").fetchone()[0] == "open"
    client.fail_discovery = True
    client.pair_snaps = {"PA": make_snap(token="A", pair="PA", price=0.0013)}
    clock[0] += 15
    stats = sc.run_cycle()
    assert stats["errors"] == 1
    assert conn.execute("SELECT exit_reason FROM positions").fetchone()[0] == "take_profit"


def test_missing_position_pair_counts_as_failed_exit():
    sc, client, conn, clock = setup()
    client.profiles = ["A"]
    client.token_pairs = {"A": make_snap(token="A", pair="PA", price=0.001)}
    sc.run_cycle()
    clock[0] += 16 * 60
    sc.run_cycle()  # pair_snaps empty -> missing observation for the open pair
    # one failure on the regular refresh, one on the post-latency retry; then wait for next cycle
    p = conn.execute("SELECT status, failed_exit_attempts FROM positions").fetchone()
    assert tuple(p) == ("open", 2)


def test_latency_refetch_fills_pending_entry_in_same_cycle():
    sc, client, conn, clock = setup(latency_seconds=3.0)
    client.profiles = ["A"]
    client.token_pairs = {"A": make_snap(token="A", pair="PA", price=0.001)}
    client.pair_snaps = {"PA": make_snap(token="A", pair="PA", price=0.00102)}
    sc.run_cycle()
    p = conn.execute("SELECT status, entry_ref_price_usd, opened_at - signal_at FROM positions").fetchone()
    assert p[0] == "open" and p[1] == 0.00102 and p[2] >= 3.0


def test_multiple_discovery_feeds_deduplicate_tokens():
    sc, client, conn, clock = setup(discovery_feeds=("TOKEN-PROFILES", "TOKEN-BOOSTS"))
    client.profiles = ["A", "B"]
    client.token_pairs = {"A": make_snap(token="A", pair="PA", liquidity=1)}
    sc.run_cycle()
    assert conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM discovery_events").fetchone()[0] == 4
    sources = {r[0] for r in conn.execute("SELECT source FROM discovery_events")}
    assert sources == {"token-profiles", "token-boosts"}


def test_tracking_cadence_fast_for_candidates_slow_for_old_pairs():
    sc, client, conn, clock = setup()
    client.profiles = ["YOUNG", "OLD"]
    client.token_pairs = {"YOUNG": make_snap(token="YOUNG", pair="PY", liquidity=1, created=T0 - 120),
                          "OLD": make_snap(token="OLD", pair="PO", liquidity=1, created=T0 - 3 * 3600)}
    for _ in range(8):  # 2 minutes of 15 s cycles
        sc.run_cycle()
        clock[0] += 15
    n = dict(conn.execute("SELECT token_address, COUNT(*) FROM observations GROUP BY 1").fetchall())
    assert n["YOUNG"] == 4  # every 30 s
    assert n["OLD"] == 1  # slow cadence (600 s)
    clock[0] += 600
    sc.run_cycle()
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE token_address='OLD'").fetchone()[0] == 2


def test_equity_snapshot_each_cycle():
    sc, client, conn, clock = setup()
    sc.run_cycle()
    sc.run_cycle()
    assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 2
