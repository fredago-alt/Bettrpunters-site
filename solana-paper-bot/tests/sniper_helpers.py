"""Builders for synthetic pump.fun events (borsh-encoded per the published IDL)."""

import base64
import random
import struct

from paperbot.db import ensure_run, open_db
from paperbot.fx import StaticFx
from paperbot.sniper.config import SniperSettings
from paperbot.sniper.engine import SniperEngine
from paperbot.sniper.pumpfun import (CREATE_DISC, CREATE_SCHEMA, NATIVE_SOL_QUOTE, PUMP_PROGRAM, TRADE_DISC,
                                     TRADE_SCHEMA, b58decode, b58encode, curve_buy_tokens, curve_sell_lamports,
                                     decode_event)

T0 = 1_800_000_000.0
V_SOL0 = 30 * 10**9  # standard initial virtual reserves
V_TOK0 = 1_073_000_000 * 10**6
R_TOK0 = 793_100_000 * 10**6


def pk(seed: str) -> str:
    return b58encode((seed.encode() * 32)[:32])


def _enc(t, v) -> bytes:
    if t == "pubkey":
        return b58decode(v).rjust(32, b"\0")
    if t == "u64":
        return struct.pack("<Q", v)
    if t == "i64":
        return struct.pack("<q", v)
    if t == "u16":
        return struct.pack("<H", v)
    if t == "u8":
        return bytes([v])
    if t == "bool":
        return bytes([1 if v else 0])
    if t == "string":
        b = v.encode()
        return struct.pack("<I", len(b)) + b
    if isinstance(t, tuple) and t[0] == "vec":
        return struct.pack("<I", len(v)) + b"".join(_enc(t[1], x) for x in v)
    if isinstance(t, tuple) and t[0] == "struct":
        return b"".join(_enc(ft, v[fn]) for fn, ft in t[1])
    raise ValueError(t)


DEFAULTS = {"pubkey": NATIVE_SOL_QUOTE, "u64": 0, "i64": 0, "u16": 0, "u8": 0, "bool": False, "string": ""}


def encode(disc: bytes, schema, values: dict, truncate_after: str | None = None) -> bytes:
    out = disc
    for name, t in schema:
        v = values.get(name, [] if isinstance(t, tuple) else DEFAULTS[t])
        out += _enc(t, v)
        if name == truncate_after:
            break
    return out


def log_lines(payloads: list[bytes], program: str = PUMP_PROGRAM) -> list[str]:
    lines = [f"Program {program} invoke [1]", "Program log: Instruction: Buy"]
    lines += ["Program data: " + base64.b64encode(p).decode() for p in payloads]
    lines += [f"Program {program} consumed 1 of 2 compute units", f"Program {program} success"]
    return lines


class Curve:
    """Synthetic standard bonding curve that produces consistent trade events."""

    def __init__(self, mint="MINT", creator="CREATOR", mayhem=False, quote=NATIVE_SOL_QUOTE, creator_fee_bps=30):
        self.mint, self.creator = pk(mint), pk(creator)
        self.curve = pk(mint + "curve")
        self.vq, self.vt, self.rt = V_SOL0, V_TOK0, R_TOK0
        self.mayhem, self.quote, self.cfee = mayhem, quote, creator_fee_bps

    def create_event(self):
        return decode_event(encode(CREATE_DISC, CREATE_SCHEMA, {
            "name": "Test", "symbol": "TST", "uri": "u", "mint": self.mint, "bonding_curve": self.curve,
            "user": self.creator, "creator": self.creator, "timestamp": int(T0), "virtual_token_reserves": self.vt,
            "virtual_sol_reserves": self.vq, "real_token_reserves": self.rt, "is_mayhem_mode": self.mayhem,
            "quote_mint": self.quote, "virtual_quote_reserves": self.vq, "creator_fee_bps": self.cfee}))

    def trade(self, user: str, sol: float = 0.0, tokens: int = 0, is_buy=True, raw=False):
        if is_buy:
            q = int(sol * 10**9)
            tok = curve_buy_tokens(self.vq, self.vt, q)
            self.vq += q
            self.vt -= tok
            self.rt -= tok
        else:
            tok = tokens
            q = curve_sell_lamports(self.vq, self.vt, tok)
            self.vq -= q
            self.vt += tok
            self.rt += tok
        payload = encode(TRADE_DISC, TRADE_SCHEMA, {
            "mint": self.mint, "sol_amount": q, "token_amount": tok, "is_buy": is_buy, "user": pk(user),
            "timestamp": int(T0), "virtual_sol_reserves": self.vq, "virtual_token_reserves": self.vt,
            "real_sol_reserves": max(self.vq - V_SOL0, 0), "real_token_reserves": self.rt, "fee_basis_points": 95,
            "creator": self.creator, "creator_fee_basis_points": self.cfee, "ix_name": "buy" if is_buy else "sell",
            "mayhem_mode": self.mayhem, "quote_mint": self.quote, "quote_amount": q,
            "virtual_quote_reserves": self.vq, "real_quote_reserves": max(self.vq - V_SOL0, 0)})
        return payload if raw else decode_event(payload)


class FixedSol:
    def __init__(self, usd=150.0):
        self.usd = usd

    def get(self):
        return None if self.usd is None else (self.usd, "test")

    def refresh(self):
        pass


def make_sniper(**kw):
    base = dict(latency_ms=0.0, entry_fill_failure_prob=0.0, exit_fill_failure_prob=0.0, min_age_seconds=0.0,
                min_buys=3, min_unique_buyers=3, min_net_inflow_sol=1.0, min_buy_sell_ratio=1.0)
    base.update(kw)
    s = SniperSettings(**base)
    s.validate()
    conn = open_db(":memory:")
    ensure_run(conn, s.run_id, "live", s)
    sol = FixedSol()
    eng = SniperEngine(conn, s, StaticFx(1.5), sol, rng=random.Random(1))
    return eng, conn, sol


def position(conn):
    return conn.execute("SELECT * FROM positions ORDER BY id DESC").fetchone()


def pump_until_signal(eng, c: Curve, t=T0, buyers=4, sol_each=0.5):
    """Create + `buyers` distinct buys; returns the time of the last event."""
    eng.on_create(c.create_event(), t)
    for i in range(buyers):
        t += 1
        eng.on_trade(c.trade(f"buyer{i}", sol_each), t)
    return t
