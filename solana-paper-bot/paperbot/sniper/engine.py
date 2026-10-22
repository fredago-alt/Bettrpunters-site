"""pump.fun sniper PAPER TRADING engine.

Fed decoded on-chain events (in arrival order) plus a clock. Shares the
account/positions/order_events/equity tables with the DEX Screener bot under
its own run_id, so the existing metrics and dashboard work unchanged.

Fills are simulated against the bonding curve: the price impact of our order
is computed exactly from the reserves current at fill time (after the
simulated latency), and the per-token trade fee is taken from on-chain events.
"""

from __future__ import annotations

import json
import logging
import random
import sqlite3
from dataclasses import dataclass, field

from .. import PAPER_TRADING_ONLY
from ..db import transaction
from .pumpfun import (LAMPORTS_PER_SOL, TOKEN_UNIT, CompleteEvent, CreateEvent, CurveAccount, TradeEvent,
                      curve_buy_tokens, curve_progress_pct, simulate_curve_buy, simulate_curve_sell, spot_price_sol)

log = logging.getLogger(__name__)
assert PAPER_TRADING_ONLY is True


@dataclass
class Bar:
    start: float
    n_buys: int = 0
    n_sells: int = 0
    buy_lamports: int = 0
    sell_lamports: int = 0
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None


@dataclass
class MintState:
    mint: str
    bonding_curve: str
    symbol: str | None
    creator: str
    seen_at: float
    creator_fee_bps: int | None
    virtual_quote: int | None = None
    virtual_token: int | None = None
    real_token: int | None = None
    protocol_fee_bps: int | None = None
    buys: int = 0
    sells: int = 0
    buy_lamports: int = 0
    sell_lamports: int = 0
    creator_buy_lamports: int = 0
    buyers: set = field(default_factory=set)
    creator_sold: bool = False
    complete: bool = False
    decided: bool = False  # entry evaluated to a final outcome
    last_reasons: list = field(default_factory=list)
    bar: Bar | None = None
    last_update: float = 0.0


