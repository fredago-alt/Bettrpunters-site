import pytest

from paperbot.db import open_db
from paperbot.research import Costs, entry, features_at, horizon_return, rule_return, run_research
from paperbot.sniper.pumpfun import NATIVE_SOL_QUOTE

T0 = 1_800_000_000.0


def add_token(conn, mint, seen_at, prices, buyers=10, creator_sold_at=None, mayhem=0):
    """prices: list of (seconds_after_launch, close_price); 5 s bars."""
    conn.execute("INSERT INTO pump_tokens(mint, bonding_curve, mayhem, quote_mint, seen_at, status, creator_fee_bps) "
                 "VALUES (?,?,?,?,?, 'watching', 30)", (mint, mint + "c", mayhem, NATIVE_SOL_QUOTE, seen_at))
    prev = prices[0][1]
    for k, (dt, p) in enumerate(prices):
        conn.execute(
            "INSERT INTO pump_bars(mint, bar_start, bar_end, n_buys, n_sells, buy_lamports, sell_lamports, "
            "unique_buyers, open_price_sol, high_price_sol, low_price_sol, close_price_sol, real_token, "
            "creator_sold, sol_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mint, seen_at + dt - 5, seen_at + dt, 3, 1, 2 * 10**9, 10**8, min(buyers, 3 * (k + 1)), prev,
             max(prev, p), min(prev, p), p, 700_000_000 * 10**6,
             int(creator_sold_at is not None and dt >= creator_sold_at), 150.0))
        prev = p


def tok(conn, mint):
    from paperbot.research import load_tokens
    return next(t for t in load_tokens(conn) if t.mint == mint)


def test_costs_flat_round_trip_equals_fees():
    c = Costs(position_aud=5, aud_per_usd=1.5, network_lamports=0)
    assert c.net_return(0.0, 125, 150) == pytest.approx(((1 / 1.0125) * 0.9875 - 1) * 100)
    small = Costs(position_aud=0.5, network_lamports=5_005_000)
    assert small.net_return(1.0, 125, 150) == -100.0  # budget below network fee


def test_features_use_only_bars_ended_by_decision_time():
    conn = open_db(":memory:")
    add_token(conn, "A", T0, [(5, 1.0), (10, 1.1), (15, 5.0), (20, 6.0)])
    t = tok(conn, "A")
    f = features_at(t, 10)
    assert f["price_change_pct"] == pytest.approx(10.0)  # the 5.0 bar ends at 15 s: not visible at 10 s
    assert features_at(t, 2) is None


def test_entry_uses_a_later_bar_and_horizon_return():
    conn = open_db(":memory:")
    add_token(conn, "A", T0, [(5, 1.0), (10, 1.0), (15, 2.0), (40, 2.0)])
    t = tok(conn, "A")
    t_in, p_in, _ = entry(t, 10)  # decision at 10 s -> first bar ending after 10.5 s
    assert (t_in - T0, p_in) == (15, 2.0)
    assert horizon_return(t, 10, 30, Costs(network_lamports=0)) == pytest.approx(
        Costs(network_lamports=0).net_return(0.0, 125, 150))


def test_rule_exits_stop_first_and_on_creator_sell():
    conn = open_db(":memory:")
    add_token(conn, "A", T0, [(5, 1.0), (10, 1.0), (15, 1.0), (20, 0.5), (25, 2.0)])
    add_token(conn, "B", T0 + 1, [(5, 1.0), (10, 1.0), (15, 1.0), (20, 1.3), (25, 2.0)], creator_sold_at=20)
    c = Costs(network_lamports=0)
    assert rule_return(tok(conn, "A"), 10, c, 30, -15, 120) == pytest.approx(c.net_return(-0.5, 125, 150))
    assert rule_return(tok(conn, "B"), 10, c, 300, -50, 120) == pytest.approx(c.net_return(0.3, 125, 150))


def test_report_runs_and_splits_chronologically():
    conn = open_db(":memory:")
    for i in range(60):
        path = [(5 * k, 1.0 + (0.02 * k if i % 2 else -0.02 * k)) for k in range(1, 100)]
        add_token(conn, f"T{i}", T0 + 100 * i, path, buyers=40)
    add_token(conn, "M", T0, [(5, 1.0), (10, 9.0)], mayhem=1)  # excluded
    text = run_research(conn, Costs())
    assert "Launches recorded (SOL-quoted, non-mayhem): 60" in text
    assert "(30 / 30 tokens)" in text and "Rule/exit combinations tested: 24" in text
    assert "PAPER TRADING ONLY" in text


def test_report_needs_enough_data():
    assert "Not enough" in run_research(open_db(":memory:"), Costs())




def test_launch_ceiling_buys_after_first_trade_and_reports_upper_bound():
    from paperbot.research import launch_ceiling

    conn = open_db(":memory:")
    for i in range(50):
        # first bar opens at 1.0 (after the creator's buy), pumps to 1.3 by 10 s, then dies
        path = [(5, 1.0), (10, 1.3), (15, 0.5), (20, 0.4)]
        add_token(conn, f"L{i}", T0 + 60 * i, path)
    text = launch_ceiling(conn, 5.0, 1.42)
    assert "UPPER BOUND" in text and "hindsight best in 30s" in text
    def mean_after(label):
        line = next(ln for ln in text.splitlines() if label in ln)
        return float(line.split("mean")[1].split("%")[0])

    assert mean_after("sell at +10s") > 20  # +30% before costs
    assert mean_after("sell at +30s") < -50  # the token died
    assert mean_after("hindsight best in 30s") > 20
