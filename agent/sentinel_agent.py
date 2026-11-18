#!/usr/bin/env python3
"""
Sentinel — off-chain AI risk agent (stage 2 of the Sentinel loop).

Role in the system (see ../PLAN.md section 3 and ../BUILD_NOTES.md):

  1. DETECT   SentinelReactive (Lasna, Reactive Network) watches Swap events
              of a demo DEX pool on Base Sepolia. For every swap it computes a
              coarse on-chain signal (price deviation, running swap count)
              and emits `SignalRaised` on Lasna, plus a callback that stores
              the signal in RiskGate on Base Sepolia.
  2. DECIDE   THIS AGENT. It polls Lasna for `SignalRaised`, enriches each
              signal with short context from Base Sepolia (recent Swap
              activity of the same pool), scores the risk with the small,
              fully transparent rule/statistical model below, maps the score
              to a protective action, and writes the decision on-chain via
              `RiskGate.submitDecision(pool, score, action)`. Only the
              registered agent address may call that function.
  3. EXECUTE  RiskGate emits `DecisionMade`; SentinelReactive hears it and
              callbacks `RiskGate.executeAction`, which runs the protective
              action on DemoVault (pause / tightenStop / exitPosition).

The agent is a real runtime component, not a build tool: its decisions are
what move the vault. Every decision (inputs, score, action, one-line reason,
tx hash) is logged as a JSON line to stdout and to decisions.log so judges
can replay the agent's reasoning end to end.

Scoring model (no black box — see score_signal() for the commented code):
  score = deviation_points (0..50) + frequency_points (0..30)
        + volume_points (0..20), clamped to 0..100.
Action mapping (fixed, shared with the contracts):
  score < 40 -> 0 none | 40..59 -> 1 pause | 60..79 -> 2 tightenStop
  score >= 80 -> 3 exitPosition.

Usage:
  python3 sentinel_agent.py --self-test
  python3 sentinel_agent.py --dry-run --fake-signal 900,8,250000
  python3 sentinel_agent.py --reactive-address 0x... --riskgate-address 0x...
  python3 sentinel_agent.py --reactive-address 0x... --dry-run   (listen only)

The signing key is read at runtime from the farming testnet wallet file
(default ~/workspace/tzero-farm/wallet.json). The key value is never printed,
logged, or copied anywhere; it exists only in memory while signing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants (chain ids and defaults verified in ../docs-verification.md)
# ---------------------------------------------------------------------------

LASNA_CHAIN_ID = 5318007
BASE_SEPOLIA_CHAIN_ID = 84532

DEFAULT_LASNA_RPC = "https://lasna-rpc.rnk.dev/"
DEFAULT_BASE_RPC = "https://sepolia.base.org"

DEFAULT_WALLET_PATH = os.path.expanduser("~/workspace/tzero-farm/wallet.json")
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_PATH = os.path.join(AGENT_DIR, "decisions.log")
STATE_PATH = os.path.join(AGENT_DIR, "agent_state.json")

# Event signatures (canonical). SignalRaised is emitted by SentinelReactive
# on Lasna; Swap is emitted by MockPool on Base Sepolia.
SIGNAL_RAISED_SIG = "SignalRaised(address,int256,uint256)"
SWAP_SIG = "Swap(address,int256,uint256)"

# How many recent Base Sepolia blocks to scan for pool context, and the max
# block span per get_logs call while polling Lasna (public RPCs cap ranges).
CONTEXT_BLOCK_SPAN = 200  # publicnode caps eth_getLogs at a 200-block range
POLL_BLOCK_SPAN = 1000

# Action codes — must match RiskGate/DemoVault (see BUILD_NOTES.md).
ACTION_NAMES = {0: "none", 1: "pause", 2: "tightenStop", 3: "exitPosition"}

# Minimal ABI: only what the agent calls.
RISKGATE_ABI = [
    {
        "type": "function",
        "name": "submitDecision",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "pool", "type": "address"},
            {"name": "score", "type": "uint256"},
            {"name": "action", "type": "uint8"},
        ],
        "outputs": [],
    }
]

# Candidate field names for the private key inside the wallet JSON file.
KEY_FIELD_CANDIDATES = ("private_key", "privateKey", "key")


# ---------------------------------------------------------------------------
# Scoring model — transparent rules, no black box.
#
# The model is a weighted sum of three sub-scores, each saturating at a cap.
# Weights reflect how a human risk watcher triages a DEX pool:
#   * Price deviation is the strongest dump signal        -> up to 50 points.
#   * Swap frequency (burst activity) amplifies it        -> up to 30 points.
#   * Volume adds conviction (a dump needs size)          -> up to 20 points.
# Saturation points are chosen from the demo pool's scale: a 10% single-swap
# deviation (1000 bps), a burst of 10 swaps, or ~1e6 units of recent volume
# each max out their component. Everything is linear/log below the caps, so
# any score can be re-derived by hand from the logged inputs.
# ---------------------------------------------------------------------------

def score_signal(deviation_bps: int, frequency: int, volume: int) -> tuple[int, str]:
    """Score one signal. Returns (score 0..100, one-line human reason)."""
    deviation_bps = abs(int(deviation_bps))
    frequency = max(0, int(frequency))
    volume = max(0, int(volume))

    # Deviation: 0.05 points per bp -> 200 bps (2%) = 10, 1000 bps (10%) = 50.
    deviation_points = min(50.0, deviation_bps * 0.05)
    # Frequency: 3 points per swap in the window -> 10 swaps = 30 (cap).
    frequency_points = min(30.0, frequency * 3.0)
    # Volume: log-scaled so dust and whale flow are distinguishable without
    # one huge trade dominating: log10(1+volume)/6 of the 20-point cap,
    # saturating at volume = 1e6.
    volume_points = min(20.0, 20.0 * math.log10(1 + volume) / 6.0)

    score = int(round(deviation_points + frequency_points + volume_points))
    score = max(0, min(100, score))

    reason = (
        f"deviation {deviation_bps}bps -> {deviation_points:.1f}pts; "
        f"frequency {frequency} swaps -> {frequency_points:.1f}pts; "
        f"volume {volume} -> {volume_points:.1f}pts; total {score}/100"
    )
    return score, reason


def action_for_score(score: int) -> int:
    """Fixed score -> action mapping shared with the contracts."""
    if score < 40:
        return 0  # none: noise, leave the position alone
    if score < 60:
        return 1  # pause: suspicious, freeze the vault first
    if score < 80:
        return 2  # tightenStop: strong pattern, pull the stop in
    return 3      # exitPosition: dump in progress, get out


# ---------------------------------------------------------------------------
# Logging — every decision as one JSON line to stdout AND decisions.log.
# ---------------------------------------------------------------------------

def log_decision(record: dict, log_path: str) -> None:
    line = json.dumps(record)
    print(line, flush=True)
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:  # logging must never kill the agent loop
        print(f"[warn] could not write decision log: {exc}", file=sys.stderr)


def make_record(pool, deviation_bps, frequency, volume, score, action,
                reason, tx_repr, context=None) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pool": pool,
        "deviation_bps": deviation_bps,
        "frequency": frequency,
        "volume": volume,
        "context": context or {},
        "score": score,
        "action": action,
        "action_name": ACTION_NAMES[action],
        "reason": reason,
        "tx": tx_repr,
    }


# ---------------------------------------------------------------------------
# Wallet — key is loaded in-memory only, never printed or copied.
# ---------------------------------------------------------------------------

def load_signer(wallet_path: str):
    """Return (account, address) from the wallet JSON. Key stays in memory."""
    from eth_account import Account

    with open(wallet_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    key = None
    for field in KEY_FIELD_CANDIDATES:
        if data.get(field):
            key = data[field]
            break
    if not key:
        raise SystemExit(
            f"wallet file {wallet_path} has none of the expected key fields "
            f"{KEY_FIELD_CANDIDATES} (found fields: {sorted(data.keys())})"
        )
    account = Account.from_key(key)
    del key
    return account, account.address


# ---------------------------------------------------------------------------
# Chain helpers
# ---------------------------------------------------------------------------

def topic0(signature: str) -> str:
    from web3 import Web3
    return Web3.keccak(text=signature).hex()


def decode_signal_log(log) -> dict | None:
    """Decode a SignalRaised log defensively.

    The event is SignalRaised(address pool, int256 deviationBps,
    uint256 frequency). Whether `pool` is indexed is a compile-time detail,
    so both layouts are handled: indexed pool arrives in topics[1], otherwise
    it is the first 32-byte word of data.
    """
    topics = log["topics"]
    data = bytes(log["data"])
    words = [data[i:i + 32] for i in range(0, len(data) - 31, 32)]
    try:
        if len(topics) >= 2:
            pool = "0x" + topics[1].hex()[-40:]
            num_words = words
        elif words:
            pool = "0x" + words[0].hex()[-40:]
            num_words = words[1:]
        else:
            return None
        deviation = int.from_bytes(num_words[0], "big", signed=True)
        frequency = int.from_bytes(num_words[1], "big")
    except (IndexError, ValueError):
        return None
    return {
        "pool": pool,
        "deviation_bps": deviation,
        "frequency": frequency,
        "block": log["blockNumber"],
        "tx_hash": log["transactionHash"].hex(),
        "log_index": log["logIndex"],
    }


def fetch_pool_context(w3_base, pool: str, span: int = CONTEXT_BLOCK_SPAN) -> dict:
    """Short context from Base Sepolia: recent Swap activity of the pool.

    Best-effort enrichment only — any RPC failure returns an empty context
    and the signal's own fields still drive the score.
    """
    from web3 import Web3

    try:
        latest = w3_base.eth.block_number
        from_block = max(0, latest - span)
        logs = w3_base.eth.get_logs({
            "fromBlock": from_block,
            "toBlock": latest,
            # pool arrives lowercase from log topics; web3 needs checksum form
            "address": Web3.to_checksum_address(pool),
            "topics": ["0x" + topic0(SWAP_SIG)],
        })
        total_volume = 0
        for log in logs:
            data = bytes(log["data"])
            if len(data) >= 64:  # (int256 amountDeltaBps, uint256 volume)
                total_volume += int.from_bytes(data[32:64], "big")
        return {"recent_swaps": len(logs), "recent_volume": total_volume,
                "context_blocks": span}
    except Exception as exc:  # noqa: BLE001 - context must never block a decision
        return {"recent_swaps": None, "recent_volume": None,
                "context_error": str(exc)[:120]}


def submit_decision(w3_base, riskgate_address: str, account,
                    pool: str, score: int, action: int) -> str:
    """Send RiskGate.submitDecision(...) signed by the agent. Returns tx hex."""
    gate = w3_base.eth.contract(
        address=w3_base.to_checksum_address(riskgate_address), abi=RISKGATE_ABI)
    fn = gate.functions.submitDecision(
        w3_base.to_checksum_address(pool), score, action)
    tx = fn.build_transaction({
        "from": account.address,
        "chainId": BASE_SEPOLIA_CHAIN_ID,
        "nonce": w3_base.eth.get_transaction_count(account.address),
        "gasPrice": int(w3_base.eth.gas_price * 1.2),
    })
    if "gas" not in tx:  # estimation failed upstream; use a safe fixed limit
        tx["gas"] = 200_000
    signed = account.sign_transaction(tx)
    tx_hash = w3_base.eth.send_raw_transaction(signed.raw_transaction)
    return tx_hash.hex()


# ---------------------------------------------------------------------------
# Decision pipeline — one signal in, one logged decision out.
# ---------------------------------------------------------------------------

def process_signal(signal: dict, *, w3_base, riskgate_address, account,
                   dry_run: bool, log_path: str) -> dict:
    pool = signal["pool"]
    context = fetch_pool_context(w3_base, pool) if w3_base is not None else {}
    # Volume for scoring: prefer the measured recent volume from Base Sepolia;
    # fall back to a volume carried by the signal itself (fake-signal mode).
    volume = context.get("recent_volume")
    if volume is None:
        volume = int(signal.get("volume", 0))

    score, reason = score_signal(signal["deviation_bps"], signal["frequency"], volume)
    action = action_for_score(score)
    reason += f" -> action {action} ({ACTION_NAMES[action]})"

    if dry_run:
        tx_repr = "dry-run"
    else:
        tx_hash = submit_decision(w3_base, riskgate_address, account,
                                  pool, score, action)
        tx_repr = tx_hash if tx_hash.startswith("0x") else "0x" + tx_hash

    record = make_record(pool, signal["deviation_bps"], signal["frequency"],
                         volume, score, action, reason, tx_repr, context)
    log_decision(record, log_path)
    return record


# ---------------------------------------------------------------------------
# Polling loop over Lasna
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


def poll_loop(args, w3_lasna, w3_base, account) -> None:
    from web3 import Web3  # noqa: F401  (ensures web3 present before loop)

    state = load_state()
    saved = int(state["last_block"]) + 1 if state.get("last_block") is not None else None
    if args.from_block is not None and saved is not None:
        # Resume never reprocesses: an explicit --from-block is a floor for
        # the first run, not a rewind past already-processed blocks.
        next_block = max(args.from_block, saved)
    elif args.from_block is not None:
        next_block = args.from_block
    elif saved is not None:
        next_block = saved
    else:
        next_block = w3_lasna.eth.block_number  # default: only new signals

    seen: set[tuple[str, int]] = set()
    sig_topic = "0x" + topic0(SIGNAL_RAISED_SIG)
    reactive = w3_lasna.to_checksum_address(args.reactive_address)

    print(f"[sentinel] polling Lasna from block {next_block} for SignalRaised "
          f"at {reactive} (dry_run={args.dry_run})", flush=True)

    while True:
        try:
            head = w3_lasna.eth.block_number
            while next_block <= head:
                to_block = min(head, next_block + POLL_BLOCK_SPAN - 1)
                logs = w3_lasna.eth.get_logs({
                    "fromBlock": next_block,
                    "toBlock": to_block,
                    "address": reactive,
                    "topics": [sig_topic],
                })
                for log in logs:
                    key = (log["transactionHash"].hex(), log["logIndex"])
                    if key in seen:
                        continue
                    seen.add(key)
                    signal = decode_signal_log(log)
                    if signal is None:
                        print(f"[warn] undecodable SignalRaised log {key}",
                              file=sys.stderr)
                        continue
                    process_signal(signal, w3_base=w3_base,
                                   riskgate_address=args.riskgate_address,
                                   account=account, dry_run=args.dry_run,
                                   log_path=args.log_file)
                next_block = to_block + 1
                state["last_block"] = to_block
                save_state(state)
        except KeyboardInterrupt:
            print("[sentinel] stopped by user", flush=True)
            return
        except Exception as exc:  # noqa: BLE001 - RPC hiccups must not kill the watcher
            print(f"[warn] poll error: {exc}", file=sys.stderr)
        if args.once:
            return
        time.sleep(args.poll_interval)


# ---------------------------------------------------------------------------
# Self-test — synthetic signals through the scoring model only. Sends nothing,
# touches no network, writes no decision log.
# ---------------------------------------------------------------------------

def self_test() -> int:
    cases = [
        ("calm",    {"deviation_bps": 30,   "frequency": 1,  "volume": 500}),
        ("medium",  {"deviation_bps": 350,  "frequency": 5,  "volume": 50_000}),
        ("extreme", {"deviation_bps": 1200, "frequency": 12, "volume": 900_000}),
    ]
    print("Sentinel scoring self-test (no network, nothing sent)")
    for name, sig in cases:
        score, reason = score_signal(sig["deviation_bps"], sig["frequency"],
                                     sig["volume"])
        action = action_for_score(score)
        print(f"[{name:7s}] deviation={sig['deviation_bps']}bps "
              f"frequency={sig['frequency']} volume={sig['volume']} "
              f"-> score={score} action={action} ({ACTION_NAMES[action]})")
        print(f"          reason: {reason}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Sentinel off-chain AI risk agent (Reactive Meets AI).")
    p.add_argument("--reactive-address",
                   default=os.environ.get("SENTINEL_REACTIVE_ADDRESS"),
                   help="SentinelReactive contract address on Lasna")
    p.add_argument("--riskgate-address",
                   default=os.environ.get("SENTINEL_RISKGATE_ADDRESS"),
                   help="RiskGate contract address on Base Sepolia")
    p.add_argument("--base-rpc", default=os.environ.get("BASE_SEPOLIA_RPC",
                                                        DEFAULT_BASE_RPC),
                   help="Base Sepolia RPC URL")
    p.add_argument("--lasna-rpc", default=os.environ.get("LASNA_RPC",
                                                         DEFAULT_LASNA_RPC),
                   help="Lasna (Reactive testnet) RPC URL")
    p.add_argument("--wallet", default=DEFAULT_WALLET_PATH,
                   help="wallet JSON holding the agent signing key")
    p.add_argument("--from-block", type=int, default=None,
                   help="Lasna block to start scanning from "
                        "(default: saved state, else current head)")
    p.add_argument("--poll-interval", type=float, default=10.0,
                   help="seconds between polls (default 10)")
    p.add_argument("--once", action="store_true",
                   help="run one scan pass and exit")
    p.add_argument("--dry-run", action="store_true",
                   help="score and log decisions but never send a transaction")
    p.add_argument("--log-file", default=DEFAULT_LOG_PATH,
                   help="decision log path (JSON lines)")
    p.add_argument("--self-test", action="store_true",
                   help="run synthetic scoring tests and exit")
    p.add_argument("--fake-signal", metavar="DEV,FREQ,VOL",
                   help="process one synthetic signal (deviation bps, "
                        "frequency, volume) through the full pipeline in "
                        "dry-run mode, then exit; no network is used")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.self_test:
        return self_test()

    if args.fake_signal:
        try:
            dev, freq, vol = (int(x) for x in args.fake_signal.split(","))
        except ValueError:
            raise SystemExit("--fake-signal must be DEV,FREQ,VOL integers")
        signal = {"pool": "0x0000000000000000000000000000000000000000",
                  "deviation_bps": dev, "frequency": freq, "volume": vol}
        record = process_signal(signal, w3_base=None, riskgate_address=None,
                                account=None, dry_run=True,
                                log_path=args.log_file)
        print(f"[fake-signal] processed in dry-run mode: score={record['score']} "
              f"action={record['action']} ({record['action_name']}), nothing sent")
        return 0

    if not args.reactive_address:
        raise SystemExit("--reactive-address is required for polling mode")

    from web3 import Web3
    # Curl-like UA: this VM's egress path rejects the default Python UA on
    # publicnode-backed RPCs (sepolia.base.org).
    import requests as _rq

    def _mk(url):
        s = _rq.Session()
        s.headers.update({"User-Agent": "curl/8.5.0"})
        return Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 20}, session=s))

    w3_lasna = _mk(args.lasna_rpc)
    w3_base = _mk(args.base_rpc)
    if not w3_lasna.is_connected():
        raise SystemExit(f"cannot connect to Lasna RPC {args.lasna_rpc}")
    if not w3_base.is_connected():
        raise SystemExit(f"cannot connect to Base Sepolia RPC {args.base_rpc}")

    account = None
    if not args.dry_run:
        if not args.riskgate_address:
            raise SystemExit("--riskgate-address is required when sending "
                             "decisions (omit it only with --dry-run)")
        account, address = load_signer(args.wallet)
        print(f"[sentinel] agent signer: {address}", flush=True)

    poll_loop(args, w3_lasna, w3_base, account)
    return 0


if __name__ == "__main__":
    sys.exit(main())
