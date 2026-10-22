"""Executable-price fill simulation with explicit fee and slippage accounting.

Accounting conventions (each cost is applied exactly once):

* Slippage is applied ONLY to the execution price. A buy executes at
  ref * (1 + s); a sell at ref * (1 - s). It never touches cash directly.
  ``slippage_aud`` is an attribution figure (how much worse the execution
  price was than the reference price), NOT an extra deduction.
* Fees are applied ONLY to cash, as a percentage of the executed notional.
  On a buy the fee is paid on top of the notional; on a sell it is deducted
  from gross proceeds.
* P&L = sell proceeds - buy cash outlay. Fees and slippage are already inside
  that number; reporting them separately is attribution, not a second charge.

The reference price is DEX Screener's ``priceUsd`` (last traded price), which
is not a firm quote. All cost parameters are labelled placeholder assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fx import aud_to_usd, usd_to_aud

MAX_SLIPPAGE_FRACTION = 0.5


@dataclass(frozen=True)
class CostModel:
    fee_pct: float  # per side, % of notional
    slippage_pct: float  # per side, % of reference price
    impact_factor: float = 0.0  # extra slippage fraction = factor * order_usd / liquidity_usd

    @classmethod
    def from_settings(cls, s) -> "CostModel":
        return cls(s.fee_pct_per_side, s.slippage_pct_per_side, s.slippage_impact_factor)

    @property
    def fee_rate(self) -> float:
        return self.fee_pct / 100.0

    def slippage_fraction(self, order_usd: float = 0.0, liquidity_usd: float | None = None) -> float:
        frac = self.slippage_pct / 100.0
        if self.impact_factor and liquidity_usd and liquidity_usd > 0:
            frac += self.impact_factor * order_usd / liquidity_usd
        return min(frac, MAX_SLIPPAGE_FRACTION)


@dataclass(frozen=True)
class BuyFill:
    qty: float
    ref_price_usd: float
    exec_price_usd: float
    aud_per_usd: float
    notional_aud: float  # qty * exec price, in AUD
    fee_aud: float
    slippage_aud: float  # attribution only
    cash_out_aud: float  # notional + fee == budget


@dataclass(frozen=True)
class SellFill:
    qty: float
    ref_price_usd: float
    exec_price_usd: float
    aud_per_usd: float
    gross_aud: float  # qty * exec price, in AUD
    fee_aud: float
    slippage_aud: float  # attribution only
    proceeds_aud: float  # gross - fee


def simulate_buy(budget_aud: float, ref_price_usd: float, aud_per_usd: float, cost: CostModel,
                 liquidity_usd: float | None = None) -> BuyFill:
    """Spend exactly ``budget_aud`` (fee included) at the executable ask."""
    if budget_aud <= 0:
        raise ValueError("budget_aud must be > 0")
    if not ref_price_usd or ref_price_usd <= 0:
        raise ValueError("ref_price_usd must be > 0")
    notional_aud = budget_aud / (1.0 + cost.fee_rate)
    fee_aud = budget_aud - notional_aud
    s = cost.slippage_fraction(aud_to_usd(notional_aud, aud_per_usd), liquidity_usd)
    exec_price = ref_price_usd * (1.0 + s)
    qty = aud_to_usd(notional_aud, aud_per_usd) / exec_price
    slippage_aud = usd_to_aud(qty * (exec_price - ref_price_usd), aud_per_usd)
    return BuyFill(qty, ref_price_usd, exec_price, aud_per_usd, notional_aud, fee_aud, slippage_aud, budget_aud)


def simulate_sell(qty: float, ref_price_usd: float, aud_per_usd: float, cost: CostModel,
                  liquidity_usd: float | None = None) -> SellFill:
    """Sell ``qty`` tokens at the executable bid."""
    if qty <= 0:
        raise ValueError("qty must be > 0")
    if ref_price_usd is None or ref_price_usd < 0:
        raise ValueError("ref_price_usd must be >= 0")
    s = cost.slippage_fraction(qty * ref_price_usd, liquidity_usd)
    exec_price = ref_price_usd * (1.0 - s)
    gross_aud = usd_to_aud(qty * exec_price, aud_per_usd)
    fee_aud = gross_aud * cost.fee_rate
    slippage_aud = usd_to_aud(qty * (ref_price_usd - exec_price), aud_per_usd)
    return SellFill(qty, ref_price_usd, exec_price, aud_per_usd, gross_aud, fee_aud, slippage_aud, gross_aud - fee_aud)


def round_trip_breakeven_move(cost: CostModel) -> float:
    """Reference-price % move needed to break even after both sides' costs."""
    s = cost.slippage_fraction()
    f = cost.fee_rate
    return ((1 + f) * (1 + s) / ((1 - s) * (1 - f)) - 1) * 100.0
