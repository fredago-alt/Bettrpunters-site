"""Read-only decoding of pump.fun program logs, plus bonding-curve maths.

Events are Anchor events emitted in transaction logs as ``Program data: <base64>``.
Only events emitted by the pump.fun program itself are accepted: other
programs reuse the same event names, so each ``Program data`` line is
attributed to the program on top of the invoke stack.

Field layouts follow pump.fun's published IDL
(github.com/pump-fun/pump-public-docs, idl/pump.json). Decoding is
sequential and tolerant: if an older/shorter event ends early, the remaining
fields are None; trailing bytes from newer versions are ignored.

Verified against real mainnet trades: for standard SOL-quoted curves the
constant-product maths below reproduces the on-chain token/SOL amounts
exactly. "Mayhem mode" curves do NOT follow it, so the simulator skips them.
"""

from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass, field

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
NATIVE_SOL_QUOTE = "11111111111111111111111111111111"  # quote_mint value for SOL-quoted curves
LAMPORTS_PER_SOL = 1_000_000_000
TOKEN_UNIT = 1_000_000  # pump.fun tokens have 6 decimals
INITIAL_REAL_TOKEN_RESERVES = 793_100_000 * TOKEN_UNIT  # curve progress reference

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def _disc(kind: str, name: str) -> bytes:
    return hashlib.sha256(f"{kind}:{name}".encode()).digest()[:8]


# --------------------------------------------------------------- borsh reader
class _Reader:
    def __init__(self, raw: bytes, offset: int = 8):
        self.raw, self.o = raw, offset

    def _take(self, n: int) -> bytes:
        if self.o + n > len(self.raw):
            raise EOFError
        v = self.raw[self.o:self.o + n]
        self.o += n
        return v

    def read(self, t):
        if t == "pubkey":
            return b58encode(self._take(32))
        if t == "u64":
            return struct.unpack("<Q", self._take(8))[0]
        if t == "i64":
            return struct.unpack("<q", self._take(8))[0]
        if t == "u16":
            return struct.unpack("<H", self._take(2))[0]
        if t == "u8":
            return self._take(1)[0]
        if t == "bool":
            return self._take(1)[0] == 1
        if t == "string":
            n = struct.unpack("<I", self._take(4))[0]
            if n > 4096:
                raise ValueError("implausible string length")
            return self._take(n).decode("utf-8", "replace")
        if isinstance(t, tuple) and t[0] == "vec":
            n = struct.unpack("<I", self._take(4))[0]
            if n > 1000:
                raise ValueError("implausible vec length")
            return [self.read(t[1]) for _ in range(n)]
        if isinstance(t, tuple) and t[0] == "struct":
            return {k: self.read(v) for k, v in t[1]}
        raise ValueError(f"unknown type {t}")


def _decode(raw: bytes, schema: list[tuple[str, object]], required: int, offset: int = 8) -> dict | None:
    """Decode fields in order; fields past the end of ``raw`` are None.
    Returns None if fewer than ``required`` leading fields are present."""
    r = _Reader(raw, offset)
    out: dict = {}
    for i, (name, t) in enumerate(schema):
        try:
            out[name] = r.read(t)
        except EOFError:
            if i < required:
                return None
            out.update({n: None for n, _ in schema[i:]})
            break
    return out


SHAREHOLDER = ("struct", [("address", "pubkey"), ("share_bps", "u16")])

TRADE_SCHEMA = [
    ("mint", "pubkey"), ("sol_amount", "u64"), ("token_amount", "u64"), ("is_buy", "bool"), ("user", "pubkey"),
    ("timestamp", "i64"), ("virtual_sol_reserves", "u64"), ("virtual_token_reserves", "u64"),
    ("real_sol_reserves", "u64"), ("real_token_reserves", "u64"), ("fee_recipient", "pubkey"),
    ("fee_basis_points", "u64"), ("fee", "u64"), ("creator", "pubkey"), ("creator_fee_basis_points", "u64"),
    ("creator_fee", "u64"), ("track_volume", "bool"), ("total_unclaimed_tokens", "u64"),
    ("total_claimed_tokens", "u64"), ("current_sol_volume", "u64"), ("last_update_timestamp", "i64"),
    ("ix_name", "string"), ("mayhem_mode", "bool"), ("cashback_fee_basis_points", "u64"), ("cashback", "u64"),
    ("buyback_fee_basis_points", "u64"), ("buyback_fee", "u64"), ("shareholders", ("vec", SHAREHOLDER)),
    ("quote_mint", "pubkey"), ("quote_amount", "u64"), ("virtual_quote_reserves", "u64"),
    ("real_quote_reserves", "u64"), ("holder_rewards_bps", "u64"), ("holder_rewards", "u64"),
    ("creator_fee_unclaimed", "u64"),
]
CREATE_SCHEMA = [
    ("name", "string"), ("symbol", "string"), ("uri", "string"), ("mint", "pubkey"), ("bonding_curve", "pubkey"),
    ("user", "pubkey"), ("creator", "pubkey"), ("timestamp", "i64"), ("virtual_token_reserves", "u64"),
    ("virtual_sol_reserves", "u64"), ("real_token_reserves", "u64"), ("token_total_supply", "u64"),
    ("token_program", "pubkey"), ("is_mayhem_mode", "bool"), ("is_cashback_enabled", "bool"),
    ("quote_mint", "pubkey"), ("virtual_quote_reserves", "u64"), ("creator_fee_bps", "u64"),
    ("is_holder_reward", "bool"), ("depth", "u8"),
]
COMPLETE_SCHEMA = [("user", "pubkey"), ("mint", "pubkey"), ("bonding_curve", "pubkey"), ("timestamp", "i64"),
                   ("quote_mint", "pubkey")]
