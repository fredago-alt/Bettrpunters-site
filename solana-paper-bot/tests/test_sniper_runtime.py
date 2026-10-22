import asyncio
import json

import pytest
import requests

from paperbot.config import ConfigError
from paperbot.sniper.config import SniperSettings
from paperbot.sniper.pumpfun import PUMP_PROGRAM
from paperbot.sniper.rpc import LogStream, ReadOnlyRpc, RpcError, redact
from paperbot.sniper.runner import SolPrice, run
from sniper_helpers import Curve, FixedSol, log_lines, make_sniper


class FakeWS:
    def __init__(self, messages, fail_after=None):
        self.messages = list(messages)
        self.sent = []
        self.fail_after = fail_after

    async def send(self, m):
        self.sent.append(json.loads(m))

    async def recv(self):
        if self.fail_after is not None and len(self.sent) and not self.messages:
            raise ConnectionError("dropped")
        if not self.messages:
            await asyncio.sleep(3600)
        return self.messages.pop(0)

    async def close(self):
        pass


def notif(logs, err=None, sig="sig"):
    return json.dumps({"jsonrpc": "2.0", "method": "logsNotification", "params": {
        "result": {"context": {"slot": 7}, "value": {"signature": sig, "err": err, "logs": logs}}, "subscription": 1}})


ACK = json.dumps({"jsonrpc": "2.0", "result": 1, "id": 1})


def test_stream_subscribes_read_only_and_skips_failed_txs():
    ws = FakeWS([ACK, notif(["a"], err={"x": 1}), notif(["b"], sig="good")])

    async def connect(url):
        return ws

    async def main():
        s = LogStream("wss://x", PUMP_PROGRAM, connect=connect)
        async for _, sig, slot, logs in s.stream():
            return sig, slot, logs

    sig, slot, logs = asyncio.run(main())
    assert (sig, slot, logs) == ("good", 7, ["b"])
    assert ws.sent[0]["method"] == "logsSubscribe"
    assert ws.sent[0]["params"][0] == {"mentions": [PUMP_PROGRAM]}


def test_stream_reconnects_after_drop(monkeypatch):
    sockets = [FakeWS([ACK], fail_after=0), FakeWS([ACK, notif(["ok"])])]

    async def connect(url):
        return sockets.pop(0)

    async def no_sleep(_):
        return None

    async def main():
        s = LogStream("wss://x", PUMP_PROGRAM, connect=connect)
        monkeypatch.setattr(asyncio, "sleep", no_sleep)
        async for _, _, _, logs in s.stream():
            return s.reconnects, logs

    reconnects, logs = asyncio.run(main())
    assert reconnects == 1 and logs == ["ok"]


def test_rpc_retries_then_decodes_accounts():
    import base64

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._b = code, body

        def json(self):
            return self._b

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

    class Sess:
        def __init__(self):
            self.calls = 0

        def post(self, url, json, timeout):
            self.calls += 1
            if self.calls == 1:
                return Resp(429, {})
            return Resp(200, {"result": {"value": [{"data": [base64.b64encode(b"abc").decode(), "base64"]}, None]}})

    rpc = ReadOnlyRpc("https://x", session=Sess(), sleep=lambda s: None)
    assert rpc.get_multiple_accounts(["A", "B"]) == {"A": b"abc", "B": None}

    class Dead:
        def post(self, *a, **k):
            raise requests.ConnectionError("down")

    with pytest.raises(RpcError):
        ReadOnlyRpc("https://x", session=Dead(), max_retries=1, sleep=lambda s: None).call("getSlot", [])


def test_redact_hides_api_keys():
    assert redact("https://mainnet.helius-rpc.com/?api-key=SECRET") == "https://mainnet.helius-rpc.com/?<redacted>"
    assert "SECRET" not in redact("wss://x/?api-key=SECRET")


def test_sniper_settings_env_and_validation():
    s = SniperSettings.from_env({"SNIPER_TAKE_PROFIT_PCT": "40", "SNIPER_LATENCY_MS": "250"})
    assert s.take_profit_pct == 40 and s.latency_ms == 250
    for bad in ({"SNIPER_RPC_WS_URL": "ws://insecure"}, {"SNIPER_STOP_LOSS_PCT": "5"},
                {"SNIPER_RUN_ID": "live"}, {"SNIPER_MAX_AGE_SECONDS": "1", "SNIPER_MIN_AGE_SECONDS": "2"}):
        with pytest.raises(ConfigError):
            SniperSettings.from_env(bad)
    assert SniperSettings().rules_fingerprint() == SniperSettings(latency_ms=1).rules_fingerprint()
    assert SniperSettings().rules_fingerprint() != SniperSettings(take_profit_pct=50).rules_fingerprint()


