import random

from paperbot.research import _wallet_token_results, wallet_research
from sniper_helpers import T0, Curve, make_sniper, pk


def _world(n_tokens=240, seed=5):
    """Synthetic launches: 'SMART' wallets buy first on tokens that then pump and sell
    into the pump; random wallets buy late. Half the tokens pump, half die."""
    eng, conn, _ = make_sniper(min_buys=10**6)  # never trade, just record
    rng = random.Random(seed)
    t = T0
    for n in range(n_tokens):
        c = Curve(f"W{n}")
        base = 10_000 * (n + 1)
        eng.on_create(c.create_event(), t, f"C{n}", base)
        pumps = n % 2 == 0
        slot = base + 1
        if pumps:
            for s in range(3):
                eng.on_trade(c.trade(f"SMART{s}", 0.5), t + 0.4, slot=slot, signature=f"a{n}{s}")
            for k in range(25):
                slot += 1
                eng.on_trade(c.trade(f"crowd{rng.randrange(500)}", 1.0), t + 1 + k * 0.4, slot=slot,
                             signature=f"b{n}{k}")
            for s in range(3):
                held = 0
                # sell everything the smart wallet bought
                held = sum(r[0] for r in conn.execute(
                    "SELECT token_amount FROM pump_early_trades WHERE mint=? AND user=? AND is_buy=1",
                    (c.mint, pk(f"SMART{s}"))))
                slot += 1
                eng.on_trade(c.trade(f"SMART{s}", tokens=held, is_buy=False), t + 15, slot=slot,
                             signature=f"c{n}{s}")
        else:
            for k in range(6):
                slot += 1
                eng.on_trade(c.trade(f"crowd{rng.randrange(500)}", 0.3), t + 1 + k, slot=slot, signature=f"d{n}{k}")
        t += 30
    return conn


def test_wallet_results_pnl_signs():
    c = Curve()
    ev_b = c.trade("A", 1.0)
    ev_up = c.trade("B", 5.0)
    rows = [
        {"by_creator": 0, "user": "A", "is_buy": 1, "quote_amount": ev_b.quote_amount, "token_amount": ev_b.token_amount,
         "slot": 1},
        {"by_creator": 0, "user": "B", "is_buy": 1, "quote_amount": ev_up.quote_amount,
         "token_amount": ev_up.token_amount, "slot": 2},
    ]
    res = _wallet_token_results(rows, (c.vq, c.vt))
    assert res["A"]["pnl"] > 0 and res["B"]["pnl"] < 0  # first buyer gains from the later buyer's pump


def test_finds_persistent_wallets_and_copy_delay_matters():
    conn = _world()
    text = wallet_research(conn, 1.42)
    assert "COPY-TRADING" in text
    assert "SMART" not in text  # addresses are shown, not names
    smart = {pk(f"SMART{s}")[:6] for s in range(3)}
    top_lines = [ln for ln in text.splitlines() if ".." in ln and "->" in ln]
    assert {ln.split("..")[0].strip() for ln in top_lines[:3]} == smart
    copy = next(ln for ln in text.splitlines() if ln.strip().startswith("A$50:"))
    means = [float(p.split("mean")[1].split("%")[0]) for p in copy.split("|")]
    assert means[0] > means[-1]  # copying 1 slot late beats 10 slots late


def test_needs_enough_data():
    eng, conn, _ = make_sniper()
    assert "not enough data" in wallet_research(conn, 1.42)