BONDING_CURVE_SCHEMA = [
    ("virtual_token_reserves", "u64"), ("virtual_quote_reserves", "u64"), ("real_token_reserves", "u64"),
    ("real_quote_reserves", "u64"), ("token_total_supply", "u64"), ("complete", "bool"), ("creator", "pubkey"),
    ("is_mayhem_mode", "bool"), ("is_cashback_coin", "bool"), ("quote_mint", "pubkey"), ("creator_fee_bps", "u64"),
]

TRADE_DISC = _disc("event", "TradeEvent")
CREATE_DISC = _disc("event", "CreateEvent")
COMPLETE_DISC = _disc("event", "CompleteEvent")
BONDING_CURVE_DISC = _disc("account", "BondingCurve")


@dataclass(frozen=True)
class TradeEvent:
    mint: str
    is_buy: bool
    user: str
    timestamp: int
    quote_mint: str  # NATIVE_SOL_QUOTE for SOL curves (also assumed for legacy events without the field)
    quote_amount: int  # lamports for SOL curves; excludes fees
    token_amount: int  # raw (6 decimals)
    virtual_quote: int  # reserves AFTER this trade
    virtual_token: int
    real_quote: int
    real_token: int
    fee_bps: int | None
    creator_fee_bps: int | None
    creator: str | None
    mayhem: bool
    ix_name: str | None

    @property
    def is_sol_quoted(self) -> bool:
        return self.quote_mint == NATIVE_SOL_QUOTE

    @property
    def price_sol(self) -> float | None:
        return spot_price_sol(self.virtual_quote, self.virtual_token)


@dataclass(frozen=True)
class CreateEvent:
    mint: str
    bonding_curve: str
    name: str
    symbol: str
    creator: str
    timestamp: int | None
    quote_mint: str
    mayhem: bool
    creator_fee_bps: int | None
    virtual_quote: int | None
    virtual_token: int | None

    @property
    def is_sol_quoted(self) -> bool:
        return self.quote_mint == NATIVE_SOL_QUOTE


@dataclass(frozen=True)
class CompleteEvent:
    mint: str
    bonding_curve: str


@dataclass(frozen=True)
class CurveAccount:
    virtual_token: int
    virtual_quote: int
    real_token: int
    real_quote: int
    complete: bool
    mayhem: bool | None
    quote_mint: str
    creator_fee_bps: int | None
    extra: dict = field(default_factory=dict, compare=False)


def decode_event(raw: bytes):
    head = raw[:8]
    try:
        if head == TRADE_DISC:
            d = _decode(raw, TRADE_SCHEMA, required=10)
            if d is None:
                return None
            legacy = d["quote_mint"] is None
            return TradeEvent(
                mint=d["mint"], is_buy=d["is_buy"], user=d["user"], timestamp=d["timestamp"],
                quote_mint=NATIVE_SOL_QUOTE if legacy else d["quote_mint"],
                quote_amount=d["sol_amount"] if legacy else d["quote_amount"],
                token_amount=d["token_amount"],
                virtual_quote=d["virtual_sol_reserves"] if legacy else d["virtual_quote_reserves"],
                virtual_token=d["virtual_token_reserves"],
                real_quote=d["real_sol_reserves"] if legacy else d["real_quote_reserves"],
                real_token=d["real_token_reserves"],
                fee_bps=d["fee_basis_points"], creator_fee_bps=d["creator_fee_basis_points"],
                creator=d["creator"], mayhem=bool(d["mayhem_mode"]), ix_name=d["ix_name"],
            )
        if head == CREATE_DISC:
            d = _decode(raw, CREATE_SCHEMA, required=6)
            if d is None:
                return None
            return CreateEvent(
                mint=d["mint"], bonding_curve=d["bonding_curve"], name=d["name"], symbol=d["symbol"],
                creator=d["creator"] or d["user"], timestamp=d["timestamp"],
                quote_mint=d["quote_mint"] or NATIVE_SOL_QUOTE, mayhem=bool(d["is_mayhem_mode"]),
                creator_fee_bps=d["creator_fee_bps"],
                virtual_quote=d["virtual_quote_reserves"] or d["virtual_sol_reserves"],
                virtual_token=d["virtual_token_reserves"],
            )
        if head == COMPLETE_DISC:
            d = _decode(raw, COMPLETE_SCHEMA, required=3)
            return None if d is None else CompleteEvent(d["mint"], d["bonding_curve"])
    except (struct.error, ValueError, UnicodeDecodeError):
        return None
    return None


