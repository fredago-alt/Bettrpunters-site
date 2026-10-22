"""Async runner for the pump.fun sniper simulator. PAPER TRADING ONLY.

Tasks:
  stream   - pump.fun logs via logsSubscribe -> decoded events -> engine
  ticker   - every 250 ms: latency fills, max-hold exits, bar flushes
  refresh  - every N s: re-read held tokens' bonding-curve accounts over RPC,
             so open positions update even if the stream stalls
  sol      - SOL/USD price refresh (DEX Screener SOL/stablecoin pairs)
  status   - log a one-line summary and write an equity snapshot every 60 s
"""

from __future__ import annotations

import asyncio
import logging
import time

from .. import BANNER
from ..dexscreener import ApiError, DexScreenerClient
from .engine import SniperEngine
from .pumpfun import PUMP_PROGRAM, CompleteEvent, CreateEvent, TradeEvent, decode_curve_account, events_from_logs
from .rpc import LogStream, ReadOnlyRpc, RpcError

log = logging.getLogger(__name__)

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"USDC", "USDT"}


class SolPrice:
    """USD per SOL from the deepest SOL/USDC or SOL/USDT pair on DEX Screener."""

    def __init__(self, client: DexScreenerClient, max_age_seconds: float = 600.0, clock=time.time):
        self.client = client
        self.max_age = max_age_seconds
        self.clock = clock
        self._value: tuple[float, str] | None = None
        self._at = 0.0

    def refresh(self) -> None:
        pairs = [p for p in self.client.pairs_for_tokens([WSOL]) if p.token_address == WSOL
                 and p.quote_symbol in STABLES and p.price_usd and p.liquidity_usd]
        if not pairs:
            raise ApiError("no SOL/stablecoin pair returned")
        best = max(pairs, key=lambda p: p.liquidity_usd)
        if not 5 <= best.price_usd <= 5000:
            raise ApiError(f"SOL price {best.price_usd} outside sanity bounds")
        self._value = (best.price_usd, f"dexscreener:{best.dex_id}:{best.quote_symbol}")
        self._at = self.clock()

    def get(self) -> tuple[float, str] | None:
        if self._value is None or self.clock() - self._at > self.max_age:
            return None
        return self._value


async def run(engine: SniperEngine, s, stream: LogStream, rpc: ReadOnlyRpc, sol: SolPrice,
              max_seconds: float | None = None) -> None:
    log.warning(BANNER)
    loop = asyncio.get_running_loop()
    started = time.time()

    async def sol_task():
        while True:
            try:
                await loop.run_in_executor(None, sol.refresh)
            except Exception as exc:
                log.warning("SOL price refresh failed: %s", exc)
            await asyncio.sleep(s.sol_price_refresh_seconds)

    async def stream_task():
        async for received_at, sig, slot, logs in stream.stream():
            for ev in events_from_logs(logs):
                try:
                    if isinstance(ev, CreateEvent):
                        engine.on_create(ev, received_at, sig, slot)
                    elif isinstance(ev, TradeEvent):
                        engine.on_trade(ev, received_at, slot, sig)
                    elif isinstance(ev, CompleteEvent):
                        engine.on_complete(ev, received_at)
                except Exception:
                    log.exception("engine error on %s", type(ev).__name__)

    async def ticker():
        while True:
            try:
                engine.tick(time.time())
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(0.25)

    async def refresh_task():
        while True:
            held = engine.held_curves()
            if held:
                try:
                    accounts = await loop.run_in_executor(None, rpc.get_multiple_accounts, list(held))
                    now = time.time()
                    for curve, data in accounts.items():
                        engine.on_curve_account(held[curve], decode_curve_account(data) if data else None, now)
                except RpcError as exc:
                    log.warning("bonding-curve refresh failed: %s", exc)
                except Exception:
                    log.exception("bonding-curve refresh crashed")
            await asyncio.sleep(s.account_refresh_seconds)

    async def status_task():
        while True:
            await asyncio.sleep(60)
            try:
                engine.snapshot(time.time())
                log.info("sniper: %s stream_connected=%s msgs=%d reconnects=%d sol=%s", engine.summary(),
                         stream.connected, stream.messages, stream.reconnects, sol.get())
            except Exception:
                log.exception("status failed")

    tasks = [asyncio.create_task(t()) for t in (sol_task, stream_task, ticker, refresh_task, status_task)]
    try:
        if max_seconds is None:
            await asyncio.gather(*tasks)
        else:
            await asyncio.sleep(max(0.0, max_seconds - (time.time() - started)))
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        engine.snapshot(time.time())


async def check_stream(s, seconds: float) -> dict:
    """Connect, decode for ``seconds`` and report counts (no trading, no DB)."""
    stream = LogStream(s.rpc_ws_url, PUMP_PROGRAM, idle_seconds=min(s.stream_idle_reconnect_seconds, seconds))
    counts = {"messages": 0, "TradeEvent": 0, "CreateEvent": 0, "CompleteEvent": 0, "sol_standard_trades": 0,
              "mayhem_trades": 0, "non_sol_trades": 0}

    async def consume():
        async for _, _, _, logs in stream.stream():
            counts["messages"] += 1
            for ev in events_from_logs(logs):
                counts[type(ev).__name__] += 1
                if isinstance(ev, TradeEvent):
                    key = "mayhem_trades" if ev.mayhem else "sol_standard_trades" if ev.is_sol_quoted else "non_sol_trades"
                    counts[key] += 1

    task = asyncio.create_task(consume())
    await asyncio.sleep(seconds)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    counts["reconnects"] = stream.reconnects
    return counts
