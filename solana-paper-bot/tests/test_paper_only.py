"""Guarantees that this project can only ever paper trade."""

import ast
import re
from dataclasses import fields
from pathlib import Path

import pytest

import paperbot
from paperbot.config import FORBIDDEN_ENV_VARS, ConfigError, Settings

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "paperbot").rglob("*.py")) + sorted((ROOT / "dashboard").rglob("*.py"))

# Wallet / signing / RPC-submission libraries that must never be imported.
FORBIDDEN_MODULES = {"solana", "solders", "anchorpy", "web3", "eth_account", "nacl", "base58", "bip_utils",
                     "mnemonic", "jupiter_python_sdk", "spl", "ecdsa", "cryptography"}
FORBIDDEN_PATTERNS = [
    r"sendTransaction", r"send_transaction", r"signTransaction", r"sign_transaction", r"Keypair",
    r"private_?key", r"seed_?phrase", r"mnemonic", r"/swap\b", r"quote-api\.jup", r"\.post\(", r"\.put\(",
]


def test_sources_found():
    assert len(SOURCES) >= 10


def test_paper_only_flag_and_banner():
    assert paperbot.PAPER_TRADING_ONLY is True
    assert "PAPER TRADING ONLY" in paperbot.BANNER


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_wallet_or_signing_imports(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        for n in names:
            assert n.split(".")[0] not in FORBIDDEN_MODULES, f"{path.name} imports {n}"


@pytest.mark.parametrize("path", [p for p in SOURCES if p.name != "config.py"], ids=lambda p: p.name)
def test_no_transaction_code_patterns(path):
    text = path.read_text()
    for pat in FORBIDDEN_PATTERNS:
        assert not re.search(pat, text, re.IGNORECASE), f"{path.name} matches forbidden pattern {pat}"


def test_requirements_have_no_wallet_libraries():
    reqs = (ROOT / "requirements.txt").read_text().lower()
    for mod in FORBIDDEN_MODULES:
        assert not re.search(rf"^{mod}\b", reqs, re.MULTILINE)


def test_no_live_trading_setting_exists():
    names = {f.name for f in fields(Settings)}
    for bad in ("live_trading", "enable_live", "wallet", "private_key", "seed_phrase", "rpc_url", "dry_run", "paper"):
        assert not any(bad in n for n in names), bad


@pytest.mark.parametrize("var", FORBIDDEN_ENV_VARS)
def test_refuses_to_start_with_wallet_or_live_env(var):
    with pytest.raises(ConfigError, match="PAPER TRADING ONLY"):
        Settings.from_env({var: "anything"})


def test_http_client_only_issues_get_requests():
    src = (ROOT / "paperbot" / "dexscreener.py").read_text()
    calls = re.findall(r"self\.session\.(\w+)\(", src)
    assert calls and set(calls) == {"get"}


def test_dashboard_shows_paper_banner():
    src = (ROOT / "dashboard" / "app.py").read_text()
    assert "BANNER" in src and "PAPER TRADING ONLY" in src
