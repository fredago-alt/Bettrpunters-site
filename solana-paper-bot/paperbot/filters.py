"""Entry filters. Each rule returns a rejection reason code or nothing.

IMPORTANT: passing these filters is NOT evidence that a token is safe,
legitimate or sellable. DEX Screener data cannot detect honeypots, mint/freeze
authorities, rug-pull risk or wash trading. The filters only narrow which
tokens the *simulation* will consider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SnapshotLike(Protocol):
    price_usd: float | None
    liquidity_usd: float | None
    buys_m5: int | None
    sells_m5: int | None
    price_change_m5: float | None
    pair_created_at: float | None
    quote_symbol: str | None


@dataclass(frozen=True)
class FilterResult:
    passed: bool
    reasons: list[str]
    metrics: dict[str, float | None]


def evaluate(snap: SnapshotLike, now: float, s) -> FilterResult:
    reasons: list[str] = []
    m: dict[str, float | None] = {}

    if snap.price_usd is None or snap.price_usd <= 0:
        reasons.append("price_unavailable")

    if snap.quote_symbol not in s.allowed_quote_symbols:
        reasons.append(f"quote_not_allowed:{snap.quote_symbol}")

    if snap.liquidity_usd is None:
        reasons.append("liquidity_unavailable")
    elif snap.liquidity_usd < s.min_liquidity_usd:
        reasons.append("liquidity_below_min")
    m["liquidity_usd"] = snap.liquidity_usd

    if snap.pair_created_at is None:
        reasons.append("pair_age_unavailable")
        m["pair_age_min"] = None
    else:
        age_min = (now - snap.pair_created_at) / 60.0
        m["pair_age_min"] = age_min
        if age_min < s.min_pair_age_minutes:
            reasons.append("pair_too_new")
        elif age_min > s.max_pair_age_minutes:
            reasons.append("pair_too_old")

    buys, sells = snap.buys_m5, snap.sells_m5
    if buys is None or sells is None:
        reasons.append("txns_unavailable")
        m["txns_m5"] = m["buy_sell_ratio"] = None
    else:
        m["txns_m5"] = buys + sells
        if buys + sells < s.min_txns_m5:
            reasons.append("txns_below_min")
        if sells == 0:
            m["buy_sell_ratio"] = None
            # No observed sells means sellability is unverified (possible honeypot).
            if s.reject_zero_sells:
                reasons.append("no_sells_observed")
        else:
            ratio = buys / sells
            m["buy_sell_ratio"] = ratio
            if ratio < s.min_buy_sell_ratio:
                reasons.append("buy_sell_ratio_below_min")

    if snap.price_change_m5 is not None:
        m["price_change_m5"] = snap.price_change_m5
        if abs(snap.price_change_m5) > s.max_price_change_m5_pct:
            reasons.append("price_change_m5_extreme")

    return FilterResult(not reasons, reasons, m)
