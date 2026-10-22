"""Streamlit dashboard (read-only). PAPER TRADING ONLY.

Run:  streamlit run dashboard/app.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperbot import BANNER  # noqa: E402
from paperbot.config import ConfigError, load_settings  # noqa: E402
from paperbot.metrics import MIN_TRADES_FOR_EVALUATION, compute  # noqa: E402

st.set_page_config(page_title="PAPER TRADING ONLY - Solana meme-coin simulator", layout="wide")

st.markdown(
    f"""<div style="background:#b00020;color:#fff;padding:14px 18px;border-radius:6px;
    font-size:1.35rem;font-weight:700;text-align:center;letter-spacing:.04em">
    PAPER TRADING ONLY &mdash; {BANNER.split(' - ', 1)[1]}</div>""",
    unsafe_allow_html=True,
)
st.caption("Simulated results with placeholder cost assumptions. Nothing here is evidence of real-world "
           "profitability. A token passing filters is NOT evidence it is safe, legitimate or sellable.")

try:
    settings = load_settings(os.environ.get("PAPERBOT_ENV_FILE", ".env"))
except ConfigError as exc:
    st.error(f"Configuration error: {exc}")
    st.stop()

db_path = st.sidebar.text_input("SQLite database", settings.db_path)
if not Path(db_path).exists():
    st.warning(f"Database {db_path} not found. Run `python -m paperbot init-db` and `python -m paperbot scan`.")
    st.stop()


def ro_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


conn = ro_connect(db_path)
try:
    runs = pd.read_sql_query("SELECT run_id, kind, created_at, rules_fingerprint FROM runs ORDER BY created_at", conn)
except Exception:
    st.warning("Database has no schema yet. Run `python -m paperbot init-db`.")
    st.stop()
if runs.empty:
    st.info("No runs yet. Start the scanner with `python -m paperbot scan`.")
    st.stop()

run_ids = runs["run_id"].tolist()
# Newest run first (fresh accounts are usually what you want to watch).
run_ids = run_ids[::-1]
run_id = st.sidebar.selectbox("Run", run_ids, index=0)
if st.sidebar.button("Refresh"):
    st.rerun()
st.sidebar.caption(f"Rules fingerprint: `{runs.set_index('run_id').loc[run_id, 'rules_fingerprint']}`")


def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn, params=params)


def ts(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    for c in cols:
        if c in df:
            df[c] = pd.to_datetime(df[c], unit="s", utc=True)
    return df


acct = conn.execute("SELECT * FROM account WHERE run_id=?", (run_id,)).fetchone()
open_pos = ts(q("SELECT * FROM positions WHERE run_id=? AND status='open' ORDER BY opened_at", (run_id,)),
              "opened_at", "last_mark_at", "signal_at")
m = compute(conn, run_id)
positions_value = float(open_pos["last_nlv_aud"].fillna(0).sum()) if not open_pos.empty else 0.0
unrealised = float((open_pos["last_nlv_aud"].fillna(0) - open_pos["cost_aud"]).sum()) if not open_pos.empty else 0.0
equity = acct["cash_aud"] + positions_value

# ---- headline tiles --------------------------------------------------------
c = st.columns(4)
c[0].metric("Virtual cash (A$)", f"{acct['cash_aud']:.4f}")
c[1].metric("Total equity (A$)", f"{equity:.4f}", f"{equity - acct['starting_cash_aud']:+.4f} vs start")
c[2].metric("Realised P&L (A$)", f"{m.net_pnl_aud:+.4f}")
c[3].metric("Unrealised P&L (A$)", f"{unrealised:+.4f}")
c = st.columns(4)
c[0].metric("Closed trades", m.trades)
c[1].metric("Win rate", "n/a" if m.win_rate is None else f"{m.win_rate:.1%}")
c[2].metric("Max drawdown", f"A${m.max_drawdown_aud:.4f}", f"{m.max_drawdown_pct:.2f}%", delta_color="off")
c[3].metric("Trading costs (A$)", f"{m.fees_aud + m.slippage_aud:.4f}",
            f"fees {m.fees_aud:.4f} / slippage {m.slippage_aud:.4f}", delta_color="off")
c = st.columns(4)
c[0].metric("Net expectancy / trade", "n/a" if m.expectancy_aud is None else f"A${m.expectancy_aud:+.4f}")
c[1].metric("Profit factor", "n/a" if m.profit_factor is None else f"{m.profit_factor:.3f}")
c[2].metric("Avg win / avg loss (A$)",
            f"{'n/a' if m.avg_win_aud is None else f'{m.avg_win_aud:.2f}'} / "
            f"{'n/a' if m.avg_loss_aud is None else f'{m.avg_loss_aud:.2f}'}")
c[3].metric("Failed exits", m.failed_exit_attempts, f"{m.stale_writeoffs} stale write-offs", delta_color="off")

if not m.sufficient_sample:
    st.warning(f"Insufficient sample: {m.trades} closed trades (< {MIN_TRADES_FOR_EVALUATION}). "
               "Do not draw conclusions about the strategy yet.")

# ---- equity & drawdown (two charts, one axis each) -------------------------
eq = ts(q("SELECT at, equity_aud FROM equity_snapshots WHERE run_id=? ORDER BY at", (run_id,)), "at")
if not eq.empty:
    eq = eq.set_index("at")
    eq["drawdown_aud"] = eq["equity_aud"] - eq["equity_aud"].cummax()
    left, right = st.columns(2)
    left.subheader("Total equity (A$)")
    left.line_chart(eq["equity_aud"], height=240)
    right.subheader("Drawdown from peak (A$)")
    right.area_chart(eq["drawdown_aud"], height=240)

# ---- tables -----------------------------------------------------------------
st.subheader(f"Open positions ({len(open_pos)})")
if open_pos.empty:
    st.write("None.")
else:
    open_pos["unrealised_aud"] = open_pos["last_nlv_aud"] - open_pos["cost_aud"]
    st.dataframe(open_pos[["symbol", "token_address", "opened_at", "cost_aud", "qty", "entry_ref_price_usd",
                           "entry_exec_price_usd", "last_ref_price_usd", "last_nlv_aud", "unrealised_aud",
                           "last_mark_at", "pending_exit_reason", "failed_exit_attempts"]],
                 use_container_width=True, hide_index=True)

closed = ts(q("SELECT * FROM positions WHERE run_id=? AND status='closed' ORDER BY closed_at DESC", (run_id,)),
            "opened_at", "closed_at")
st.subheader(f"Closed trades ({len(closed)})")
if not closed.empty:
    closed["fees_aud"] = closed["entry_fee_aud"].fillna(0) + closed["exit_fee_aud"].fillna(0)
    closed["slippage_aud"] = closed["entry_slippage_aud"].fillna(0) + closed["exit_slippage_aud"].fillna(0)
    st.dataframe(closed[["symbol", "opened_at", "closed_at", "exit_reason", "cost_aud", "proceeds_aud", "pnl_aud",
                         "pnl_pct", "fees_aud", "slippage_aud", "entry_ref_price_usd", "exit_ref_price_usd",
                         "entry_aud_per_usd", "exit_aud_per_usd", "failed_exit_attempts", "hold_seconds"]],
                 use_container_width=True, hide_index=True)

left, right = st.columns(2)
with left:
    st.subheader("Exit reasons")
    st.dataframe(pd.DataFrame(sorted(m.exit_reasons.items()), columns=["reason", "count"]), hide_index=True)
    st.subheader("Abandoned entries")
    st.dataframe(pd.DataFrame(sorted(m.abandon_reasons.items()), columns=["reason", "count"]), hide_index=True)
with right:
    st.subheader("Filter rejections (last 24h)")
    fd = q("SELECT reasons_json FROM filter_decisions WHERE run_id=? AND passed=0 AND decided_at >= "
           "(SELECT MAX(decided_at) FROM filter_decisions WHERE run_id=?) - 86400", (run_id, run_id))
    counts: dict[str, int] = {}
    for r in fd["reasons_json"]:
        for code in json.loads(r)["reasons"]:
            k = code.split(":")[0]
            counts[k] = counts.get(k, 0) + 1
    st.dataframe(pd.DataFrame(sorted(counts.items(), key=lambda kv: -kv[1]), columns=["reason", "count"]),
                 hide_index=True)

st.subheader("Recent order events")
st.dataframe(ts(q("SELECT at, side, event, reason, token_address, ref_price_usd, position_id FROM order_events "
                  "WHERE run_id=? ORDER BY id DESC LIMIT 100", (run_id,)), "at"),
             use_container_width=True, hide_index=True)

with st.expander("Data ingestion"):
    stats = conn.execute("SELECT COUNT(*) AS n, SUM(status='missing') AS missing, MIN(observed_at) AS lo, "
                         "MAX(observed_at) AS hi FROM observations").fetchone()
    ntok = conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0]
    st.write(f"{ntok} tokens tracked; {stats['n']} observations ({stats['missing'] or 0} 'missing' - "
             "token/pair no longer returned by the API).")
    if stats["lo"]:
        st.write(f"Recorded period: {pd.to_datetime(stats['lo'], unit='s', utc=True)} -> "
                 f"{pd.to_datetime(stats['hi'], unit='s', utc=True)}")
    st.dataframe(ts(q("SELECT observed_at, symbol, token_address, status, price_usd, liquidity_usd, buys_m5, "
                      "sells_m5, volume_m5_usd, pair_created_at, aud_per_usd FROM observations "
                      "ORDER BY id DESC LIMIT 200"), "observed_at", "pair_created_at"),
                 use_container_width=True, hide_index=True)

is_sniper = conn.execute("SELECT COUNT(*) FROM pump_tokens").fetchone()[0] > 0 if conn.execute(
    "SELECT 1 FROM sqlite_master WHERE name='pump_tokens'").fetchone() else False
if is_sniper:
    with st.expander("pump.fun sniper data"):
        st.write(pd.DataFrame(conn.execute("SELECT status, COUNT(*) FROM pump_tokens GROUP BY 1").fetchall(),
                              columns=["status", "tokens"]))
        st.dataframe(ts(q("SELECT seen_at, symbol, mint, status, rejection, creator_fee_bps, mayhem "
                          "FROM pump_tokens ORDER BY seen_at DESC LIMIT 200"), "seen_at"),
                     use_container_width=True, hide_index=True)

with st.expander("Labelled assumptions (costs, latency, fills, FX)", expanded=True):
    assumptions = settings.assumptions()
    if run_id != "live":
        try:
            from paperbot.sniper.config import SniperSettings

            sn = SniperSettings.from_env()
            if run_id == sn.run_id:
                assumptions = {**sn.assumptions(), "FX": settings.assumptions()["FX"]}
        except ConfigError:
            pass
    for k, v in assumptions.items():
        st.markdown(f"- **{k}:** {v}")
    st.caption("These are placeholders to be stress-tested (see `python -m paperbot scenarios`), not measurements.")
