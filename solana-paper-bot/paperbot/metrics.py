"""Performance statistics computed only from recorded simulated trades."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

MIN_TRADES_FOR_EVALUATION = 200


@dataclass
class Metrics:
    run_id: str
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    net_pnl_aud: float
    expectancy_aud: float | None  # mean net P&L per closed trade
    expectancy_pct: float | None  # mean net return per trade, % of position cost
    avg_win_aud: float | None
    avg_loss_aud: float | None
    profit_factor: float | None  # gross wins / |gross losses|
    max_drawdown_aud: float
    max_drawdown_pct: float
    fees_aud: float
    slippage_aud: float  # attribution; already inside net P&L
    failed_exit_attempts: int
    stale_writeoffs: int
    abandoned_entries: int
    exit_reasons: dict[str, int]
    abandon_reasons: dict[str, int]
    sufficient_sample: bool

    def as_dict(self) -> dict:
        return asdict(self)


def compute(conn: sqlite3.Connection, run_id: str) -> Metrics:
    start = conn.execute("SELECT starting_cash_aud FROM account WHERE run_id=?", (run_id,)).fetchone()
    starting = start["starting_cash_aud"] if start else 0.0
    closed = conn.execute(
        "SELECT * FROM positions WHERE run_id=? AND status='closed' ORDER BY closed_at, id", (run_id,)).fetchall()
    pnls = [r["pnl_aud"] for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)

    fees = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(entry_fee_aud,0) + COALESCE(exit_fee_aud,0)),0) AS f, "
        "COALESCE(SUM(COALESCE(entry_slippage_aud,0) + COALESCE(exit_slippage_aud,0)),0) AS s, "
        "COALESCE(SUM(failed_exit_attempts),0) AS fe FROM positions WHERE run_id=? AND status IN ('open','closed')",
        (run_id,)).fetchone()

    def counts(sql: str) -> dict[str, int]:
        return {r[0] or "?": r[1] for r in conn.execute(sql, (run_id,)).fetchall()}

    exit_reasons = counts("SELECT exit_reason, COUNT(*) FROM positions WHERE run_id=? AND status='closed' GROUP BY 1")
    abandon_reasons = counts(
        "SELECT abandon_reason, COUNT(*) FROM positions WHERE run_id=? AND status='abandoned' GROUP BY 1")

    dd_aud, dd_pct = max_drawdown(equity_curve(conn, run_id, starting))
    n = len(pnls)
    return Metrics(
        run_id=run_id, trades=n, wins=len(wins), losses=len(losses),
        win_rate=len(wins) / n if n else None,
        net_pnl_aud=sum(pnls),
        expectancy_aud=sum(pnls) / n if n else None,
        expectancy_pct=sum(r["pnl_pct"] for r in closed) / n if n else None,
        avg_win_aud=gross_win / len(wins) if wins else None,
        avg_loss_aud=-gross_loss / len(losses) if losses else None,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        max_drawdown_aud=dd_aud, max_drawdown_pct=dd_pct,
        fees_aud=fees["f"], slippage_aud=fees["s"], failed_exit_attempts=fees["fe"],
        stale_writeoffs=exit_reasons.get("stale_writeoff", 0),
        abandoned_entries=sum(abandon_reasons.values()),
        exit_reasons=exit_reasons, abandon_reasons=abandon_reasons,
        sufficient_sample=n >= MIN_TRADES_FOR_EVALUATION,
    )


def equity_curve(conn: sqlite3.Connection, run_id: str, starting: float) -> list[float]:
    """Mark-to-market snapshots if recorded, else the realised equity path."""
    snaps = [r[0] for r in conn.execute(
        "SELECT equity_aud FROM equity_snapshots WHERE run_id=? ORDER BY at, id", (run_id,)).fetchall()]
    if snaps:
        return [starting] + snaps
    curve, eq = [starting], starting
    for r in conn.execute("SELECT pnl_aud FROM positions WHERE run_id=? AND status='closed' ORDER BY closed_at, id",
                          (run_id,)):
        eq += r[0]
        curve.append(eq)
    return curve


def max_drawdown(curve: list[float]) -> tuple[float, float]:
    peak, dd_aud, dd_pct = float("-inf"), 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            dd_aud = max(dd_aud, peak - v)
            dd_pct = max(dd_pct, (peak - v) / peak * 100.0)
    return dd_aud, dd_pct


def format_report(m: Metrics, title: str = "") -> str:
    def f(v, fmt="{:.4f}"):
        return "n/a" if v is None else fmt.format(v)

    lines = [
        "=" * 64,
        "PAPER TRADING ONLY - simulated results, not real money",
        f"{title} run={m.run_id}".strip(),
        "=" * 64,
        f"Closed trades          {m.trades}  (wins {m.wins} / losses {m.losses})",
    ]
    if not m.sufficient_sample:
        lines.append(f"!! INSUFFICIENT SAMPLE: {m.trades} < {MIN_TRADES_FOR_EVALUATION} trades - do not draw conclusions")
    lines += [
        f"Win rate               {f(m.win_rate, '{:.1%}')}",
        f"Net P&L (A$)           {m.net_pnl_aud:.4f}",
        f"Net expectancy/trade   A${f(m.expectancy_aud)}  ({f(m.expectancy_pct, '{:.2f}')}% of position)",
        f"Average win / loss     A${f(m.avg_win_aud)} / A${f(m.avg_loss_aud)}",
        f"Profit factor          {f(m.profit_factor, '{:.3f}')}",
        f"Max drawdown           A${m.max_drawdown_aud:.4f} ({m.max_drawdown_pct:.2f}%)",
        f"Fees paid (A$)         {m.fees_aud:.4f}",
        f"Slippage cost (A$)     {m.slippage_aud:.4f}  (already included in net P&L)",
        f"Failed exit attempts   {m.failed_exit_attempts}   stale write-offs {m.stale_writeoffs}",
        f"Abandoned entries      {m.abandoned_entries}  {m.abandon_reasons}",
        f"Exit reasons           {m.exit_reasons}",
    ]
    return "\n".join(lines)


def bootstrap_mean_ci(values: list[float], n_resamples: int = 5000, seed: int = 7,
                      level: float = 0.95) -> tuple[float, float, float]:
    """(low, high, share of resamples with mean > 0) for the mean of ``values``.
    Resamples trades with replacement; deterministic for a given seed."""
    import random

    if len(values) < 2:
        return float("nan"), float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(n_resamples))
    lo = means[int((1 - level) / 2 * n_resamples)]
    hi = means[int((1 + level) / 2 * n_resamples) - 1]
    return lo, hi, sum(m > 0 for m in means) / n_resamples


def compare_runs(conn: sqlite3.Connection, run_ids: list[str] | None = None) -> list[dict]:
    """Side-by-side evaluation rows. Returns are per trade as % of the A$ cost, so
    runs with different account or position sizes are comparable."""
    if run_ids is None:
        run_ids = [r[0] for r in conn.execute("SELECT run_id FROM runs WHERE kind='live' ORDER BY created_at")]
    rows = []
    for run in run_ids:
        m = compute(conn, run)
        trades = conn.execute("SELECT pnl_pct, closed_at, opened_at, cost_aud FROM positions WHERE run_id=? AND "
                              "status='closed' ORDER BY closed_at, id", (run,)).fetchall()
        pcts = [t[0] for t in trades]
        lo, hi, p_pos = bootstrap_mean_ci(pcts)
        half = len(pcts) // 2
        days = (trades[-1][1] - trades[0][2]) / 86400 if trades else 0.0
        costs = m.fees_aud + m.slippage_aud
        rows.append({
            "run": run, "trades": m.trades, "days": days, "trades_per_day": m.trades / days if days else None,
            "win_rate": m.win_rate, "expectancy_pct": m.expectancy_pct, "ci95_low": lo, "ci95_high": hi,
            "p_mean_positive": p_pos, "first_half_pct": sum(pcts[:half]) / half if half else None,
            "second_half_pct": sum(pcts[half:]) / (len(pcts) - half) if len(pcts) - half else None,
            "expectancy_aud": m.expectancy_aud, "net_pnl_aud": m.net_pnl_aud, "profit_factor": m.profit_factor,
            "avg_win_aud": m.avg_win_aud, "avg_loss_aud": m.avg_loss_aud, "max_dd_pct": m.max_drawdown_pct,
            "fees_aud": m.fees_aud, "slippage_aud": m.slippage_aud,
            "costs_pct_of_traded": costs / sum(t[3] for t in trades) * 100 if trades else None,
            "failed_exits": m.failed_exit_attempts, "stale_writeoffs": m.stale_writeoffs,
            "exit_reasons": m.exit_reasons, "abandoned": m.abandon_reasons,
        })
    return rows


def format_comparison(rows: list[dict]) -> str:
    def f(v, fmt):
        return "n/a" if v is None or v != v else fmt.format(v)

    out = ["PAPER TRADING ONLY - simulated results. Per-trade returns are % of the A$ position cost.", ""]
    for r in rows:
        verdict = ("CI excludes 0 (positive)" if r["ci95_low"] == r["ci95_low"] and r["ci95_low"] > 0 else
                   "CI excludes 0 (negative)" if r["ci95_high"] == r["ci95_high"] and r["ci95_high"] < 0 else
                   "CI includes 0 - not distinguishable from zero")
        out += [
            f"== {r['run']}: {r['trades']} closed trades over {r['days']:.1f} days "
            f"({f(r['trades_per_day'], '{:.1f}')}/day)",
            f"   win rate {f(r['win_rate'], '{:.1%}')} | profit factor {f(r['profit_factor'], '{:.2f}')} | "
            f"net P&L A${r['net_pnl_aud']:.2f} | max drawdown {r['max_dd_pct']:.1f}%",
            f"   net expectancy {f(r['expectancy_pct'], '{:+.2f}')}% per trade "
            f"(A${f(r['expectancy_aud'], '{:+.4f}')}); 95% CI {f(r['ci95_low'], '{:+.2f}')}% .. "
            f"{f(r['ci95_high'], '{:+.2f}')}%  -> {verdict}",
            f"   share of bootstrap resamples with positive mean: {f(r['p_mean_positive'], '{:.0%}')}",
            f"   stability: first half {f(r['first_half_pct'], '{:+.2f}')}% vs second half "
            f"{f(r['second_half_pct'], '{:+.2f}')}% per trade",
            f"   avg win A${f(r['avg_win_aud'], '{:.4f}')} / avg loss A${f(r['avg_loss_aud'], '{:.4f}')} | "
            f"fees A${r['fees_aud']:.2f} + slippage A${r['slippage_aud']:.2f} "
            f"= {f(r['costs_pct_of_traded'], '{:.2f}')}% of amount traded",
            f"   failed exit attempts {r['failed_exits']} | stale write-offs {r['stale_writeoffs']}",
            f"   exits {r['exit_reasons']}",
            f"   abandoned entries {r['abandoned']}",
            "",
        ]
    out.append("Caveats: in-sample only (rules were not frozen before this data); runs overlap in time and "
               "see the same market, so they are not independent; costs, latency and fill rates are labelled "
               "assumptions. Do not risk money on this - next step is freeze + out-of-sample.")
    return "\n".join(out)
