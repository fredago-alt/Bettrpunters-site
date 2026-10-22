"""Exploratory research on recorded pump.fun launches (from the sniper's pump_bars).

PAPER TRADING ONLY. This is hypothesis generation, not proof of an edge.

Method (bias controls):
* Universe: every SOL-quoted, non-mayhem launch the sniper saw, including tokens
  that rugged or died - nothing is filtered on what happened later.
* No look-ahead: features at decision age T use only bars that ENDED by T.
  The entry price is the close of the first bar ending after T + latency (a
  later, usually worse, price); if no trade happened since, the curve price is
  unchanged, so the last close is the true price.
* Costs: per-token fee (protocol + creator) on both sides plus a flat network
  fee per transaction for the chosen position size.
* Chronological split: the earlier half of launches is "discovery", the later
  half "validation". Thresholds are fixed in advance (no tuning). A rule is
  only flagged PROMISING if it is net-positive in both halves AND the 95%
  bootstrap interval of its validation mean is above zero.
* The number of rules tested is printed: with many rules, some will look good
  by chance.
"""

from __future__ import annotations

import sqlite3
from bisect import bisect_right
from dataclasses import dataclass

from .metrics import bootstrap_mean_ci
from .sniper.pumpfun import (NATIVE_SOL_QUOTE, curve_buy_tokens, curve_progress_pct, curve_sell_lamports,
                             simulate_curve_buy, simulate_curve_sell)

LATENCY_S = 0.5


@dataclass
class Token:
    mint: str
    seen_at: float
    fee_bps: int
    ends: list  # bar_end times, ascending
    bars: list  # sqlite rows, same order


@dataclass(frozen=True)
class Costs:
    position_aud: float = 5.0
    aud_per_usd: float = 1.42
    network_lamports: int = 105_000
    default_sol_usd: float = 150.0

    def net_return(self, gross: float, fee_bps: int, sol_usd: float | None) -> float:
        """Net % for a spot-price return ``gross`` (0.10 = +10%) on a round trip."""
        sol_usd = sol_usd or self.default_sol_usd
        budget_sol = self.position_aud / self.aud_per_usd / sol_usd
        net_sol = self.network_lamports / 1e9
        f = fee_bps / 10_000
        curve_in = (budget_sol - net_sol) / (1 + f)
        if curve_in <= 0:
            return -100.0
        out = curve_in * (1 + gross) * (1 - f) - net_sol
        return (out / budget_sol - 1) * 100


def load_tokens(conn: sqlite3.Connection) -> list[Token]:
    toks = conn.execute(
        "SELECT mint, seen_at, COALESCE(creator_fee_bps, 30) AS cfee FROM pump_tokens "
        "WHERE mayhem = 0 AND (quote_mint IS NULL OR quote_mint = ?) ORDER BY seen_at",
        (NATIVE_SOL_QUOTE,)).fetchall()
    raw: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM pump_bars ORDER BY mint, bar_start, id"):
        raw.setdefault(r["mint"], []).append(r)
    bars = {m: sorted(single_timeline(rows), key=lambda r: r["bar_end"]) for m, rows in raw.items()}
    out = []
    for t in toks:
        b = bars.get(t["mint"], [])
        # creator fee in create events is often 0 while trades show 30 bps; use the larger, plus 95 protocol
        out.append(Token(t["mint"], t["seen_at"], 95 + max(int(t["cfee"] or 0), 30), [x["bar_end"] for x in b], b))
    return out


def single_timeline(rows: list) -> list:
    """Older databases contain the same token's bars written by several sniper processes
    (main + test variants), interleaved in time. Each process's bars never overlap each
    other, so split the bars into non-overlapping chains and keep the chain that saw the
    most trades - i.e. one process's view, without double counting."""
    chains: list[list] = []
    for r in sorted(rows, key=lambda r: (r["bar_start"], r["id"])):
        for ch in chains:
            if ch[-1]["bar_end"] <= r["bar_start"] + 1e-6:
                ch.append(r)
                break
        else:
            chains.append([r])
    if len(chains) <= 1:
        return rows
    return max(chains, key=lambda ch: (sum(b["n_buys"] + b["n_sells"] for b in ch), len(ch)))