def test_sniper_env_example_documents_every_setting():
    from dataclasses import fields
    from pathlib import Path

    from dotenv import dotenv_values

    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    documented = {k[len("SNIPER_"):].lower() for k in env if k.startswith("SNIPER_") and k != "SNIPER_ENABLED"}
    assert documented == {f.name for f in fields(SniperSettings)}
    assert SniperSettings.from_env({k: v for k, v in env.items() if k.startswith("SNIPER_")}) == SniperSettings()


def test_sol_price_picks_deepest_stablecoin_pair_and_expires():
    from conftest import make_snap

    WSOL = "So11111111111111111111111111111111111111112"

    class Client:
        def pairs_for_tokens(self, tokens):
            return [make_snap(token=WSOL, pair="P1", price=151.0, liquidity=1e6, quote="USDC"),
                    make_snap(token=WSOL, pair="P2", price=149.0, liquidity=5e7, quote="USDC"),
                    make_snap(token=WSOL, pair="P3", price=999.0, liquidity=9e9, quote="BONK")]

    now = [0.0]
    sp = SolPrice(Client(), max_age_seconds=600, clock=lambda: now[0])
    assert sp.get() is None
    sp.refresh()
    assert sp.get()[0] == 149.0
    now[0] = 601
    assert sp.get() is None


def test_runner_end_to_end_with_fake_stream():
    eng, conn, _ = make_sniper(max_hold_seconds=0.5, latency_ms=0)
    c = Curve()
    batches = [log_lines([c.trade(f"b{i}", 0.5, raw=True)]) for i in range(4)]

    class FakeStream:
        connected, messages, reconnects = True, 0, 0

        async def stream(self):
            import time

            from paperbot.sniper.pumpfun import CREATE_DISC, CREATE_SCHEMA
            from sniper_helpers import encode
            create = encode(CREATE_DISC, CREATE_SCHEMA, {
                "name": "T", "symbol": "T", "uri": "", "mint": c.mint, "bonding_curve": c.curve, "user": c.creator,
                "creator": c.creator, "virtual_token_reserves": 1_073_000_000 * 10**6,
                "virtual_sol_reserves": 30 * 10**9, "virtual_quote_reserves": 30 * 10**9, "creator_fee_bps": 30})
            yield time.time(), "s0", 1, log_lines([create])
            for i, b in enumerate(batches):
                await asyncio.sleep(0.05)
                yield time.time(), f"s{i + 1}", 1, b
            await asyncio.sleep(3600)

    class FakeRpc:
        def get_multiple_accounts(self, addrs):
            return {a: None for a in addrs}

    s = eng.s
    asyncio.run(run(eng, s, FakeStream(), FakeRpc(), FixedSol(), max_seconds=1.5))
    row = conn.execute("SELECT status, exit_reason FROM positions").fetchone()
    assert tuple(row) == ("closed", "max_hold")
    assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] >= 1


def test_variant_env_overlays_sniper_settings_with_own_run_id(tmp_path):
    from paperbot.sniper.config import variant_env

    (tmp_path / "sl30.env").write_text("SNIPER_STOP_LOSS_PCT=-30\n")
    env = variant_env("sl30", {"SNIPER_TAKE_PROFIT_PCT": "40", "PAPERBOT_DB_PATH": "x"}, tmp_path)
    s = SniperSettings.from_env(env)
    assert (s.stop_loss_pct, s.take_profit_pct, s.run_id) == (-30.0, 40.0, "sniper-sl30")
    assert s.rules_fingerprint() != SniperSettings(take_profit_pct=40).rules_fingerprint()


def test_variant_env_rejects_bad_names_files_and_keys(tmp_path):
    from paperbot.sniper.config import variant_env

    (tmp_path / "ok.env").write_text("PAPERBOT_DB_PATH=/elsewhere\n")
    (tmp_path / "same.env").write_text("SNIPER_RUN_ID=sniper\n")
    for name in ("../etc", "UPPER", "missing", "ok", "same"):
        with pytest.raises(ConfigError):
            variant_env(name, {}, tmp_path)


def test_shipped_variant_files_are_valid():
    from pathlib import Path

    from paperbot.sniper.config import VARIANTS_DIR, variant_env

    for f in sorted(Path(VARIANTS_DIR).glob("*.env")):
        SniperSettings.from_env(variant_env(f.stem, {}))