class SniperEngine:
    def __init__(self, conn: sqlite3.Connection, settings, fx, sol_price, rng: random.Random | None = None):
        self.conn = conn
        self.s = settings
        self.run_id = settings.run_id
        self.fx = fx  # .get() -> FxQuote (AUD per USD)
        self.sol_price = sol_price  # .get() -> (usd_per_sol, source) or None
        self.rng = rng or random.Random(settings.random_seed)
        self.states: dict[str, MintState] = {}
        self.stats = {"creates": 0, "trades": 0, "skipped_mayhem": 0, "skipped_non_sol": 0, "signals": 0}

    # ------------------------------------------------------------ conversions
    def _rates(self):
        sp = self.sol_price.get()
        if sp is None:
            return None
        q = self.fx.get()
        return sp[0], sp[1], q.aud_per_usd, q.source

    @staticmethod
    def lamports_to_aud(lamports: float, sol_usd: float, aud_per_usd: float) -> float:
        return lamports / LAMPORTS_PER_SOL * sol_usd * aud_per_usd

    @staticmethod
    def aud_to_lamports(aud: float, sol_usd: float, aud_per_usd: float) -> int:
        return int(aud / aud_per_usd / sol_usd * LAMPORTS_PER_SOL)

    def fee_bps(self, st: MintState) -> int:
        proto = st.protocol_fee_bps if st.protocol_fee_bps is not None else self.s.protocol_fee_bps
        creator = st.creator_fee_bps if st.creator_fee_bps is not None else self.s.default_creator_fee_bps
        return proto + creator

    # ------------------------------------------------------------ account
    def cash(self) -> float:
        return self.conn.execute("SELECT cash_aud FROM account WHERE run_id=?", (self.run_id,)).fetchone()[0]

    def _set_cash(self, v: float, now: float) -> None:
        if v < -1e-9:
            raise RuntimeError(f"virtual cash would go negative ({v})")
        self.conn.execute("UPDATE account SET cash_aud=?, updated_at=? WHERE run_id=?", (max(v, 0.0), now, self.run_id))

    def _positions(self, where: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM positions WHERE run_id=? AND {where} ORDER BY id",
                                 (self.run_id, *params)).fetchall()

    def active_positions(self) -> list[sqlite3.Row]:
        return self._positions("status IN ('pending','open')")

    def _event(self, side, event, mint, at, position_id=None, reason=None, price_usd=None, details=None):
        self.conn.execute(
            "INSERT INTO order_events(run_id, position_id, token_address, side, event, reason, at, observation_id, "
            "ref_price_usd, details_json) VALUES (?,?,?,?,?,?,?,NULL,?,?)",
            (self.run_id, position_id, mint, side, event, reason, at, price_usd,
             json.dumps(details) if details else None))

    # ------------------------------------------------------------ events
    def on_create(self, ev: CreateEvent, received_at: float, signature: str = "", slot: int = 0) -> None:
        self.stats["creates"] += 1
        rejection = None
        if ev.mayhem:
            rejection = "mayhem_mode_unmodelled"
            self.stats["skipped_mayhem"] += 1
        elif not ev.is_sol_quoted:
            rejection = "non_sol_quote"
            self.stats["skipped_non_sol"] += 1
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO pump_tokens(mint, bonding_curve, name, symbol, creator, quote_mint, mayhem, "
                "creator_fee_bps, chain_created_at, seen_at, signature, slot, status, rejection) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev.mint, ev.bonding_curve, ev.name[:100], ev.symbol[:30], ev.creator, ev.quote_mint, int(ev.mayhem),
                 ev.creator_fee_bps, ev.timestamp, received_at, signature, slot,
                 "skipped" if rejection else "watching", rejection))
        if rejection is None:
            self.states[ev.mint] = MintState(ev.mint, ev.bonding_curve, ev.symbol, ev.creator, received_at,
                                             ev.creator_fee_bps, ev.virtual_quote, ev.virtual_token,
                                             last_update=received_at)

    def on_trade(self, ev: TradeEvent, received_at: float) -> None:
        st = self.states.get(ev.mint)
        if st is None:  # created before we started watching, or skipped type
            return
        if ev.mayhem or not ev.is_sol_quoted:
            return
        self.stats["trades"] += 1
        with transaction(self.conn):
            self._roll_bar(st, received_at)
            price = spot_price_sol(ev.virtual_quote, ev.virtual_token)
            st.virtual_quote, st.virtual_token, st.real_token = ev.virtual_quote, ev.virtual_token, ev.real_token
            if ev.fee_bps:
                st.protocol_fee_bps = ev.fee_bps
            if ev.creator_fee_bps is not None and (ev.fee_bps or ev.creator_fee_bps):
                st.creator_fee_bps = ev.creator_fee_bps
            st.last_update = received_at
            b = st.bar or Bar(start=received_at)
            st.bar = b
            if price is not None:
                b.open = price if b.open is None else b.open
                b.high = price if b.high is None else max(b.high, price)
                b.low = price if b.low is None else min(b.low, price)
                b.close = price
            if ev.is_buy:
                st.buys += 1
                st.buy_lamports += ev.quote_amount
                b.n_buys += 1
                b.buy_lamports += ev.quote_amount
                if ev.user == st.creator:
                    st.creator_buy_lamports += ev.quote_amount
                else:
                    st.buyers.add(ev.user)
            else:
                st.sells += 1
                st.sell_lamports += ev.quote_amount
                b.n_sells += 1
                b.sell_lamports += ev.quote_amount
                if ev.user == st.creator:
                    st.creator_sold = True
            self._after_market_update(st, received_at)

    def on_complete(self, ev: CompleteEvent, received_at: float) -> None:
        st = self.states.get(ev.mint)
        if st is None:
            return
        with transaction(self.conn):
            st.complete = True
            for pos in self._positions("token_address=? AND status='pending'", (ev.mint,)):
                self._abandon(pos, received_at, "curve_completed")
            for pos in self._positions("token_address=? AND status='open'", (ev.mint,)):
                self._close(pos, st, received_at, "curve_complete", force=True)

    def on_curve_account(self, mint: str, acc: CurveAccount | None, now: float) -> None:
        """Independent refresh of a held token's reserves via RPC (not via the stream)."""
        st = self.states.get(mint)
        if st is None or acc is None:
            return
        with transaction(self.conn):
            st.virtual_quote, st.virtual_token, st.real_token = acc.virtual_quote, acc.virtual_token, acc.real_token
            if acc.creator_fee_bps is not None:
                st.creator_fee_bps = acc.creator_fee_bps
            if acc.complete and not st.complete:
                st.complete = True
                for pos in self._positions("token_address=? AND status='open'", (mint,)):
                    self._close(pos, st, now, "curve_complete", force=True)
                return
            self._after_market_update(st, now)

    def tick(self, now: float) -> None:
        """Time-driven work: latency fills, max-hold exits, bar flushes, cleanup."""
        with transaction(self.conn):
            for pos in self._positions("status='pending'"):
                st = self.states.get(pos["token_address"])
                if st is None:
                    self._abandon(pos, now, "state_lost")
                elif now >= pos["signal_at"] + self.s.latency_ms / 1000.0:
                    self._try_fill_entry(pos, st, now)
            for pos in self._positions("status='open'"):
                st = self.states.get(pos["token_address"])
                if st is not None:
                    self._manage_open(pos, st, now)
            held = {p["token_address"] for p in self.active_positions()}
            for mint, st in list(self.states.items()):
                if st.bar and now - st.bar.start >= self.s.bar_seconds:
                    self._flush_bar(st, now)
                age = now - st.seen_at
                if not st.decided and age > self.s.max_age_seconds:
                    st.decided = True
                    self._token_status(mint, "rejected", ",".join(st.last_reasons) or "no_activity_in_window")
                if age > self.s.track_window_seconds and mint not in held:
                    if st.bar:
                        self._flush_bar(st, now)
                    del self.states[mint]

    # ------------------------------------------------------------ bars
    def _roll_bar(self, st: MintState, now: float) -> None:
        if st.bar and now - st.bar.start >= self.s.bar_seconds:
            self._flush_bar(st, now)

    def _flush_bar(self, st: MintState, now: float) -> None:
        b = st.bar
        st.bar = None
        if b is None:
            return
        sp = self.sol_price.get()
        self.conn.execute(
            "INSERT INTO pump_bars(mint, bar_start, bar_end, n_buys, n_sells, buy_lamports, sell_lamports, "
            "unique_buyers, open_price_sol, high_price_sol, low_price_sol, close_price_sol, virtual_quote, "
            "virtual_token, real_token, creator_sold, sol_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (st.mint, b.start, min(now, b.start + self.s.bar_seconds), b.n_buys, b.n_sells, b.buy_lamports,
             b.sell_lamports, len(st.buyers), b.open, b.high, b.low, b.close, st.virtual_quote, st.virtual_token,
             st.real_token, int(st.creator_sold), sp[0] if sp else None))

    # ------------------------------------------------------------ entry
    def entry_reasons(self, st: MintState, now: float) -> list[str]:
        s = self.s
        r = []
        age = now - st.seen_at
        if age < s.min_age_seconds:
            r.append("too_young")
        if age > s.max_age_seconds:
            r.append("too_old")
        if st.buys < s.min_buys:
            r.append("buys_below_min")
        if len(st.buyers) < s.min_unique_buyers:
            r.append("unique_buyers_below_min")
        net = (st.buy_lamports - st.creator_buy_lamports - st.sell_lamports) / LAMPORTS_PER_SOL
        if net < s.min_net_inflow_sol:
            r.append("net_inflow_below_min")
        if st.sells and st.buys / st.sells < s.min_buy_sell_ratio:
            r.append("buy_sell_ratio_below_min")
        if st.creator_buy_lamports / LAMPORTS_PER_SOL > s.max_creator_buy_sol:
            r.append("creator_buy_too_large")
        if s.reject_creator_sold and st.creator_sold:
            r.append("creator_sold")
        if st.real_token is not None and curve_progress_pct(st.real_token) > s.max_curve_progress_pct:
            r.append("curve_too_advanced")
        if st.complete:
            r.append("curve_complete")
        if not st.virtual_quote or not st.virtual_token:
            r.append("reserves_unknown")
        return r

    def _after_market_update(self, st: MintState, now: float) -> None:
        for pos in self._positions("token_address=? AND status='open'", (st.mint,)):
            self._manage_open(pos, st, now)
        if st.decided:
            return
        reasons = self.entry_reasons(st, now)
        st.last_reasons = reasons
        if reasons:
            if "too_old" in reasons or "creator_sold" in reasons or "curve_complete" in reasons:
                st.decided = True
                self._token_status(st.mint, "rejected", ",".join(reasons))
            return
        # Market conditions pass; account constraints decide the final outcome.
        st.decided = True
        account_reasons = []
        rates = self._rates()
        if rates is None:
            account_reasons.append("sol_price_unavailable")
        if len(self.active_positions()) >= self.s.max_open_positions:
            account_reasons.append("max_open_positions")
        elif self._budget_aud() < self.s.min_position_aud:
            account_reasons.append("insufficient_cash")
        metrics = {"age_s": now - st.seen_at, "buys": st.buys, "sells": st.sells, "unique_buyers": len(st.buyers),
                   "net_inflow_sol": (st.buy_lamports - st.creator_buy_lamports - st.sell_lamports) / LAMPORTS_PER_SOL,
                   "curve_progress_pct": curve_progress_pct(st.real_token) if st.real_token is not None else None}
        self.conn.execute(
            "INSERT INTO filter_decisions(run_id, observation_id, token_address, decided_at, passed, reasons_json) "
            "VALUES (?,?,?,?,?,?)",
            (self.run_id, 0, st.mint, now, int(not account_reasons),
             json.dumps({"reasons": account_reasons, "metrics": metrics})))
        if account_reasons:
            self._token_status(st.mint, "rejected", ",".join(account_reasons))
            return
        price_sol = spot_price_sol(st.virtual_quote, st.virtual_token)
        cur = self.conn.execute(
            "INSERT INTO positions(run_id, token_address, pair_address, symbol, status, signal_observation_id, "
            "signal_at, signal_price_usd) VALUES (?,?,?,?, 'pending', 0, ?, ?)",
            (self.run_id, st.mint, st.bonding_curve, st.symbol, now, price_sol * rates[0]))
        self.stats["signals"] += 1
        self._event("buy", "signal", st.mint, now, cur.lastrowid, price_usd=price_sol * rates[0], details=metrics)
        self._token_status(st.mint, "signal", None)
        if self.s.latency_ms == 0:
            self._try_fill_entry(self.conn.execute("SELECT * FROM positions WHERE id=?", (cur.lastrowid,)).fetchone(),
                                 st, now)

    def _budget_aud(self) -> float:
        return min(self.cash() * self.s.position_pct_of_cash, self.s.max_position_aud)

    def _try_fill_entry(self, pos, st: MintState, now: float) -> None:
        rates = self._rates()
        if rates is None:
            return self._abandon(pos, now, "sol_price_unavailable")
        sol_usd, sol_src, aud_per_usd, fx_src = rates
        if st.complete:
            return self._abandon(pos, now, "curve_completed")
        price_usd = spot_price_sol(st.virtual_quote, st.virtual_token) * sol_usd
        drift = (price_usd / pos["signal_price_usd"] - 1) * 100
        if drift > self.s.max_entry_drift_pct:
            return self._abandon(pos, now, "entry_price_drift", {"drift_pct": drift})
        if self.rng.random() < self.s.entry_fill_failure_prob:
            return self._abandon(pos, now, "simulated_fill_failure")
        budget_aud = self._budget_aud()
        if budget_aud < self.s.min_position_aud:
            return self._abandon(pos, now, "insufficient_cash")
        budget = self.aud_to_lamports(budget_aud, sol_usd, aud_per_usd)
        try:
            buy = simulate_curve_buy(st.virtual_quote, st.virtual_token, budget, self.fee_bps(st),
                                     self.s.network_fee_total)
        except ValueError:
            return self._abandon(pos, now, "budget_below_network_fee")
        if buy.tokens_raw <= 0:
            return self._abandon(pos, now, "zero_tokens")
        to_aud = lambda lam: self.lamports_to_aud(lam, sol_usd, aud_per_usd)  # noqa: E731
        cost_aud = to_aud(buy.total_cost)
        self._set_cash(self.cash() - cost_aud, now)
        qty = buy.tokens_raw / TOKEN_UNIT
        sell = simulate_curve_sell(st.virtual_quote + buy.curve_in, st.virtual_token - buy.tokens_raw,
                                   buy.tokens_raw, self.fee_bps(st), self.s.network_fee_total)
        self.conn.execute(
            "UPDATE positions SET status='open', opened_at=?, qty=?, entry_ref_price_usd=?, entry_exec_price_usd=?, "
            "entry_aud_per_usd=?, entry_fx_source=?, entry_notional_aud=?, entry_fee_aud=?, entry_slippage_aud=?, "
            "cost_aud=?, last_mark_at=?, last_ref_price_usd=?, last_nlv_aud=?, entry_sol_usd=?, "
            "entry_network_fee_aud=?, entry_curve_in_lamports=?, tokens_raw=? WHERE id=?",
            (now, qty, price_usd, buy.curve_in / LAMPORTS_PER_SOL * sol_usd / qty, aud_per_usd,
             f"{fx_src}; SOL/USD {sol_src}", to_aud(buy.curve_in), to_aud(buy.trade_fee + buy.network_fee),
             to_aud(buy.slippage), cost_aud, now, price_usd, to_aud(sell.proceeds), sol_usd,
             to_aud(buy.network_fee), buy.curve_in, buy.tokens_raw, pos["id"]))
        self._event("buy", "filled", st.mint, now, pos["id"], price_usd=price_usd,
                    details={"budget_lamports": budget, "curve_in": buy.curve_in, "trade_fee": buy.trade_fee,
                             "network_fee": buy.network_fee, "tokens_raw": buy.tokens_raw,
                             "fee_bps": self.fee_bps(st), "sol_usd": sol_usd, "aud_per_usd": aud_per_usd})
        self._token_status(st.mint, "position_open", None)

    def _abandon(self, pos, now: float, reason: str, details: dict | None = None) -> None:
        self.conn.execute("UPDATE positions SET status='abandoned', abandon_reason=? WHERE id=?", (reason, pos["id"]))
        self._event("buy", "abandoned", pos["token_address"], now, pos["id"], reason=reason, details=details)
        self._token_status(pos["token_address"], "abandoned", reason)

    # ------------------------------------------------------------ exit
    def _nlv(self, pos, st: MintState, rates) -> tuple:
        """Value our tokens by selling them into the current reserves with our own
        (simulated) buy added back in: on-chain reserves never contained it, but in
        reality it would still be sitting in the curve when we sell."""
        sol_usd, _, aud_per_usd, _ = rates
        vq, vt = st.virtual_quote, st.virtual_token
        cin = pos["entry_curve_in_lamports"] or 0
        if cin:
            vt -= curve_buy_tokens(vq, vt, cin)
            vq += cin
        sell = simulate_curve_sell(vq, vt, pos["tokens_raw"] or int(round(pos["qty"] * TOKEN_UNIT)),
                                   self.fee_bps(st), self.s.network_fee_total)
        return sell, self.lamports_to_aud(sell.proceeds, sol_usd, aud_per_usd)

    def exit_reason(self, pos, st: MintState, nlv_aud: float, now: float) -> str | None:
        pnl_pct = (nlv_aud / pos["cost_aud"] - 1) * 100
        if pnl_pct >= self.s.take_profit_pct:
            return "take_profit"
        if pnl_pct <= self.s.stop_loss_pct:
            return "stop_loss"
        if self.s.exit_on_creator_sell and st.creator_sold:
            return "creator_sold"
        if now - pos["opened_at"] >= self.s.max_hold_seconds:
            return "max_hold"
        return None

    def _manage_open(self, pos, st: MintState, now: float) -> None:
        rates = self._rates()
        if rates is None or not st.virtual_quote:
            return
        sell, nlv = self._nlv(pos, st, rates)
        price_usd = spot_price_sol(st.virtual_quote, st.virtual_token) * rates[0]
        self.conn.execute("UPDATE positions SET last_mark_at=?, last_ref_price_usd=?, last_nlv_aud=? WHERE id=?",
                          (now, price_usd, nlv, pos["id"]))
        pos = self.conn.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        if pos["pending_exit_reason"] is None:
            reason = self.exit_reason(pos, st, nlv, now)
            if reason is None:
                return
            self.conn.execute("UPDATE positions SET pending_exit_reason=?, pending_exit_at=? WHERE id=?",
                              (reason, now, pos["id"]))
            self._event("sell", "signal", st.mint, now, pos["id"], reason=reason, price_usd=price_usd)
            pos = self.conn.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
        if now >= pos["pending_exit_at"] + self.s.latency_ms / 1000.0:
            if self.rng.random() < self.s.exit_fill_failure_prob:
                self.conn.execute("UPDATE positions SET failed_exit_attempts=failed_exit_attempts+1, "
                                  "pending_exit_at=? WHERE id=?", (now, pos["id"]))
                self._event("sell", "failed", st.mint, now, pos["id"], reason="simulated_fill_failure",
                            price_usd=price_usd)
                return
            self._close(pos, st, now, pos["pending_exit_reason"])

    def _close(self, pos, st: MintState, now: float, reason: str, force: bool = False) -> None:
        rates = self._rates()
        if rates is None:
            return
        sol_usd, _, aud_per_usd, _ = rates
        sell, proceeds_aud = self._nlv(pos, st, rates)
        proceeds_aud = max(proceeds_aud, 0.0)
        to_aud = lambda lam: self.lamports_to_aud(lam, sol_usd, aud_per_usd)  # noqa: E731
        price_usd = spot_price_sol(st.virtual_quote, st.virtual_token) * sol_usd
        pnl = proceeds_aud - pos["cost_aud"]
        self._set_cash(self.cash() + proceeds_aud, now)
        self.conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, exit_reason=?, exit_ref_price_usd=?, "
            "exit_exec_price_usd=?, exit_aud_per_usd=?, exit_gross_aud=?, exit_fee_aud=?, exit_slippage_aud=?, "
            "proceeds_aud=?, pnl_aud=?, pnl_pct=?, hold_seconds=?, last_nlv_aud=?, pending_exit_reason=NULL, "
            "pending_exit_at=NULL, exit_sol_usd=?, exit_network_fee_aud=? WHERE id=?",
            (now, reason, price_usd, sell.curve_out / LAMPORTS_PER_SOL * sol_usd / pos["qty"], aud_per_usd,
             to_aud(sell.curve_out), to_aud(sell.trade_fee + sell.network_fee), to_aud(sell.slippage),
             proceeds_aud, pnl, pnl / pos["cost_aud"] * 100, now - pos["opened_at"], proceeds_aud, sol_usd,
             to_aud(sell.network_fee), pos["id"]))
        self._event("sell", "filled", st.mint, now, pos["id"], reason=reason, price_usd=price_usd,
                    details={"curve_out": sell.curve_out, "trade_fee": sell.trade_fee,
                             "network_fee": sell.network_fee, "pnl_aud": pnl, "forced": force})
        self._token_status(st.mint, "closed", reason)

    # ------------------------------------------------------------ misc
    def _token_status(self, mint: str, status: str, detail: str | None) -> None:
        self.conn.execute("UPDATE pump_tokens SET status=?, rejection=? WHERE mint=?", (status, detail, mint))

    def held_curves(self) -> dict[str, str]:
        return {p["pair_address"]: p["token_address"] for p in self.active_positions()}

    def summary(self) -> dict:
        acct = self.conn.execute("SELECT * FROM account WHERE run_id=?", (self.run_id,)).fetchone()
        open_rows = self._positions("status='open'")
        value = sum(r["last_nlv_aud"] or 0 for r in open_rows)
        closed = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(pnl_aud),0) FROM positions WHERE run_id=? AND "
                                   "status='closed'", (self.run_id,)).fetchone()
        return {"cash_aud": acct["cash_aud"], "equity_aud": acct["cash_aud"] + value, "open": len(open_rows),
                "closed": closed[0], "realised_pnl_aud": closed[1], "watching": len(self.states), **self.stats}

    def snapshot(self, now: float) -> None:
        sm = self.summary()
        open_rows = self._positions("status='open'")
        unreal = sum((r["last_nlv_aud"] or 0) - r["cost_aud"] for r in open_rows)
        self.conn.execute(
            "INSERT INTO equity_snapshots(run_id, at, cash_aud, positions_value_aud, equity_aud, realised_pnl_aud, "
            "unrealised_pnl_aud) VALUES (?,?,?,?,?,?,?)",
            (self.run_id, now, sm["cash_aud"], sm["equity_aud"] - sm["cash_aud"], sm["equity_aud"],
             sm["realised_pnl_aud"], unreal))