def _upto(tok: Token, t: float) -> list:
    return tok.bars[:bisect_right(tok.ends, t)]


def features_at(tok: Token, age: float) -> dict | None:
    """Features from bars ended by seen_at + age, or None if no trade yet."""
    past = _upto(tok, tok.seen_at + age)
    if not past:
        return None
    first_open = past[0]["open_price_sol"] or past[0]["close_price_sol"]
    last = past[-1]
    buys = sum(b["n_buys"] for b in past)
    sells = sum(b["n_sells"] for b in past)
    return {
        "buys": buys, "sells": sells, "unique_buyers": last["unique_buyers"],
        "net_inflow_sol": sum(b["buy_lamports"] - b["sell_lamports"] for b in past) / 1e9,
        "buy_sell_ratio": buys / sells if sells else float(buys),
        "price_change_pct": (last["close_price_sol"] / first_open - 1) * 100 if first_open else 0.0,
        "creator_sold": bool(last["creator_sold"]),
        "curve_progress_pct": curve_progress_pct(last["real_token"]) if last["real_token"] is not None else 0.0,
        "last_trade_age": age - (last["bar_end"] - tok.seen_at),
    }


def price_at(tok: Token, t: float) -> tuple[float | None, float | None]:
    """(close price, sol_usd) as of time t (curve price is unchanged between trades)."""
    past = _upto(tok, t)
    if not past:
        return None, None
    return past[-1]["close_price_sol"], past[-1]["sol_usd"]


def entry(tok: Token, age: float):
    """First bar ending after the decision + latency; else last known price."""
    t = tok.seen_at + age + LATENCY_S
    i = bisect_right(tok.ends, t)
    if i < len(tok.bars):
        b = tok.bars[i]
        return b["bar_end"], b["close_price_sol"], b["sol_usd"]
    p, s = price_at(tok, t)
    return t, p, s


def horizon_return(tok: Token, age: float, horizon: float, costs: Costs) -> float | None:
    t_in, p_in, sol = entry(tok, age)
    if not p_in:
        return None
    p_out, _ = price_at(tok, t_in + horizon)
    return costs.net_return(p_out / p_in - 1, tok.fee_bps, sol)


def rule_return(tok: Token, age: float, costs: Costs, tp: float, sl: float, max_hold: float,
                exit_on_creator_sell: bool = True) -> float | None:
    """Net % with TP/SL on net value and a max hold, using bar high/low.
    Conservative: if one bar touches both, the stop is assumed first; exits fill at
    that bar's close (latency), not at the threshold."""
    t_in, p_in, sol = entry(tok, age)
    if not p_in:
        return None
    i = bisect_right(tok.ends, t_in)
    for b in tok.bars[i:]:
        if b["bar_end"] > t_in + max_hold:
            break
        low = costs.net_return(b["low_price_sol"] / p_in - 1, tok.fee_bps, sol)
        high = costs.net_return(b["high_price_sol"] / p_in - 1, tok.fee_bps, sol)
        if low <= sl or high >= tp or (exit_on_creator_sell and b["creator_sold"]):
            return costs.net_return(b["close_price_sol"] / p_in - 1, tok.fee_bps, sol)
    p_out, _ = price_at(tok, t_in + max_hold)
    return costs.net_return(p_out / p_in - 1, tok.fee_bps, sol)


# Pre-declared rules: (name, decision age s, condition). Fixed before looking at results.
RULES = [
    ("any token alive", 30, lambda f: True),
    ("creator has not sold", 30, lambda f: not f["creator_sold"]),
    ("current sniper entry", 30, lambda f: f["buys"] >= 10 and f["unique_buyers"] >= 8 and f["net_inflow_sol"] >= 3
     and f["buy_sell_ratio"] >= 2 and not f["creator_sold"] and f["curve_progress_pct"] <= 70),
    ("strong crowd @60s", 60, lambda f: f["unique_buyers"] >= 25 and f["net_inflow_sol"] >= 10
     and not f["creator_sold"]),
    ("survivor @120s", 120, lambda f: not f["creator_sold"] and f["unique_buyers"] >= 15
     and f["price_change_pct"] > 0 and f["last_trade_age"] < 15),
    ("survivor @300s", 300, lambda f: not f["creator_sold"] and f["unique_buyers"] >= 30
     and f["price_change_pct"] > 0 and f["last_trade_age"] < 15),
    ("dip after pump @60s", 60, lambda f: not f["creator_sold"] and f["unique_buyers"] >= 15
     and f["price_change_pct"] < -20),
    ("near graduation @120s", 120, lambda f: not f["creator_sold"] and 50 <= f["curve_progress_pct"] <= 80),
]
EXITS = [("TP+30/SL-15/120s", 30.0, -15.0, 120.0), ("TP+60/SL-25/300s", 60.0, -25.0, 300.0),
         ("hold 120s*", 1e9, -100.0, 120.0)]
