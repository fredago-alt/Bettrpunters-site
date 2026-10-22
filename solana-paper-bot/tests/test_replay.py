import json

import pytest

from conftest import T0, make_settings, make_snap
from paperbot.db import open_db
from paperbot.replay import (FrozenRulesMismatch, cost_scenarios, freeze_rules, replay, replay_out_of_sample,
                             time_split)
from paperbot.store import insert_observation, record_discovery

# SYNTHETIC test data only. It exercises the replay mechanics; it says nothing
# about real-market profitability.


def _obs(conn, t, token, pair, price, fx=1.5, **kw):
    kw.setdefault("created", t - 5 * 60)
    record_discovery(conn, token, "solana", "synthetic", t)
    insert_observation(conn, t, snap=make_snap(token=token, pair=pair, price=price, **kw),
                       aud_per_usd=fx, fx_source="synthetic")


def _missing(conn, t, token, pair):
    insert_observation(conn, t, token_address=token, pair_address=pair, missing_reason="synthetic_gone")


def _path(conn, t, token, pair, first, last):
    """Signal at t, fill obs at t+15, then `last` price every 60 s for 17 minutes."""
    created = t - 5 * 60
    _obs(conn, t, token, pair, first, created=created)
    _obs(conn, t + 15, token, pair, first, created=created)
    for k in range(1, 18):
        _obs(conn, t + 15 + k * 60, token, pair, last, created=created)
    return t + 15 + 18 * 60


def build_source(conn, t0=T0):
    """Three sequential tokens: A pumps (TP), B dumps (SL), C vanishes (write-off)."""
    t = _path(conn, t0, "A", "PA", 1.0, 1.3)
    t = _path(conn, t, "B", "PB", 1.0, 0.8)
    # C: fills then disappears forever
    _obs(conn, t, "C", "PC", 1.0)
    _obs(conn, t + 15, "C", "PC", 1.0)
    for k in range(1, 6):
        _missing(conn, t + 15 + k * 15 * 60, "C", "PC")
    _obs(conn, t + 4 * 3600, "Z", "PZ", 1.0, liquidity=10)  # unrelated, advances the clock


def settings(**kw):
    return make_settings(latency_seconds=3.0, **kw)


@pytest.fixture
def src():
    conn = open_db(":memory:")
    build_source(conn)
    return conn


def test_replay_produces_expected_trades(src):
    res = replay(src, settings())
    rows = res.conn.execute("SELECT token_address, exit_reason, entry_ref_price_usd FROM positions "
                            "WHERE status='closed' ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("A", "take_profit"), ("B", "stop_loss"), ("C", "stale_writeoff")]
    # entries filled at the first observation after latency (t+15), not the signal observation
    assert all(r[2] == 1.0 for r in rows)
    assert res.metrics.trades == 3 and res.metrics.stale_writeoffs == 1
    assert res.metrics.failed_exit_attempts >= 1
    assert not res.metrics.sufficient_sample


def test_replay_is_deterministic(src):
    a = replay(src, settings(entry_fill_failure_prob=0.3, exit_fill_failure_prob=0.3)).metrics
    b = replay(src, settings(entry_fill_failure_prob=0.3, exit_fill_failure_prob=0.3)).metrics
    assert a.as_dict() | {"run_id": ""} == b.as_dict() | {"run_id": ""}


def test_no_look_ahead_results_unchanged_by_future_data():
    """Replaying a window must give identical results whether or not later data exists."""
    early = open_db(":memory:")
    full = open_db(":memory:")
    for c in (early, full):
        build_source(c)
    # Only `full` gets a big future pump for token A after the window end.
    end = T0 + 5 * 3600
    _obs(full, end + 10, "A", "PA", 50.0)
    _obs(full, end + 20, "NEW", "PN", 1.0)
    r1 = replay(early, settings(), end=end).metrics.as_dict()
    r2 = replay(full, settings(), end=end).metrics.as_dict()
    r1["run_id"] = r2["run_id"] = ""
    assert r1 == r2


def test_window_end_leaves_position_open_instead_of_peeking(src):
    res = replay(src, settings(), end=T0 + 30)  # A filled at t0+15; its +30% comes later
    assert res.metrics.trades == 0 and res.open_at_end == 1


def test_survivorship_vanished_token_is_included_as_loss(src):
    res = replay(src, settings())
    c = res.conn.execute("SELECT pnl_aud, cost_aud FROM positions WHERE token_address='C'").fetchone()
    assert c["pnl_aud"] == pytest.approx(-c["cost_aud"])  # whole stake lost under the 0% recovery assumption


def test_replay_uses_recorded_fx():
    conn = open_db(":memory:")
    for dt, px in [(0, 1.0), (15, 1.0), (915, 1.0), (930, 1.0)]:
        _obs(conn, T0 + dt, "A", "PA", px, fx=1.6 if dt else 1.4)
    res = replay(conn, settings())
    p = res.conn.execute("SELECT entry_aud_per_usd, exit_aud_per_usd FROM positions").fetchone()
    assert tuple(p) == (1.6, 1.6)


def test_chronological_split(src):
    split = time_split(src, 0.5)
    lo = src.execute("SELECT MIN(observed_at) FROM observations").fetchone()[0]
    hi = src.execute("SELECT MAX(observed_at) FROM observations").fetchone()[0]
    assert split == pytest.approx(lo + (hi - lo) / 2)
    with pytest.raises(ValueError):
        time_split(src, 1.5)


def test_frozen_rules_enforced(tmp_path, src):
    path = str(tmp_path / "frozen.json")
    freeze_rules(settings(), path, frozen_at=T0 + 1094)  # just before B's path
    with pytest.raises(FrozenRulesMismatch):
        replay_out_of_sample(src, settings(take_profit_pct=25), path)
    # cost assumptions are not rules; OOS only sees data after the freeze (so token A is excluded)
    res = replay_out_of_sample(src, settings(fee_pct_per_side=0.5), path)
    tokens = {r[0] for r in res.conn.execute("SELECT token_address FROM positions")}
    assert "A" not in tokens and "B" in tokens
    doc = json.load(open(path))
    assert doc["rules"]["take_profit_pct"] == 15.0


def test_cost_scenarios_more_costs_never_help(src):
    rows = cost_scenarios(src, settings(), fees=(0.0, 1.0, 2.0), slippages=(0.0, 1.5, 5.0))
    assert len(rows) == 9
    by = {(r["fee_pct"], r["slippage_pct"]): r for r in rows}
    assert by[(0.0, 0.0)]["fees_aud"] == 0 and by[(0.0, 0.0)]["slippage_aud"] == 0
    assert by[(2.0, 5.0)]["net_pnl_aud"] < by[(1.0, 1.5)]["net_pnl_aud"] < by[(0.0, 0.0)]["net_pnl_aud"]