def decode_curve_account(data: bytes) -> CurveAccount | None:
    if data[:8] != BONDING_CURVE_DISC:
        return None
    d = _decode(data, BONDING_CURVE_SCHEMA, required=6)
    if d is None:
        return None
    return CurveAccount(d["virtual_token_reserves"], d["virtual_quote_reserves"], d["real_token_reserves"],
                        d["real_quote_reserves"], d["complete"], d["is_mayhem_mode"],
                        d["quote_mint"] or NATIVE_SOL_QUOTE, d["creator_fee_bps"])


def events_from_logs(logs: list[str]) -> list:
    """pump.fun events from one transaction's log lines, attributed by invoke stack."""
    stack: list[str] = []
    out = []
    for line in logs:
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line.split()[1])
        elif line.startswith("Program ") and (line.endswith(" success") or " failed" in line):
            if stack:
                stack.pop()
        elif line.startswith("Program data: ") and stack and stack[-1] == PUMP_PROGRAM:
            try:
                raw = base64.b64decode(line[len("Program data: "):], validate=True)
            except ValueError:
                continue
            ev = decode_event(raw)
            if ev is not None:
                out.append(ev)
    return out


# ---------------------------------------------------------------- curve maths
def spot_price_sol(virtual_quote: int, virtual_token: int) -> float | None:
    """SOL per whole token implied by the reserves (no size impact)."""
    if not virtual_quote or not virtual_token or virtual_quote <= 0 or virtual_token <= 0:
        return None
    return (virtual_quote / LAMPORTS_PER_SOL) / (virtual_token / TOKEN_UNIT)


def curve_buy_tokens(virtual_quote: int, virtual_token: int, quote_in: int) -> int:
    """Raw tokens out for ``quote_in`` lamports entering the curve (fees excluded)."""
    if quote_in <= 0:
        return 0
    k = virtual_quote * virtual_token
    new_token = -(-k // (virtual_quote + quote_in))  # ceil: rounding favours the curve
    return max(virtual_token - new_token, 0)


def curve_sell_lamports(virtual_quote: int, virtual_token: int, token_in: int) -> int:
    """Lamports out of the curve for ``token_in`` raw tokens (fees not yet deducted)."""
    if token_in <= 0:
        return 0
    k = virtual_quote * virtual_token
    new_quote = -(-k // (virtual_token + token_in))
    return max(virtual_quote - new_quote, 0)


def curve_progress_pct(real_token: int) -> float:
    return max(0.0, min(100.0, (1 - real_token / INITIAL_REAL_TOKEN_RESERVES) * 100.0))


@dataclass(frozen=True)
class CurveBuy:
    tokens_raw: int
    curve_in: int  # lamports into the curve
    trade_fee: int  # protocol + creator fee, lamports
    network_fee: int  # base + priority fee, lamports
    total_cost: int  # == budget
    slippage: int  # lamports: curve_in minus tokens valued at the pre-trade spot price (attribution)


@dataclass(frozen=True)
class CurveSell:
    tokens_raw: int
    curve_out: int
    trade_fee: int
    network_fee: int
    proceeds: int  # curve_out - trade_fee - network_fee (may be <= 0 for dust)
    slippage: int  # tokens valued at spot minus curve_out (attribution)


def simulate_curve_buy(virtual_quote: int, virtual_token: int, budget: int, fee_bps: int,
                       network_fee: int) -> CurveBuy:
    """Spend exactly ``budget`` lamports: network fee, then curve input plus the
    percentage fee charged on top of it (as pump.fun does for buys)."""
    spendable = budget - network_fee
    if spendable <= 0:
        raise ValueError("budget does not cover network fees")
    curve_in = spendable * 10_000 // (10_000 + fee_bps)
    trade_fee = spendable - curve_in
    tokens = curve_buy_tokens(virtual_quote, virtual_token, curve_in)
    spot_value = tokens * virtual_quote / virtual_token
    return CurveBuy(tokens, curve_in, trade_fee, network_fee, budget, int(round(curve_in - spot_value)))


def simulate_curve_sell(virtual_quote: int, virtual_token: int, tokens: int, fee_bps: int,
                        network_fee: int) -> CurveSell:
    out = curve_sell_lamports(virtual_quote, virtual_token, tokens)
    fee = -(-out * fee_bps // 10_000)
    spot_value = tokens * virtual_quote / virtual_token
    return CurveSell(tokens, out, fee, network_fee, out - fee - network_fee, int(round(spot_value - out)))