BASE_AGES = (10, 30, 60, 120)
BASE_HORIZONS = (30, 60, 120, 300)


def _stats(vals: list[float]) -> dict:
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0}
    s = sorted(vals)
    lo, hi, _ = bootstrap_mean_ci(vals, n_resamples=2000)
    return {"n": n, "mean": sum(vals) / n, "median": s[n // 2], "pos": sum(v > 0 for v in vals) / n,
            "lo": lo, "hi": hi}


def _fmt(st: dict) -> str:
    if st["n"] == 0:
        return "n=0"
    ci = "" if st["lo"] != st["lo"] else f" CI[{st['lo']:+.1f},{st['hi']:+.1f}]"
    return f"n={st['n']:<5} mean {st['mean']:+6.1f}% med {st['median']:+6.1f}% win {st['pos']:4.0%}{ci}"


def run_research(conn: sqlite3.Connection, costs: Costs) -> str:
    toks = load_tokens(conn)
    alive = [t for t in toks if t.bars]
    if len(alive) < 40:
        return f"Not enough recorded launches with trades ({len(alive)}); let the sniper record more data first."
    half = len(toks) // 2
    split_at = toks[half].seen_at
    disc = [t for t in alive if t.seen_at < split_at]
    val = [t for t in alive if t.seen_at >= split_at]
    out = [
        "PAPER TRADING ONLY - exploratory research on recorded pump.fun launches (hypotheses, not proof).",
        f"Launches recorded (SOL-quoted, non-mayhem): {len(toks)}; with at least one trade: {len(alive)}.",
        f"Costs: A${costs.position_aud:g} position, per-token fee (~1.25% each way), "
        f"{costs.network_lamports} lamports network fee per transaction.",
        f"Discovery = launches before the median launch time, validation = after ({len(disc)} / {len(val)} tokens).",
        "",
        "A. BASE RATES - buy every live token at age T, sell H seconds later (net % after costs)",
    ]
    for age in BASE_AGES:
        for h in BASE_HORIZONS:
            if age + h > 600:
                continue
            vals = [horizon_return(t, age, h, costs) for t in alive if features_at(t, age)]
            out.append(f"  T={age:>3}s H={h:>3}s  {_fmt(_stats(vals))}")
    out += ["", "B. PRE-DECLARED RULES x EXITS (discovery | validation)"]
    tested = 0
    promising = []
    for name, age, cond in RULES:
        for ex_name, tp, sl, hold in EXITS:
            tested += 1
            res = []
            for group in (disc, val):
                vals = []
                for t in group:
                    f = features_at(t, age)
                    if f and cond(f):
                        vals.append(rule_return(t, age, costs, tp, sl, min(hold, 600 - age)))
                res.append(_stats(vals))
            d, v = res
            flag = ""
            if d["n"] >= 20 and v["n"] >= 20 and d["mean"] > 0 and v["mean"] > 0 and v["lo"] > 0:
                flag = "  <-- PROMISING (needs frozen out-of-sample test)"
                promising.append(f"{name} / {ex_name}")
            out.append(f"  {name:<24} {ex_name:<17}")
            out.append(f"      disc {_fmt(d)}")
            out.append(f"      val  {_fmt(v)}{flag}")
    out += ["", "Exits: holds are capped at the 600 s recording window; every exit also fires if the creator "
                "sells (*including the plain hold).",
            f"Rule/exit combinations tested: {tested}. With this many, roughly one could look "
                "'significant' by chance alone.",
            "PROMISING: " + (", ".join(promising) if promising else "none")]
    return "\n".join(out)


# ----------------------------------------------------------------- launch ceiling
LAUNCH_EXITS = (2, 5, 10, 15, 30, 60)
LAUNCH_FEES = (("cheap fee 0.0001 SOL", 105_000), ("typical sniper 0.001 SOL", 1_005_000),
               ("competitive 0.005 SOL", 5_005_000))


def launch_ceiling(conn: sqlite3.Connection, position_aud: float, aud_per_usd: float) -> str:
    """Best-case 'first buyer' test: buy at the price right after the token's FIRST trade
    (normally the creator's own buy in the launch transaction - nobody else can be earlier)
    and sell N seconds after launch. Also a perfect-hindsight sale at the highest price in
    the first 30 s, which no real bot can achieve. Both are upper bounds: real snipers
    compete for the first slot and most of them land later than this."""
    toks = [t for t in load_tokens(conn) if t.bars]
    if len(toks) < 40:
        return "Not enough recorded launches for the launch-ceiling test."
    half = toks[len(toks) // 2].seen_at
    out = [
        "",
        "C. LAUNCH CEILING - 'first to buy, first to sell' (UPPER BOUND, not achievable in practice)",
        "   Entry: the price right after the very first trade (usually the creator's own buy).",
        f"   Position A${position_aud:g}. Sell at a fixed time after launch, or at the best price in 30 s (hindsight).",
    ]
    for fee_name, network in LAUNCH_FEES:
        costs = Costs(position_aud=position_aud, aud_per_usd=aud_per_usd, network_lamports=network)
        out.append(f"   -- network/priority fee per transaction: {fee_name}")
        for label, seconds in [(f"sell at +{s}s", s) for s in LAUNCH_EXITS] + [("hindsight best in 30s", None)]:
            halves: list[list[float]] = [[], []]
            for t in toks:
                first = t.bars[0]
                p_in = first["open_price_sol"] or first["close_price_sol"]
                if not p_in:
                    continue
                if seconds is None:
                    window = [b for b in t.bars if b["bar_end"] <= t.seen_at + 30] or [first]
                    p_out = max(b["high_price_sol"] or 0 for b in window)
                else:
                    p_out, _ = price_at(t, t.seen_at + seconds)
                    p_out = p_out or p_in
                r = costs.net_return(p_out / p_in - 1, t.fee_bps, first["sol_usd"])
                halves[t.seen_at >= half].append(r)
            d, v = _stats(halves[0]), _stats(halves[1])
            out.append(f"      {label:<22} disc {_fmt(d)}")
            out.append(f"      {'':<22} val  {_fmt(v)}")
    out.append("   Bars are 5 s, so '+2s' uses the last price known by then; the first bar's open is the price "
               "after its first trade.")
    return "\n".join(out)


# ------------------------------------------------------------- slot-by-slot entry
SLOT_OFFSETS = (0, 1, 2, 3, 5, 10, 25)
SLOT_HOLDS = (5, 12, 25, 50)  # ~2, 5, 10, 20 seconds at ~0.4 s per slot


def _typical_sol_usd(conn: sqlite3.Connection) -> float:
    vals = sorted(r[0] for r in conn.execute("SELECT sol_usd FROM pump_bars WHERE sol_usd > 0 LIMIT 20000"))
    return vals[len(vals) // 2] if vals else 150.0


def slot_ceiling(conn: sqlite3.Connection, aud_per_usd: float, sizes=(5.0, 50.0),
                 fees=(("typical sniper 0.001 SOL", 1_005_000), ("competitive 0.005 SOL", 5_005_000))) -> str:
    """Net % for landing our buy k slots after the launch slot and selling h slots later,
    using every recorded trade and exact bonding-curve maths (our order's price impact
    included, and our buy kept in the curve when we sell).

    Entry is OPTIMISTIC: k=0 means directly after the creator's own launch transaction;
    k>=1 means first in slot launch+k, ahead of everyone else in that slot. Exit is
    pessimistic: after every trade in the exit slot."""
    toks = conn.execute(
        "SELECT mint, slot, signature, seen_at FROM pump_tokens WHERE mayhem = 0 AND slot > 0 AND "
        "(quote_mint IS NULL OR quote_mint = ?) ORDER BY seen_at", (NATIVE_SOL_QUOTE,)).fetchall()
    trades: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM pump_early_trades ORDER BY mint, slot, id"):
        trades.setdefault(r["mint"], []).append(r)
    toks = [t for t in toks if t["mint"] in trades]
    if len(toks) < 40:
        return ("\nD. SLOT-BY-SLOT ENTRY: not enough data yet "
                f"({len(toks)} launches with recorded early trades; needs the updated sniper to run for a while).")
    sol_usd = _typical_sol_usd(conn)
    half = toks[len(toks) // 2]["seen_at"]
    max_slots = max(SLOT_OFFSETS) + max(SLOT_HOLDS)
    out = ["", "D. SLOT-BY-SLOT ENTRY - how late can you be? (optimistic entry, exact curve maths, net %)",
           f"   {len(toks)} launches with every trade of their first ~30 s recorded; SOL/USD ~{sol_usd:.0f}.",
           "   Each cell: discovery / validation mean net %, '*' = both halves > 0 and validation 95% CI above 0."]
    for size in sizes:
        budget = int(size / aud_per_usd / sol_usd * 1e9)
        for fee_name, network in fees:
            out.append(f"   -- A${size:g} per trade, network/priority fee {fee_name} per transaction")
            out.append("      landed at     " + "".join(f"sell +{h:>2} slots   " for h in SLOT_HOLDS))
            for k in SLOT_OFFSETS:
                cells = []
                for h in SLOT_HOLDS:
                    halves: list[list[float]] = [[], []]
                    for t in toks:
                        tr = trades[t["mint"]]
                        if tr[-1]["slot"] - t["slot"] < 0 or k + h > max_slots:
                            continue
                        r = _slot_trade(tr, t["slot"], t["signature"], k, h, budget, network)
                        if r is not None:
                            halves[t["seen_at"] >= half].append(r)
                    d, v = _stats(halves[0]), _stats(halves[1])
                    if not d["n"] or not v["n"]:
                        cells.append(f"{'n/a':<17}")
                        continue
                    star = "*" if d["mean"] > 0 and v["mean"] > 0 and v["lo"] > 0 else " "
                    cells.append(f"{d['mean']:+6.1f}/{v['mean']:+6.1f}{star}   ")
                label = "after creator" if k == 0 else f"launch+{k} slot"
                out.append(f"      {label:<14}" + "".join(cells))
    out.append("   Slots are ~0.4 s. Real snipers compete for these positions (and creators bundle their own "
               "buys), so actual fills land later than shown; treat each row as the best case for that delay.")
    return "\n".join(out)


def _slot_trade(tr: list, create_slot: int, create_sig: str | None, k: int, h: int, budget: int,
                network: int) -> float | None:
    first = tr[0]
    if first["is_buy"]:
        pre = (first["virtual_quote"] - first["quote_amount"], first["virtual_token"] + first["token_amount"])
    else:
        pre = (first["virtual_quote"] + first["quote_amount"], first["virtual_token"] - first["token_amount"])
    if k == 0:
        before = [r for r in tr if r["signature"] == create_sig]
    else:
        before = [r for r in tr if r["slot"] < create_slot + k]
    vq, vt = (before[-1]["virtual_quote"], before[-1]["virtual_token"]) if before else pre
    fee = max((r["fee_bps"] for r in tr if r["fee_bps"]), default=125)
    try:
        buy = simulate_curve_buy(vq, vt, budget, fee, network)
    except ValueError:
        return -100.0
    if buy.tokens_raw <= 0:
        return None
    after = [r for r in tr if r["slot"] <= create_slot + k + h]
    xq, xt = (after[-1]["virtual_quote"], after[-1]["virtual_token"]) if after else (vq, vt)
    xt -= curve_buy_tokens(xq, xt, buy.curve_in)
    xq += buy.curve_in
    sell = simulate_curve_sell(xq, xt, buy.tokens_raw, fee, network)
    return (sell.proceeds / budget - 1) * 100


# ----------------------------------------------------------------- copy-trading
MIN_TOKENS_FOR_RECORD = 5
TOP_WALLETS = 20
COPY_DELAYS = (1, 2, 5, 10)  # slots after the copied wallet's trade
WALLET_FEE = 0.0125  # ~ protocol + creator fee per side, as observed on chain


def _end_reserves(tok: Token | None, fallback: tuple[int, int]) -> tuple[int, int]:
    """Reserves at the end of the recording window (bars), else the last early trade."""
    if tok and tok.bars:
        last = tok.bars[-1]
        if last["virtual_quote"] and last["virtual_token"]:
            return last["virtual_quote"], last["virtual_token"]
    return fallback


def _wallet_token_results(trades: list, end: tuple[int, int]) -> dict[str, dict]:
    """Per wallet (creator excluded) for one token: SOL spent, SOL received, and the
    value of tokens still held, sold into the end-of-window curve."""
    per: dict[str, dict] = {}
    for r in trades:
        if r["by_creator"] or not r["user"]:
            continue
        w = per.setdefault(r["user"], {"in": 0, "out": 0, "tokens": 0, "first_slot": r["slot"], "sell_slot": None})
        if r["is_buy"]:
            w["in"] += r["quote_amount"]
            w["tokens"] += r["token_amount"]
        else:
            w["out"] += r["quote_amount"]
            w["tokens"] -= r["token_amount"]
            if w["sell_slot"] is None:
                w["sell_slot"] = r["slot"]
    for w in per.values():
        held = max(w["tokens"], 0)
        w["value"] = w["out"] + (curve_sell_lamports(end[0], end[1], held) if held else 0)
        cost = w["in"] * (1 + WALLET_FEE)
        w["pnl"] = w["value"] * (1 - WALLET_FEE) - cost
        w["roi"] = w["pnl"] / cost * 100 if cost else 0.0
    return {k: v for k, v in per.items() if v["in"] > 0}


def wallet_research(conn: sqlite3.Connection, aud_per_usd: float, sizes=(5.0, 50.0),
                    network: int = 1_005_000) -> str:
    """Do early wallets' track records persist, and could copying them be profitable?"""
    toks = conn.execute(
        "SELECT mint, slot, seen_at FROM pump_tokens WHERE mayhem = 0 AND slot > 0 AND "
        "(quote_mint IS NULL OR quote_mint = ?) ORDER BY seen_at", (NATIVE_SOL_QUOTE,)).fetchall()
    trades: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM pump_early_trades WHERE user IS NOT NULL ORDER BY mint, slot, id"):
        trades.setdefault(r["mint"], []).append(r)
    toks = [t for t in toks if t["mint"] in trades]
    if len(toks) < 200:
        return ("\nE. COPY-TRADING: not enough data yet "
                f"({len(toks)} launches with wallet-level trades; needs a few days of recording).")
    bars = {t.mint: t for t in load_tokens(conn)}
    half = toks[len(toks) // 2]["seen_at"]
    stats: list[dict[str, dict]] = [{}, {}]  # per period: wallet -> aggregate
    results: dict[str, dict[str, dict]] = {}
    for t in toks:
        tr = trades[t["mint"]]
        res = _wallet_token_results(tr, _end_reserves(bars.get(t["mint"]),
                                                      (tr[-1]["virtual_quote"], tr[-1]["virtual_token"])))
        results[t["mint"]] = res
        period = int(t["seen_at"] >= half)
        for w, r in res.items():
            a = stats[period].setdefault(w, {"tokens": 0, "pnl": 0, "wins": 0, "rois": []})
            a["tokens"] += 1
            a["pnl"] += r["pnl"]
            a["wins"] += r["pnl"] > 0
            a["rois"].append(r["roi"])
    disc, val = stats
    eligible = {w: a for w, a in disc.items() if a["tokens"] >= MIN_TOKENS_FOR_RECORD}
    top = sorted(eligible, key=lambda w: eligible[w]["pnl"], reverse=True)[:TOP_WALLETS]
    out = ["", "E. COPY-TRADING - do early wallets' records persist, and can they be copied?",
           f"   {len(toks)} launches; discovery wallets with >= {MIN_TOKENS_FOR_RECORD} tokens: {len(eligible)}. "
           f"Creator wallets excluded. Wallet P&L: first 2 min of trades, remaining tokens sold into the "
           f"curve at the end of the 10-min window, ~{WALLET_FEE:.2%} fee each way."]
    if not top:
        return "\n".join(out + ["   No wallets with enough discovery history."])

    def summary(ws, period):
        rois = [x for w in ws for x in period.get(w, {}).get("rois", [])]
        return _fmt(_stats(rois))

    all_val = [w for w in val if val[w]["tokens"] >= 1]
    out += ["   Per-token ROI of the wallet's own trades:",
            f"      top {len(top)} wallets, discovery (how they were picked): {summary(top, disc)}",
            f"      same wallets, validation (does it persist?):          {summary(top, val)}",
            f"      every wallet, validation (baseline):                  {summary(all_val, val)}",
            "   Top wallets (discovery): tokens / win rate / P&L SOL  ->  validation tokens / P&L SOL"]
    for w in top[:10]:
        d, v = disc[w], val.get(w, {"tokens": 0, "pnl": 0})
        out.append(f"      {w[:6]}..{w[-4:]}  {d['tokens']:>4} / {d['wins'] / d['tokens']:4.0%} / "
                   f"{d['pnl'] / 1e9:+8.2f}  ->  {v['tokens']:>4} / {v['pnl'] / 1e9:+8.2f}")
    busiest = max(eligible.values(), key=lambda a: a["tokens"])["tokens"]
    out.append(f"   (Most active discovery wallet traded {busiest} tokens - very active wallets are usually bots.)")

    # Copy simulation on the validation half: follow the first top wallet to buy each token.
    sol_usd = _typical_sol_usd(conn)
    top_set = set(top)
    out.append("   Copying top wallets in validation: buy d slots after their first buy, sell d slots after "
               "their first sell (else at window end). Net % per trade:")
    for size in sizes:
        budget = int(size / aud_per_usd / sol_usd * 1e9)
        row = []
        for d in COPY_DELAYS:
            rets = []
            for t in toks:
                if t["seen_at"] < half:
                    continue
                tr = trades[t["mint"]]
                leaders = [(r["first_slot"], w) for w, r in results[t["mint"]].items() if w in top_set]
                if not leaders:
                    continue
                first_slot, w = min(leaders)
                r = _copy_trade(tr, bars.get(t["mint"]), first_slot, results[t["mint"]][w]["sell_slot"], d,
                                budget, network)
                if r is not None:
                    rets.append(r)
            st = _stats(rets)
            row.append(f"+{d} slot: " + (f"n={st['n']} mean {st['mean']:+.1f}% win {st['pos']:.0%}"
                                         + (f" CI[{st['lo']:+.1f},{st['hi']:+.1f}]" if st["n"] > 1 else "")
                                         if st["n"] else "n=0"))
        out.append(f"      A${size:g}: " + " | ".join(row))
    out.append(f"   Fee {network} lamports per transaction. Wallet addresses are public on-chain data; this "
               "does not identify people, and a wallet that looked skilled can stop, change behaviour, or "
               "exploit known copiers.")
    return "\n".join(out)


def _copy_trade(tr: list, tok: Token | None, lead_slot: int, lead_sell_slot: int | None, d: int, budget: int,
                network: int) -> float | None:
    before = [r for r in tr if r["slot"] < lead_slot + d]
    if not before:
        return None
    vq, vt = before[-1]["virtual_quote"], before[-1]["virtual_token"]
    fee = max((r["fee_bps"] for r in tr if r["fee_bps"]), default=125)
    try:
        buy = simulate_curve_buy(vq, vt, budget, fee, network)
    except ValueError:
        return -100.0
    if buy.tokens_raw <= 0:
        return None
    if lead_sell_slot is not None:
        after = [r for r in tr if r["slot"] <= lead_sell_slot + d]
        xq, xt = after[-1]["virtual_quote"], after[-1]["virtual_token"]
    else:
        xq, xt = _end_reserves(tok, (tr[-1]["virtual_quote"], tr[-1]["virtual_token"]))
    xt -= curve_buy_tokens(xq, xt, buy.curve_in)
    xq += buy.curve_in
    sell = simulate_curve_sell(xq, xt, buy.tokens_raw, fee, network)
    return (sell.proceeds / budget - 1) * 100
