"""Paper-trading simulation engine (AUD account, USD-priced tokens).

The engine is event-driven: it is fed immutable observations in time order and
a clock. The live scanner and historical replay drive the *same* engine, so the
rules, costs and fill logic are identical in both.

Order lifecycle
  signal (filters pass) -> pending -> filled at the first observation of the
  same pair at/after signal_at + latency  -> open
  exit trigger -> pending exit -> filled at first observation at/after
  trigger + latency (may fail and retry) -> closed
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from dataclasses import dataclass

from . import PAPER_TRADING_ONLY
from .costs import CostModel, simulate_buy, simulate_sell
from .db import transaction
from .filters import evaluate
from .fx import FxQuote
from .store import Observation, set_token_status

log = logging.getLogger(__name__)

assert PAPER_TRADING_ONLY is True, "this engine only simulates trades"


@dataclass
class AccountSummary:
    starting_cash_aud: float
    cash_aud: float
    positions_value_aud: float
    equity_aud: float
    realised_pnl_aud: float
    unrealised_pnl_aud: float
    open_positions: int
    closed_trades: int


class PaperEngine:
    def __init__(self, conn: sqlite3.Connection, run_id: str, settings, fx, rng: random.Random | None = None,
                 track_token_status: bool = True):
        self.conn = conn
        self.track_token_status = track_token_status  # replay runs must not touch the live token summary
        self.run_id = run_id
        self.s = settings
        self.fx = fx  # object with .get() -> FxQuote
        self.cost = CostModel.from_settings(settings)
        self.rng = rng or random.Random(settings.random_seed)

    # ------------------------------------------------------------------ utils
    def cash(self) -> float:
        return self.conn.execute("SELECT cash_aud FROM account WHERE run_id = ?", (self.run_id,)).fetchone()["cash_aud"]

    def _set_cash(self, value: float, now: float) -> None:
        if value < -1e-9:
            raise RuntimeError(f"virtual cash would go negative ({value})")
        self.conn.execute("UPDATE account SET cash_aud = ?, updated_at = ? WHERE run_id = ?",
                          (max(value, 0.0), now, self.run_id))

    def _event(self, side: str, event: str, token: str, at: float, *, position_id: int | None = None,
               reason: str | None = None, obs: Observation | None = None, details: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO order_events(run_id, position_id, token_address, side, event, reason, at, observation_id, "
            "ref_price_usd, details_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, position_id, token, side, event, reason, at, obs.id if obs else None,
             obs.price_usd if obs else None, json.dumps(details) if details else None),
        )

    def _positions(self, status_sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT * FROM positions WHERE run_id = ? AND {status_sql} ORDER BY id", (self.run_id, *params)
        ).fetchall()

    def active_positions(self) -> list[sqlite3.Row]:
        return self._positions("status IN ('pending','open')")

    def pending_pair_addresses(self) -> list[str]:
        rows = self._positions("(status = 'pending' OR (status = 'open' AND pending_exit_reason IS NOT NULL))")
        return [r["pair_address"] for r in rows]

    def open_pair_addresses(self) -> list[str]:
        return [r["pair_address"] for r in self.active_positions()]

    def _fx(self) -> FxQuote:
        return self.fx.get()

    # -------------------------------------------------------------- main hook
    def on_observation(self, obs: Observation, evaluate_entry: bool = True) -> None:
        """Process one new observation. ``obs.observed_at`` is the simulation clock."""
        with transaction(self.conn):
            handled = False
            for pos in self._positions("pair_address = ? AND status IN ('pending','open')", (obs.pair_address,)):
                handled = True
                if pos["status"] == "pending":
                    self._try_fill_entry(pos, obs)
                else:
                    self._manage_open(pos, obs)
            if obs.status == "missing" and not handled:
                for pos in self._positions("token_address = ? AND status IN ('pending','open')", (obs.token_address,)):
                    handled = True
                    if pos["status"] == "pending":
                        self._abandon(pos, obs.observed_at, "pair_missing_at_fill", obs)
                    else:
                        self._manage_open(pos, obs)
            if evaluate_entry and not handled:
                self._evaluate_entry(obs)

    def on_clock(self, now: float) -> None:
        """Time-based housekeeping that does not need a new observation (stale write-offs)."""
        with transaction(self.conn):
            for pos in self._positions("status = 'open'"):
                last = pos["last_mark_at"] or pos["opened_at"]
                if now - last >= self.s.stale_writeoff_minutes * 60.0:
                    self._write_off(pos, now)
            for pos in self._positions("status = 'pending'"):
                # A pending entry that never sees a fill-time observation is abandoned.
                if now - pos["signal_at"] > self.s.fill_timeout_seconds:
                    self._abandon(pos, now, "no_fill_observation", None)

    # ------------------------------------------------------------------ entry
    def _evaluate_entry(self, obs: Observation) -> None:
        if obs.status != "ok":
            return
        result = evaluate(obs, obs.observed_at, self.s)
        reasons = list(result.reasons)
        if result.passed:
            if self.conn.execute("SELECT 1 FROM positions WHERE run_id = ? AND token_address = ?",
                                 (self.run_id, obs.token_address)).fetchone():
                reasons.append("already_traded")
            elif len(self.active_positions()) >= self.s.max_open_positions:
                reasons.append("max_open_positions")
            elif self._budget() < self.s.min_position_aud:
                reasons.append("insufficient_cash")
        passed = not reasons
        self.conn.execute(
            "INSERT INTO filter_decisions(run_id, observation_id, token_address, decided_at, passed, reasons_json) "
            "VALUES (?,?,?,?,?,?)",
            (self.run_id, obs.id, obs.token_address, obs.observed_at, int(passed),
             json.dumps({"reasons": reasons, "metrics": result.metrics})),
        )
        self._token_status(obs.token_address, "signal" if passed else "rejected",
                           None if passed else ",".join(reasons))
        if not passed:
            return
        cur = self.conn.execute(
            "INSERT INTO positions(run_id, token_address, pair_address, symbol, status, signal_observation_id, "
            "signal_at, signal_price_usd) VALUES (?,?,?,?, 'pending', ?,?,?)",
            (self.run_id, obs.token_address, obs.pair_address, obs.symbol, obs.id, obs.observed_at, obs.price_usd),
        )
        self._event("buy", "signal", obs.token_address, obs.observed_at, position_id=cur.lastrowid, obs=obs,
                    details={"metrics": result.metrics})
        if self.s.latency_seconds == 0:
            pos = self.conn.execute("SELECT * FROM positions WHERE id = ?", (cur.lastrowid,)).fetchone()
            self._try_fill_entry(pos, obs)

    def _budget(self) -> float:
        return min(self.cash() * self.s.position_pct_of_cash, self.s.max_position_aud)

    def _try_fill_entry(self, pos: sqlite3.Row, obs: Observation) -> None:
        now = obs.observed_at
        if now < pos["signal_at"] + self.s.latency_seconds:
            return  # still inside simulated latency; cannot use this price
        if not obs.ok:
            return self._abandon(pos, now, "pair_missing_at_fill", obs)
        drift = (obs.price_usd / pos["signal_price_usd"] - 1.0) * 100.0
        if drift > self.s.max_entry_drift_pct:
            return self._abandon(pos, now, "entry_price_drift", obs, {"drift_pct": drift})
        if self.rng.random() < self.s.entry_fill_failure_prob:
            return self._abandon(pos, now, "simulated_fill_failure", obs)
        budget = self._budget()
        if budget < self.s.min_position_aud:
            return self._abandon(pos, now, "insufficient_cash", obs)
        q = self._fx()
        fill = simulate_buy(budget, obs.price_usd, q.aud_per_usd, self.cost, obs.liquidity_usd)
        self._set_cash(self.cash() - fill.cash_out_aud, now)
        nlv = simulate_sell(fill.qty, obs.price_usd, q.aud_per_usd, self.cost, obs.liquidity_usd).proceeds_aud
        self.conn.execute(
            "UPDATE positions SET status='open', opened_at=?, entry_observation_id=?, qty=?, entry_ref_price_usd=?, "
            "entry_exec_price_usd=?, entry_aud_per_usd=?, entry_fx_source=?, entry_notional_aud=?, entry_fee_aud=?, "
            "entry_slippage_aud=?, cost_aud=?, last_mark_at=?, last_ref_price_usd=?, last_liquidity_usd=?, "
            "last_nlv_aud=? WHERE id=?",
            (now, obs.id, fill.qty, fill.ref_price_usd, fill.exec_price_usd, fill.aud_per_usd, q.source,
             fill.notional_aud, fill.fee_aud, fill.slippage_aud, fill.cash_out_aud, now, obs.price_usd,
             obs.liquidity_usd, nlv, pos["id"]),
        )
        self._event("buy", "filled", pos["token_address"], now, position_id=pos["id"], obs=obs,
                    details={"cash_out_aud": fill.cash_out_aud, "fee_aud": fill.fee_aud,
                             "slippage_aud": fill.slippage_aud, "aud_per_usd": fill.aud_per_usd})
        self._token_status(pos["token_address"], "position_open", None)

    def _abandon(self, pos: sqlite3.Row, now: float, reason: str, obs: Observation | None,
                 details: dict | None = None) -> None:
        self.conn.execute("UPDATE positions SET status='abandoned', abandon_reason=? WHERE id=?", (reason, pos["id"]))
        self._event("buy", "abandoned", pos["token_address"], now, position_id=pos["id"], reason=reason, obs=obs,
                    details=details)

    # ------------------------------------------------------------------- exit
    def _manage_open(self, pos: sqlite3.Row, obs: Observation) -> None:
        now = obs.observed_at
        if not obs.ok:
            if pos["pending_exit_reason"] and now >= pos["pending_exit_at"] + self.s.latency_seconds:
                self._failed_exit(pos, now, "no_market_data", obs)
            elif not pos["pending_exit_reason"] and now - pos["opened_at"] >= self.s.max_hold_minutes * 60.0:
                self.conn.execute("UPDATE positions SET pending_exit_reason='max_hold', pending_exit_at=? WHERE id=?",
                                  (now, pos["id"]))
                self._failed_exit(pos, now, "no_market_data", obs)
            last = pos["last_mark_at"] or pos["opened_at"]
            if now - last >= self.s.stale_writeoff_minutes * 60.0:
                self._write_off(self._reload(pos), now)
            return

        q = self._fx()
        sell = simulate_sell(pos["qty"], obs.price_usd, q.aud_per_usd, self.cost, obs.liquidity_usd)
        self.conn.execute(
            "UPDATE positions SET last_mark_at=?, last_ref_price_usd=?, last_liquidity_usd=?, last_nlv_aud=? WHERE id=?",
            (now, obs.price_usd, obs.liquidity_usd, sell.proceeds_aud, pos["id"]),
        )
        pos = self._reload(pos)

        if pos["pending_exit_reason"]:
            if now >= pos["pending_exit_at"] + self.s.latency_seconds:
                self._try_fill_exit(pos, obs, q)
            return

        reason = self.exit_reason(pos["cost_aud"], sell.proceeds_aud, now - pos["opened_at"])
        if reason:
            self.conn.execute("UPDATE positions SET pending_exit_reason=?, pending_exit_at=? WHERE id=?",
                              (reason, now, pos["id"]))
            self._event("sell", "signal", pos["token_address"], now, position_id=pos["id"], reason=reason, obs=obs)
            if self.s.latency_seconds == 0:
                self._try_fill_exit(self._reload(pos), obs, q)

    def exit_reason(self, cost_aud: float, nlv_aud: float, held_seconds: float) -> str | None:
        """Exit rules are evaluated on executable net value (after exit slippage and fee)."""
        pnl_pct = (nlv_aud / cost_aud - 1.0) * 100.0
        if pnl_pct >= self.s.take_profit_pct:
            return "take_profit"
        if pnl_pct <= self.s.stop_loss_pct:
            return "stop_loss"
        if held_seconds >= self.s.max_hold_minutes * 60.0:
            return "max_hold"
        return None

    def _try_fill_exit(self, pos: sqlite3.Row, obs: Observation, q: FxQuote) -> None:
        now = obs.observed_at
        if self.rng.random() < self.s.exit_fill_failure_prob:
            return self._failed_exit(pos, now, "simulated_fill_failure", obs)
        sell = simulate_sell(pos["qty"], obs.price_usd, q.aud_per_usd, self.cost, obs.liquidity_usd)
        self._close(pos, now, pos["pending_exit_reason"], obs, sell.ref_price_usd, sell.exec_price_usd,
                    q.aud_per_usd, sell.gross_aud, sell.fee_aud, sell.slippage_aud, sell.proceeds_aud)

    def _failed_exit(self, pos: sqlite3.Row, now: float, reason: str, obs: Observation | None) -> None:
        self.conn.execute("UPDATE positions SET failed_exit_attempts = failed_exit_attempts + 1 WHERE id=?",
                          (pos["id"],))
        self._event("sell", "failed", pos["token_address"], now, position_id=pos["id"], reason=reason, obs=obs)

    def _write_off(self, pos: sqlite3.Row, now: float) -> None:
        proceeds = pos["cost_aud"] * self.s.stale_recovery_pct / 100.0
        self._event("sell", "written_off", pos["token_address"], now, position_id=pos["id"], reason="stale_no_data",
                    details={"recovery_pct": self.s.stale_recovery_pct})
        self._close(pos, now, "stale_writeoff", None, None, None, None, None, 0.0, 0.0, proceeds)

    def _close(self, pos, now, reason, obs, ref, exec_, fx, gross, fee, slip, proceeds) -> None:
        pnl = proceeds - pos["cost_aud"]
        self._set_cash(self.cash() + proceeds, now)
        self.conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, exit_reason=?, exit_observation_id=?, "
            "exit_ref_price_usd=?, exit_exec_price_usd=?, exit_aud_per_usd=?, exit_gross_aud=?, exit_fee_aud=?, "
            "exit_slippage_aud=?, proceeds_aud=?, pnl_aud=?, pnl_pct=?, hold_seconds=?, last_nlv_aud=?, "
            "pending_exit_reason=NULL, pending_exit_at=NULL WHERE id=?",
            (now, reason, obs.id if obs else None, ref, exec_, fx, gross, fee, slip, proceeds, pnl,
             pnl / pos["cost_aud"] * 100.0, now - pos["opened_at"], proceeds, pos["id"]),
        )
        if reason != "stale_writeoff":
            self._event("sell", "filled", pos["token_address"], now, position_id=pos["id"], reason=reason, obs=obs,
                        details={"proceeds_aud": proceeds, "fee_aud": fee, "slippage_aud": slip, "pnl_aud": pnl})
        self._token_status(pos["token_address"], "closed", reason)

    def _token_status(self, token: str, status: str, detail: str | None) -> None:
        if self.track_token_status:
            set_token_status(self.conn, token, status, detail)

    def _reload(self, pos: sqlite3.Row) -> sqlite3.Row:
        return self.conn.execute("SELECT * FROM positions WHERE id = ?", (pos["id"],)).fetchone()

    # --------------------------------------------------------------- reporting
    def summary(self) -> AccountSummary:
        acct = self.conn.execute("SELECT * FROM account WHERE run_id = ?", (self.run_id,)).fetchone()
        open_rows = self._positions("status = 'open'")
        value = sum(r["last_nlv_aud"] or 0.0 for r in open_rows)
        unreal = sum((r["last_nlv_aud"] or 0.0) - r["cost_aud"] for r in open_rows)
        closed = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(pnl_aud), 0) AS pnl FROM positions WHERE run_id=? AND status='closed'",
            (self.run_id,)).fetchone()
        return AccountSummary(acct["starting_cash_aud"], acct["cash_aud"], value, acct["cash_aud"] + value,
                              closed["pnl"], unreal, len(open_rows), closed["n"])

    def snapshot(self, now: float) -> AccountSummary:
        a = self.summary()
        self.conn.execute(
            "INSERT INTO equity_snapshots(run_id, at, cash_aud, positions_value_aud, equity_aud, realised_pnl_aud, "
            "unrealised_pnl_aud) VALUES (?,?,?,?,?,?,?)",
            (self.run_id, now, a.cash_aud, a.positions_value_aud, a.equity_aud, a.realised_pnl_aud,
             a.unrealised_pnl_aud),
        )
        return a
