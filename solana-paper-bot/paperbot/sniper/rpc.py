"""Read-only Solana JSON-RPC and log-stream clients. PAPER TRADING ONLY.

Only the methods in the allow-lists below can be called; anything else raises
PaperOnlyViolation before a request is made. None of them can move funds.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import ssl
import time
from typing import AsyncIterator, Callable

import requests

log = logging.getLogger(__name__)

ALLOWED_HTTP_METHODS = frozenset({"getAccountInfo", "getMultipleAccounts", "getSlot", "getHealth", "getVersion"})
ALLOWED_WS_METHODS = frozenset({"logsSubscribe", "logsUnsubscribe"})


class PaperOnlyViolation(RuntimeError):
    pass


class RpcError(RuntimeError):
    pass


def redact(url: str) -> str:
    """Hide API keys in RPC URLs before logging."""
    head, _, query = url.partition("?")
    return head + ("?<redacted>" if query else "")


class ReadOnlyRpc:
    def __init__(self, url: str, timeout: float = 10.0, max_retries: int = 3,
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep):
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.sleep = sleep
        self._id = 0

    def call(self, method: str, params: list):
        if method not in ALLOWED_HTTP_METHODS:
            raise PaperOnlyViolation(f"RPC method {method!r} is not on the read-only allow-list")
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.post(self.url, json=body, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise RpcError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                data = resp.json()
                if "error" in data:
                    raise RpcError(str(data["error"])[:200])
                return data["result"]
            except (requests.RequestException, ValueError, RpcError) as exc:
                last = exc
                if attempt < self.max_retries:
                    self.sleep(0.5 * 2 ** attempt + random.uniform(0, 0.25))
        raise RpcError(f"{method} failed after {self.max_retries + 1} attempts: {last}")

    def get_multiple_accounts(self, addresses: list[str]) -> dict[str, bytes | None]:
        out: dict[str, bytes | None] = {}
        for i in range(0, len(addresses), 100):
            batch = addresses[i:i + 100]
            res = self.call("getMultipleAccounts", [batch, {"encoding": "base64", "commitment": "processed"}])
            for addr, acc in zip(batch, (res or {}).get("value") or []):
                out[addr] = base64.b64decode(acc["data"][0]) if acc else None
        return out


class LogStream:
    """``logsSubscribe`` for one program, yielding (received_at, signature, slot, logs).

    Reconnects with exponential backoff, and also when no message arrives for
    ``idle_seconds`` (a silent socket is treated as dead). Failed transactions
    are skipped.
    """

    def __init__(self, ws_url: str, program: str, idle_seconds: float = 60.0, commitment: str = "processed",
                 connect=None, clock: Callable[[], float] = time.time):
        self.ws_url = ws_url
        self.program = program
        self.idle_seconds = idle_seconds
        self.commitment = commitment
        self._connect = connect
        self.clock = clock
        self.connected = False
        self.reconnects = 0
        self.messages = 0

    def subscribe_request(self) -> str:
        method = "logsSubscribe"
        assert method in ALLOWED_WS_METHODS
        return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": [{"mentions": [self.program]}, {"commitment": self.commitment}]})

    async def _open(self):
        if self._connect is not None:
            return await self._connect(self.ws_url)
        import websockets

        ctx = ssl.create_default_context()
        return await websockets.connect(self.ws_url, ssl=ctx, max_size=2 ** 23, ping_interval=20, ping_timeout=20)

    async def stream(self) -> AsyncIterator[tuple[float, str, int, list[str]]]:
        backoff = 1.0
        while True:
            ws = None
            try:
                ws = await self._open()
                await ws.send(self.subscribe_request())
                ack = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if "error" in ack:
                    raise RpcError(f"subscribe rejected: {ack['error']}")
                self.connected = True
                log.info("log stream connected to %s", redact(self.ws_url))
                backoff = 1.0
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), self.idle_seconds))
                    value = (((msg.get("params") or {}).get("result") or {}).get("value")) or {}
                    if not value or value.get("err") is not None:
                        continue
                    self.messages += 1
                    slot = (((msg.get("params") or {}).get("result") or {}).get("context") or {}).get("slot", 0)
                    yield self.clock(), value.get("signature", ""), slot, value.get("logs") or []
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network, protocol, idle timeout
                self.connected = False
                self.reconnects += 1
                log.warning("log stream error (%s: %s); reconnecting in %.0fs", type(exc).__name__, exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
