# Solana Meme-Coin Paper Trading Bot

> ## ⚠️ PAPER TRADING ONLY
> This project **simulates** trades with a **virtual A$50** balance. It never connects
> to a wallet, never handles private keys or seed phrases, never signs anything and never
> submits a transaction. There is **no live-trading toggle**, and the app refuses to start
> if wallet/live-trading environment variables (`PRIVATE_KEY`, `SEED_PHRASE`, `MNEMONIC`,
> `LIVE_TRADING`, …) are set. Nothing it produces is evidence of real-world profitability.

It watches newly visible Solana tokens through the documented public
[DEX Screener API](https://docs.dexscreener.com/api/reference), records every observation
in SQLite, evaluates each token against configurable risk and momentum filters, and
simulates entries and exits using executable prices, fees, slippage, latency and
failed fills. A Streamlit dashboard shows the virtual account.

**Passing the filters is not evidence that a token is safe, legitimate or sellable.**
DEX Screener data cannot detect honeypots, mint/freeze authority, rug pulls or wash
trading.

---

## Install on the VM (Ubuntu/Debian, Python 3.11+)

```bash
sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip git
git clone https://github.com/fredago-alt/Bettrpunters-site.git
cd Bettrpunters-site
git checkout claude/solana-meme-coin-bot-mr1o5c   # until merged to main
cd solana-paper-bot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # edit if you want to change any setting
.venv/bin/python -m paperbot check-config
.venv/bin/python -m paperbot init-db
.venv/bin/python -m pytest
```

## Run

```bash
# 1. One-off check that the live API data parses (no DB writes)
.venv/bin/python -m paperbot validate-ingestion

# 2. Live market data -> paper trades (Ctrl-C to stop; safe to restart, state is in SQLite)
.venv/bin/python -m paperbot scan
#    single cycle:      .venv/bin/python -m paperbot scan --once
#    in the background: nohup .venv/bin/python -m paperbot scan >> data/scan.log 2>&1 &
#    as a service:      see paperbot.service.example

# 3. Dashboard (in a second terminal)
.venv/bin/streamlit run dashboard/app.py --server.headless true --server.port 8501
#    in the background: nohup .venv/bin/streamlit run dashboard/app.py --server.headless true --server.port 8501 >> data/dashboard.log 2>&1 &
#    then open http://<vm-ip>:8501  (or tunnel: ssh -L 8501:localhost:8501 <vm>)

# 4. Reports / replay / out-of-sample
.venv/bin/python -m paperbot report                       # live run metrics
.venv/bin/python -m paperbot replay [--start ISO] [--end ISO] [--out data/replay.sqlite3]
.venv/bin/python -m paperbot split-test --fraction 0.5    # chronological in-/out-of-sample
.venv/bin/python -m paperbot scenarios                    # fee x slippage sensitivity grid
.venv/bin/python -m paperbot freeze --out frozen_rules.json
.venv/bin/python -m paperbot oos --frozen frozen_rules.json
```

### Auto-restart (recommended for multi-week runs)

```bash
bash scripts/install-autostart.sh          # once; safe to re-run
bash scripts/install-autostart.sh --remove # undo
```
This adds a cron job (at boot and every minute) and a desktop-login autostart entry that run
`scripts/keepalive.sh`, which starts the scanner and the dashboard if either is not running.
Restarts are logged to `data/keepalive.log`. If the machine itself is powered off, nothing
runs until it is started again; on restart the scanner refreshes open positions first.
Downtime still leaves gaps in the data, and a position held through a long outage exits at
whatever price is available on restart, or is written off if its pair is gone.

Port 8501 must be allowed by the VM firewall if you open the dashboard directly; an SSH
tunnel avoids exposing it. The dashboard opens the database read-only.

---

## How it works

| Module | Role |
|---|---|
| `paperbot/config.py` | Env-based settings (`PAPERBOT_*`), validation, rules fingerprint, labelled assumptions, refusal of wallet/live env vars |
| `paperbot/dexscreener.py` | GET-only API client: rate limiter (kept under 60/min and 300/min), retries with exponential backoff + jitter, `Retry-After` on 429, malformed-payload errors |
| `paperbot/fx.py` | USD↔AUD conversion with sanity bounds; live ECB daily rate (frankfurter.dev, cached) with labelled static fallback |
| `paperbot/costs.py` | Executable-price fills; fee and slippage each applied exactly once |
| `paperbot/filters.py` | Entry filters with a reason code for every rejection |
| `paperbot/engine.py` | Event-driven paper broker shared by live and replay |
| `paperbot/scanner.py` | Live loop: positions → discovery → tracking → latency fills → snapshot |
| `paperbot/replay.py` | Historical replay, chronological split, frozen-rule out-of-sample, cost scenarios |
| `paperbot/metrics.py` | Expectancy, profit factor, drawdown, average win/loss, fees, slippage, failed exits |
| `paperbot/migrations/` | Numbered SQL migrations applied by `init-db` (and automatically on start) |
| `dashboard/app.py` | Streamlit dashboard |

### Data recording
* Every token seen in a discovery feed goes into `discovery_events` and `tokens`.
* Every token is re-observed for `TRACK_WINDOW_MINUTES` after it is first seen,
  **whether or not it passed the filters and whether or not it is still in the feed**:
  every `TRACK_POLL_INTERVAL_SECONDS` (30 s) while its pair could still pass the age
  filter, and every `SLOW_TRACK_POLL_INTERVAL_SECONDS` (10 min) after that.
  If the API stops returning a pair, a `status='missing'` observation is written, so
  disappearances are part of the record.
* `observations` stores timestamp (UTC unix and ISO), price, liquidity, FDV, volume,
  buys/sells (5 m and 1 h), price change, pair creation time and the FX rate in force.
  SQLite triggers make `observations`, `discovery_events`, `filter_decisions` and
  `order_events` append-only: any UPDATE or DELETE aborts.
* Every filter evaluation is stored in `filter_decisions` with all rejection reasons.
* Open positions are refreshed **by pair address every cycle**, independently of the
  discovery feed and of the filters.

### Entry filters (initial hypotheses)
pair age 1–30 min · liquidity ≥ US$25,000 (missing liquidity = reject) · ≥ 10 txns in 5 min ·
buys/sells ≥ 1.2 in 5 min · zero sells in 5 min = reject (sellability unverified) · quote
token SOL or USDC · |5 m price change| ≤ 300% · price present · one position at a time ·
each token traded at most once per run.

### Execution model (all labelled assumptions)
* **Reference price** = DEX Screener `priceUsd` (last trade, not a quote).
* **Buy**: budget = min(10% of cash, A$5). Fee 1% is paid on top of notional, so
  notional = budget / 1.01. Executable price = ref × (1 + 1.5%). Quantity =
  notional (converted to USD) / executable price.
* **Sell**: executable price = ref × (1 − 1.5%); gross = qty × price (converted to AUD);
  fee 1% of gross deducted.
* **No double counting**: slippage only changes the execution price, fees only change
  cash. `slippage_aud` columns are attribution, not an extra charge. A unit test checks
  that a flat round trip loses exactly fee+fee+slippage+slippage for 25 cost combinations.
* **Exits** compare the *executable net liquidation value* (after exit slippage and fee)
  with the A$ cost: TP at +15%, SL at −10%, max hold 15 min. With default costs a
  ~5.13% reference move is needed just to break even, a ~+20.9% move to hit TP, and a
  ~−5.4% move hits SL.
* **Latency** 3 s: a fill uses the first observation at or after signal + 3 s (the live
  scanner sleeps and re-fetches). Entry is abandoned if the price drifted up > 10%.
* **Failed fills**: 5% random entry failures (abandon), 5% random exit failures (retry
  next observation), missing market data = failed exit. A position with no data for 60 min
  is written off at 0% recovery.
* **FX**: the rate is recorded on each observation and fill; replay uses the recorded rate.

### Replay and out-of-sample testing
* Replay feeds recorded observations strictly in time order to the same engine, into a
  separate (in-memory or `--out`) database; it never modifies the live data.
* **No look-ahead**: only the current observation is visible; fills use later prices;
  a test proves results for a window are identical whether or not later data exists.
* **No survivorship bias**: the universe is every recorded observation, including
  rejected tokens and tokens that vanished (written off under the stale rule).
* **Frozen rules**: `freeze` stores the rule fingerprint and a timestamp. `oos` refuses to
  run if any strategy rule changed and uses only data observed after the freeze. A live run
  also refuses to continue under a different rule set — use `scan --run-id <new>`.
* **Limitation**: DEX Screener's public API has no historical tick data, so replay can only
  use data this bot recorded, at its recording cadence (15 s for positions, 30 s for tracked
  tokens). Replay fills can therefore be later than the 3 s live latency.

---

## Evaluation protocol

1. `pytest` — all tests green.
2. `validate-ingestion` — field coverage looks sane.
3. Run `scan` continuously for **2–4 weeks** (systemd unit provided).
4. Aim for **≥ 200 closed simulated trades** before evaluating anything. The report and
   dashboard flag "INSUFFICIENT SAMPLE" below that.
5. Explore on the first part of the data (`split-test`, `scenarios`), decide on rules,
   then `freeze` them. If you change rules, start a new run id.
6. Keep collecting, then run `oos --frozen frozen_rules.json` on the post-freeze period
   only. Report net expectancy, profit factor, max drawdown, average win/loss, fees,
   slippage and failed exits — for both the in-sample and the frozen out-of-sample period,
   and under several fee/slippage scenarios.

**Throughput warning:** a short live smoke test saw ~24 new Solana profiles in 4 minutes,
and none passed the filters (most pairs were older than 30 min, had no reported liquidity,
or had a non-SOL/USDC quote token). 200 trades may take longer than 4 weeks with the default
feed. `PAPERBOT_DISCOVERY_FEEDS=TOKEN-PROFILES,TOKEN-BOOSTS` adds a second documented feed;
because that changes the token universe, start a new run id when you switch it.

## Tests

```bash
.venv/bin/python -m pytest
```

Covered: currency conversion (including the direction and sanity bounds), fees, slippage,
the no-double-counting identity across fee/slippage scenarios, executable-price fills,
take-profit / stop-loss / max-hold exits, latency, drift and failed fills, stale write-offs,
database migrations, immutability and persistence across restarts, API retry / 429 /
malformed-JSON handling, rate limiting, filters, scanner behaviour when discovery fails or
tokens disappear, replay look-ahead and survivorship, frozen rules, cost scenarios,
configuration validation, and paper-only guarantees (no wallet/signing imports or code,
GET-only HTTP, refusal of wallet/live env vars, no live setting, banner on the dashboard).
Replay tests use **synthetic** data that tests mechanics only.

## Storage

Each observation plus its filter decision and indexes takes roughly 1–2 KB
(`PAPERBOT_STORE_RAW_JSON=false`). Volume depends on how many new tokens appear, which
was not measured over a long period; check `du -h data/` after the first day.
