import pytest

from paperbot.costs import CostModel, round_trip_breakeven_move, simulate_buy, simulate_sell

FX = 1.5


def test_buy_spends_exactly_budget_and_fee_is_on_notional():
    c = CostModel(fee_pct=1.0, slippage_pct=1.5)
    b = simulate_buy(5.0, 0.002, FX, c)
    assert b.cash_out_aud == pytest.approx(5.0)
    assert b.notional_aud + b.fee_aud == pytest.approx(5.0)
    assert b.fee_aud == pytest.approx(b.notional_aud * 0.01)


def test_buy_uses_executable_price_above_reference():
    c = CostModel(fee_pct=1.0, slippage_pct=1.5)
    b = simulate_buy(5.0, 0.002, FX, c)
    assert b.exec_price_usd == pytest.approx(0.002 * 1.015)
    # quantity is notional / executable price (in USD), NOT notional / mid price
    assert b.qty == pytest.approx((b.notional_aud / FX) / (0.002 * 1.015))
    assert b.qty < (b.notional_aud / FX) / 0.002


def test_sell_uses_executable_price_below_reference():
    c = CostModel(fee_pct=1.0, slippage_pct=1.5)
    s = simulate_sell(1000.0, 0.002, FX, c)
    assert s.exec_price_usd == pytest.approx(0.002 * 0.985)
    assert s.gross_aud == pytest.approx(1000 * 0.002 * 0.985 * FX)
    assert s.fee_aud == pytest.approx(s.gross_aud * 0.01)
    assert s.proceeds_aud == pytest.approx(s.gross_aud - s.fee_aud)


@pytest.mark.parametrize("fee", [0.0, 0.25, 0.5, 1.0, 2.0])
@pytest.mark.parametrize("slip", [0.0, 0.5, 1.5, 3.0, 5.0])
def test_flat_round_trip_loss_equals_costs_exactly_once(fee, slip):
    """If the reference price is unchanged, the loss must equal buy fee + sell fee +
    buy slippage + sell slippage, each counted once. Any double counting breaks this."""
    c = CostModel(fee, slip)
    b = simulate_buy(5.0, 0.0004, FX, c)
    s = simulate_sell(b.qty, 0.0004, FX, c)
    loss = b.cash_out_aud - s.proceeds_aud
    assert loss == pytest.approx(b.fee_aud + s.fee_aud + b.slippage_aud + s.slippage_aud, abs=1e-12)


def test_zero_costs_flat_round_trip_is_break_even():
    c = CostModel(0.0, 0.0)
    b = simulate_buy(5.0, 0.123, FX, c)
    s = simulate_sell(b.qty, 0.123, FX, c)
    assert s.proceeds_aud == pytest.approx(5.0)


def test_pnl_reflects_price_move_and_fx():
    c = CostModel(0.0, 0.0)
    b = simulate_buy(5.0, 0.01, 1.5, c)
    s = simulate_sell(b.qty, 0.012, 1.5, c)  # +20% in USD, same FX
    assert s.proceeds_aud == pytest.approx(6.0)
    s2 = simulate_sell(b.qty, 0.01, 1.65, c)  # flat USD price, AUD weakens 10%
    assert s2.proceeds_aud == pytest.approx(5.5)


def test_higher_costs_strictly_reduce_proceeds():
    prev = None
    for fee, slip in [(0, 0), (0.25, 0.5), (1.0, 1.5), (2.0, 5.0)]:
        c = CostModel(fee, slip)
        b = simulate_buy(5.0, 0.01, FX, c)
        p = simulate_sell(b.qty, 0.0115, FX, c).proceeds_aud
        if prev is not None:
            assert p < prev
        prev = p


def test_breakeven_move_matches_simulation():
    c = CostModel(1.0, 1.5)
    move = round_trip_breakeven_move(c)
    b = simulate_buy(5.0, 1.0, FX, c)
    s = simulate_sell(b.qty, 1.0 * (1 + move / 100), FX, c)
    assert s.proceeds_aud == pytest.approx(5.0)
    assert 5.0 < move < 5.3  # ~5.13% with 1% fee + 1.5% slippage per side


def test_size_impact_slippage_scales_with_order_vs_liquidity():
    c = CostModel(0.0, 1.0, impact_factor=1.0)
    assert c.slippage_fraction(1000, 100_000) == pytest.approx(0.02)
    assert c.slippage_fraction(1000, None) == pytest.approx(0.01)
    assert c.slippage_fraction(10**9, 1) == 0.5  # capped


def test_invalid_inputs():
    c = CostModel(1, 1)
    with pytest.raises(ValueError):
        simulate_buy(0, 1.0, FX, c)
    with pytest.raises(ValueError):
        simulate_buy(5, 0, FX, c)
    with pytest.raises(ValueError):
        simulate_sell(0, 1.0, FX, c)
