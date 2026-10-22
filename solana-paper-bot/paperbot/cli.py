"""Command line entry point: ``python -m paperbot <command>``. PAPER TRADING ONLY."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

from . import BANNER
from .config import ConfigError, load_settings
from .costs import CostModel, round_trip_breakeven_move
from .db import connect, current_version, ensure_run, migrate, open_db
from .dexscreener import ApiError, DexScreenerClient, best_pair_per_token
from .engine import PaperEngine
from .filters import evaluate
from .fx import make_fx
from .metrics import compare_runs, compute, format_comparison, format_report
from .replay import cost_scenarios, freeze_rules, observation_span, replay, replay_out_of_sample, time_split
from .scanner import Scanner



def _ts(value: str | None) -> float | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _fmt(ts: float | None) -> str:
    return "-" if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def cmd_check_config(s, args) -> int:
    print(json.dumps({k: (list(v) if isinstance(v, tuple) else v) for k, v in vars(s).items()}, indent=2))
    print(f"\nRules fingerprint: {s.rules_fingerprint()}")
    print(f"Round-trip break-even reference move: {round_trip_breakeven_move(CostModel.from_settings(s)):.2f}%")
    print("\nLabelled assumptions:")
    for k, v in s.assumptions().items():
        print(f"  - {k}: {v}")
    return 0


def cmd_init_db(s, args) -> int:
    conn = connect(s.db_path)
    applied = migrate(conn)
    print(f"{s.db_path}: schema version {current_version(conn)}; applied {applied or 'nothing (up to date)'}")
    return 0


def cmd_scan(s, args) -> int:
    conn = open_db(s.db_path)
    run_id = args.run_id or s.run_id
    ensure_run(conn, run_id, "live", s)
    engine = PaperEngine(conn, run_id, s, make_fx(s))
    scanner = Scanner(conn, s, DexScreenerClient.from_settings(s), engine)
    cycles = 1 if args.once else args.cycles
    scanner.run_forever(max_cycles=cycles)
    print(format_report(compute(conn, run_id), "Live paper run"))
    return 0


def cmd_validate_ingestion(s, args) -> int:
    """One-off read of the live API: checks payload shape and reports field coverage. No DB writes."""
    client = DexScreenerClient.from_settings(s)
    profiles = client.latest_token_profiles("solana")
    print(f"token-profiles/latest: {len(profiles)} Solana profiles")
    tokens = [p["tokenAddress"] for p in profiles]
    pairs = client.pairs_for_tokens(tokens) if tokens else []
    best = best_pair_per_token(pairs)
    print(f"tokens/v1: {len(pairs)} pairs for {len(best)}/{len(tokens)} tokens ({len(tokens) - len(best)} returned no pair)")
    fields = ["price_usd", "liquidity_usd", "pair_created_at", "buys_m5", "sells_m5", "volume_m5_usd", "quote_symbol"]
    for f in fields:
        have = sum(1 for p in best.values() if getattr(p, f) is not None)
        print(f"  {f:<16} present for {have}/{len(best)}")
    now = time.time()
    reasons: dict[str, int] = {}
    passed = 0
    for p in best.values():
        r = evaluate(p, now, s)
        passed += r.passed
        for code in r.reasons:
            reasons[code.split(":")[0]] = reasons.get(code.split(":")[0], 0) + 1
    print(f"filters: {passed} passed, rejection counts {dict(sorted(reasons.items(), key=lambda kv: -kv[1]))}")
    fx = make_fx(s).get()
    print(f"FX: {fx.aud_per_usd} AUD per USD (source {fx.source})")
    print(f"API stats: {client.stats}")
    return 0


def cmd_report(s, args) -> int:
    conn = open_db(args.db or s.db_path)
    print(format_report(compute(conn, args.run_id or s.run_id), "Report"))
    return 0


def _print_replay(res, title):
    print(f"\n{title}: {res.observations} observations {_fmt(res.first_obs_at)} -> {_fmt(res.last_obs_at)}; "
          f"{res.open_at_end} position(s) still open at period end (not counted as closed trades)")
    print(format_report(res.metrics, title))


def cmd_replay(s, args) -> int:
    src = open_db(args.db or s.db_path)
    res = replay(src, s, _ts(args.start), _ts(args.end), out_path=args.out or ":memory:")
    _print_replay(res, "Replay")
    return 0


def cmd_split_test(s, args) -> int:
    src = open_db(args.db or s.db_path)
    lo, hi, n = observation_span(src)
    split = _ts(args.split_at) if args.split_at else time_split(src, args.fraction)
    print(f"Recorded data {_fmt(lo)} -> {_fmt(hi)} ({n} observations); chronological split at {_fmt(split)}")
    print(f"Rules fingerprint {s.rules_fingerprint()} (identical for both periods)")
    _print_replay(replay(src, s, None, split, label="in-sample"), "IN-SAMPLE")
    _print_replay(replay(src, s, split, None, label="out-of-sample"), "OUT-OF-SAMPLE")
    return 0


def cmd_compare(s, args) -> int:
    conn = open_db(args.db or s.db_path)
    print(format_comparison(compare_runs(conn, args.runs.split(",") if args.runs else None)))
    return 0


def cmd_research(s, args) -> int:
    from .research import Costs, launch_ceiling, run_research, slot_ceiling

    conn = open_db(args.db or s.db_path)
    if args.slots:
        print("PAPER TRADING ONLY - exploratory research (hypotheses, not proof).")
        print(slot_ceiling(conn, s.fx_static_aud_per_usd))
        return 0
    if args.launch:
        print("PAPER TRADING ONLY - exploratory research (hypotheses, not proof).")
        print(launch_ceiling(conn, args.size_aud, s.fx_static_aud_per_usd))
        return 0
    costs = Costs(position_aud=args.size_aud, aud_per_usd=s.fx_static_aud_per_usd,
                  network_lamports=5_000 + args.priority_lamports)
    print(run_research(conn, costs))
    return 0


def cmd_freeze(s, args) -> int:
    doc = freeze_rules(s, args.out)
    print(f"Rules frozen to {args.out} at {_fmt(doc['frozen_at'])} (fingerprint {doc['rules_fingerprint']}).")
    print("Keep collecting live data; then run `python -m paperbot oos --frozen", args.out + "`.")
    return 0


def cmd_oos(s, args) -> int:
    src = open_db(args.db or s.db_path)
    res = replay_out_of_sample(src, s, args.frozen, out_path=args.out or ":memory:")
    _print_replay(res, "FROZEN-RULES OUT-OF-SAMPLE")
    return 0


def cmd_scenarios(s, args) -> int:
    src = open_db(args.db or s.db_path)
    fees = [float(x) for x in args.fees.split(",")]
    slips = [float(x) for x in args.slippages.split(",")]
    rows = cost_scenarios(src, s, _ts(args.start), _ts(args.end), fees, slips)
    print("PAPER TRADING ONLY - cost sensitivity on recorded data (fee/slippage are per side, %)")
    print(f"{'fee':>6} {'slip':>6} {'trades':>7} {'win%':>7} {'net A$':>10} {'exp A$':>9} {'PF':>7} {'maxDD A$':>9}")
    for r in rows:
        win = "n/a" if r["win_rate"] is None else f"{r['win_rate']:.1%}"
        exp = "n/a" if r["expectancy_aud"] is None else f"{r['expectancy_aud']:.4f}"
        pf = "n/a" if r["profit_factor"] is None else f"{r['profit_factor']:.2f}"
        print(f"{r['fee_pct']:>6} {r['slippage_pct']:>6} {r['trades']:>7} {win:>7} {r['net_pnl_aud']:>10.4f} "
              f"{exp:>9} {pf:>7} {r['max_dd_aud']:>9.4f}")
    return 0


def _sniper_settings(variant: str | None = None):
    import os

    from .sniper.config import SniperSettings, variant_env

    return SniperSettings.from_env(variant_env(variant, os.environ) if variant else None)


def cmd_sniper(s, args) -> int:
    import asyncio
    import random

    from .sniper.engine import SniperEngine
    from .sniper.pumpfun import PUMP_PROGRAM
    from .sniper.rpc import LogStream, ReadOnlyRpc, redact
    from .sniper.runner import SolPrice, run

    ss = _sniper_settings(args.variant)
    conn = open_db(s.db_path)
    ensure_run(conn, ss.run_id, "live", ss)
    sol = SolPrice(DexScreenerClient.from_settings(s))
    engine = SniperEngine(conn, ss, make_fx(s), sol, rng=random.Random(ss.random_seed),
                          record_market_data=args.variant is None)
    print(f"pump.fun sniper SIMULATION run={ss.run_id} rules={ss.rules_fingerprint()} rpc={redact(ss.rpc_ws_url)}")
    try:
        asyncio.run(run(engine, ss, LogStream(ss.rpc_ws_url, PUMP_PROGRAM, ss.stream_idle_reconnect_seconds),
                        ReadOnlyRpc(ss.rpc_http_url), sol, max_seconds=args.seconds))
    except KeyboardInterrupt:
        pass
    print(format_report(compute(conn, ss.run_id), "Sniper paper run"))
    return 0


def cmd_sniper_check(s, args) -> int:
    import asyncio

    from .sniper.runner import check_stream

    ss = _sniper_settings()
    counts = asyncio.run(check_stream(ss, args.seconds))
    print(json.dumps(counts, indent=2))
    if counts["messages"] == 0:
        print("No messages received: check SNIPER_RPC_WS_URL / network. The public endpoint may refuse "
              "or throttle subscriptions; a free Helius or QuickNode key usually fixes this.")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="paperbot", description=BANNER)
    p.add_argument("--env-file", default=".env")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config", help="validate and print configuration and assumptions")
    sub.add_parser("init-db", help="create/migrate the SQLite database")
    sp = sub.add_parser("scan", help="live market data -> paper trades")
    sp.add_argument("--once", action="store_true", help="run a single cycle")
    sp.add_argument("--cycles", type=int, default=None)
    sp.add_argument("--run-id", default=None, help="default: PAPERBOT_RUN_ID")
    sub.add_parser("validate-ingestion", help="one-off API fetch and field coverage check (no DB writes)")
    for name, help_ in (("report", "metrics for a run"), ("replay", "replay recorded observations"),
                        ("split-test", "chronological in-sample / out-of-sample replay"),
                        ("oos", "out-of-sample replay with frozen rules"), ("scenarios", "fee/slippage grid")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--db", default=None, help="source database (default PAPERBOT_DB_PATH)")
        if name == "report":
            sp.add_argument("--run-id", default=None, help="default: PAPERBOT_RUN_ID")
        if name in ("replay", "scenarios"):
            sp.add_argument("--start", help="ISO time, UTC if no offset")
            sp.add_argument("--end")
        if name in ("replay", "oos"):
            sp.add_argument("--out", help="write replay run to this SQLite file (view it in the dashboard)")
        if name == "split-test":
            sp.add_argument("--fraction", type=float, default=0.5)
            sp.add_argument("--split-at")
        if name == "oos":
            sp.add_argument("--frozen", required=True)
        if name == "scenarios":
            sp.add_argument("--fees", default="0.25,0.5,1.0,2.0")
            sp.add_argument("--slippages", default="0.5,1.5,3.0,5.0")
    sp = sub.add_parser("sniper", help="pump.fun on-chain sniper SIMULATION (paper only)")
    sp.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    sp.add_argument("--variant", help="run a side-by-side test variant from variants/<name>.env")
    sp = sub.add_parser("sniper-check", help="connect to the Solana log stream and count decoded events")
    sp.add_argument("--seconds", type=float, default=30.0)
    sp = sub.add_parser("compare", help="side-by-side evaluation of all live paper runs")
    sp.add_argument("--db", default=None)
    sp.add_argument("--runs", help="comma-separated run ids (default: all live runs)")
    sp = sub.add_parser("research", help="exploratory analysis of recorded pump.fun launches")
    sp.add_argument("--db", default=None)
    sp.add_argument("--size-aud", type=float, default=5.0)
    sp.add_argument("--priority-lamports", type=int, default=100_000)
    sp.add_argument("--launch", action="store_true", help="only the first-buyer launch-ceiling test")
    sp.add_argument("--slots", action="store_true", help="slot-by-slot entry test (needs recorded early trades)")
    sp = sub.add_parser("freeze", help="freeze current strategy rules for out-of-sample testing")
    sp.add_argument("--out", default="frozen_rules.json")
    return p


COMMANDS = {
    "check-config": cmd_check_config, "init-db": cmd_init_db, "scan": cmd_scan,
    "validate-ingestion": cmd_validate_ingestion, "report": cmd_report, "replay": cmd_replay,
    "split-test": cmd_split_test, "compare": cmd_compare, "research": cmd_research, "freeze": cmd_freeze, "oos": cmd_oos, "scenarios": cmd_scenarios,
    "sniper": cmd_sniper, "sniper-check": cmd_sniper_check,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        s = load_settings(args.env_file)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=s.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(BANNER)
    try:
        return COMMANDS[args.cmd](s, args)
    except ApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        return 3
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
