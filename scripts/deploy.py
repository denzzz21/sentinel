#!/usr/bin/env python3
"""Sentinel deploy + loop driver. Runs ONLY on this VM.

Signing key: read in-memory from ~/workspace/tzero-farm/wallet.json
(field private_key). The value is never printed, logged, or copied.
State (addresses, tx hashes — no secrets) is kept in deploy_state.json
next to this script.
"""
from __future__ import annotations

import json
import os
import sys
import time

from web3 import Web3
from eth_account import Account
from eth_utils import keccak

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "deploy_state.json")
ART_DIR = os.path.join(HERE, "artifacts")
WALLET_PATH = os.path.expanduser("~/workspace/tzero-farm/wallet.json")

BASE_RPC = "https://sepolia.base.org"
LASNA_RPC = "https://lasna-rpc.rnk.dev/"
BASE_ID = 84532
LASNA_ID = 5318007

FAUCET = "0x2afaFD298b23b62760711756088F75B7409f5967"
SYSTEM = "0x8888888888888888888888888888888888888888"
BASE_CALLBACK_PROXY = "0x476F44b11F5C942c58f4c8E3d0300E5710743585"

FAUCET_ABI = [{
    "type": "function", "name": "request", "stateMutability": "payable",
    "inputs": [{"name": "to", "type": "address"}], "outputs": [],
}]
SYSTEM_ABI = [
    {"type": "function", "name": "depositTo", "stateMutability": "payable",
     "inputs": [{"name": "to", "type": "address"}], "outputs": []},
    {"type": "function", "name": "debt", "stateMutability": "view",
     "inputs": [{"name": "who", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "reserves", "stateMutability": "view",
     "inputs": [{"name": "who", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]


def load_account():
    with open(WALLET_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    key = data["private_key"]
    acct = Account.from_key(key)
    del key
    return acct


def connect(url):
    # This VM's egress path rejects the default Python requests UA on
    # publicnode-backed RPCs (sepolia.base.org); a curl-like UA passes.
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": "curl/8.5.0"})
    w3 = Web3(
        Web3.HTTPProvider(url, request_kwargs={"timeout": 30}, session=session)
    )
    if not w3.is_connected():
        raise SystemExit(f"cannot connect to {url}")
    return w3


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def artifact(name):
    with open(os.path.join(ART_DIR, f"{name}.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def create_address(sender: str, nonce: int) -> str:
    """CREATE address: keccak256(rlp([sender, nonce]))[12:]."""
    sender_b = bytes.fromhex(sender[2:] if sender.startswith("0x") else sender)
    if nonce == 0:
        nonce_enc = b"\x80"
    elif nonce < 128:
        nonce_enc = bytes([nonce])
    else:
        b = nonce.to_bytes((nonce.bit_length() + 7) // 8, "big")
        nonce_enc = bytes([0x80 + len(b)]) + b
    payload = b"\x94" + sender_b + nonce_enc
    rlp = bytes([0xC0 + len(payload)]) + payload
    return "0x" + keccak(rlp)[12:].hex()


def _hex(h) -> str:
    s = h.hex()
    return s if s.startswith("0x") else "0x" + s


def send(w3, acct, tx_fields: dict, chain_id: int, value: int = 0,
         gas_buffer: float = 1.3) -> dict:
    """Sign + send a tx described by {to?, data?}; fills nonce/gas/gasPrice."""
    tx = dict(tx_fields)
    tx["from"] = acct.address
    tx["chainId"] = chain_id
    tx["nonce"] = w3.eth.get_transaction_count(acct.address)
    tx["value"] = value
    tx["gasPrice"] = int(w3.eth.gas_price * 1.25) or 1
    est_tx = {k: v for k, v in tx.items() if k in ("from", "to", "data", "value")}
    try:
        est = w3.eth.estimate_gas(est_tx)
        tx["gas"] = int(est * gas_buffer) + 10_000
    except Exception as e:
        print(f"[warn] estimate_gas failed ({str(e)[:80]}), using 3,000,000")
        tx["gas"] = 3_000_000
    signed = acct.sign_transaction(tx)
    h = w3.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=300)
    return {
        "hash": _hex(h),
        "status": rcpt["status"],
        "block": rcpt["blockNumber"],
        "gasUsed": rcpt["gasUsed"],
        "contractAddress": rcpt.get("contractAddress"),
    }


def call_fn(w3, acct, contract, fn_name, args, chain_id, value=0):
    data = contract.encode_abi(fn_name, args=list(args))
    return send(w3, acct, {"to": contract.address, "data": data}, chain_id, value=value)


# ---------------------------------------------------------------------------

def cmd_status():
    acct = load_account()
    wb = connect(BASE_RPC)
    wl = connect(LASNA_RPC)
    print(f"farming address : {acct.address}")
    print(f"Base Sepolia    : ETH {wb.eth.get_balance(acct.address)/1e18:.6f}  nonce {wb.eth.get_transaction_count(acct.address)}  block {wb.eth.block_number}")
    print(f"Lasna           : lREACT {wl.eth.get_balance(acct.address)/1e18:.6f}  nonce {wl.eth.get_transaction_count(acct.address)}  block {wl.eth.block_number}")


def cmd_faucet():
    amount = float(sys.argv[2]) if len(sys.argv) > 2 else 0.002
    acct = load_account()
    wb = connect(BASE_RPC)
    bal_before = wb.eth.get_balance(acct.address)
    if bal_before < int(amount * 1e18) + int(0.0001e18):
        raise SystemExit(f"Base ETH too low for faucet: {bal_before/1e18}")
    faucet = wb.eth.contract(address=Web3.to_checksum_address(FAUCET), abi=FAUCET_ABI)
    res = call_fn(wb, acct, faucet, "request",
                  (Web3.to_checksum_address(acct.address),), BASE_ID,
                  value=int(amount * 1e18))
    print(json.dumps({"faucet_tx": res}, indent=2))
    if res["status"] != 1:
        raise SystemExit("faucet tx FAILED on-chain")
    st = load_state()
    st["faucet_tx"] = res
    st["farming"] = acct.address
    save_state(st)


def cmd_poll_lreact(timeout_s=600):
    acct = load_account()
    wl = connect(LASNA_RPC)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        bal = wl.eth.get_balance(acct.address)
        print(f"[{int(time.time()-t0):4d}s] lREACT balance: {bal/1e18:.6f}", flush=True)
        if bal > 0:
            st = load_state()
            st["lreact_balance_seen"] = bal / 1e18
            save_state(st)
            return
        time.sleep(20)
    raise SystemExit("TIMEOUT: no lREACT after faucet send — STOP, do not resend")


def cmd_precompute():
    acct = load_account()
    wb = connect(BASE_RPC)
    wl = connect(LASNA_RPC)
    nb = wb.eth.get_transaction_count(acct.address)
    nl = wl.eth.get_transaction_count(acct.address)
    addrs = {
        "base_nonce": nb,
        "lasna_nonce": nl,
        "MockPool": create_address(acct.address, nb),
        "DemoVault": create_address(acct.address, nb + 1),
        "RiskGate": create_address(acct.address, nb + 2),
        "SentinelReactive": create_address(acct.address, nl),
    }
    print(json.dumps(addrs, indent=2))
    st = load_state()
    st["precomputed"] = addrs
    st["farming"] = acct.address
    save_state(st)


def deploy_contract(w3, acct, name, args, chain_id, value=0):
    from eth_abi import encode as abi_encode
    art = artifact(name)
    bytecode = art["bytecode"]
    if args:
        ctor = next(e for e in art["abi"] if e.get("type") == "constructor")
        types = [i["type"] for i in ctor["inputs"]]
        enc = abi_encode(types, list(args)).hex()
        bytecode = bytecode + enc if bytecode.startswith("0x") else "0x" + bytecode + enc
    return send(w3, acct, {"data": bytecode}, chain_id, value=value)


def cmd_deploy_base():
    acct = load_account()
    wb = connect(BASE_RPC)
    st = load_state()
    pre = st["precomputed"]
    # sanity: nonce must still match the precompute snapshot
    nb = wb.eth.get_transaction_count(acct.address)
    if nb != pre["base_nonce"]:
        raise SystemExit(f"Base nonce shifted: precompute {pre['base_nonce']} vs now {nb} — STOP")
    sentinel = Web3.to_checksum_address(pre["SentinelReactive"])

    r1 = deploy_contract(wb, acct, "MockPool", (), BASE_ID)
    print("MockPool:", json.dumps(r1))
    if r1["status"] != 1 or r1["contractAddress"].lower() != pre["MockPool"].lower():
        raise SystemExit(f"MockPool address mismatch: got {r1['contractAddress']} want {pre['MockPool']} — STOP")
    r2 = deploy_contract(wb, acct, "DemoVault", (1_000_000, 500), BASE_ID)
    print("DemoVault:", json.dumps(r2))
    if r2["status"] != 1 or r2["contractAddress"].lower() != pre["DemoVault"].lower():
        raise SystemExit(f"DemoVault address mismatch: got {r2['contractAddress']} want {pre['DemoVault']} — STOP")
    r3 = deploy_contract(wb, acct, "RiskGate", (sentinel,), BASE_ID)
    print("RiskGate:", json.dumps(r3))
    if r3["status"] != 1 or r3["contractAddress"].lower() != pre["RiskGate"].lower():
        raise SystemExit(f"RiskGate address mismatch: got {r3['contractAddress']} want {pre['RiskGate']} — STOP")
    st["deployed"] = st.get("deployed", {})
    st["deployed"].update({"MockPool": r1, "DemoVault": r2, "RiskGate": r3})
    save_state(st)
    print("BASE DEPLOYS OK")


def cmd_deploy_lasna():
    acct = load_account()
    wl = connect(LASNA_RPC)
    st = load_state()
    pre = st["precomputed"]
    nl = wl.eth.get_transaction_count(acct.address)
    if nl != pre["lasna_nonce"]:
        raise SystemExit(f"Lasna nonce shifted: precompute {pre['lasna_nonce']} vs now {nl} — STOP")
    pool = Web3.to_checksum_address(pre["MockPool"])
    gate = Web3.to_checksum_address(pre["RiskGate"])
    r = deploy_contract(wl, acct, "SentinelReactive", (pool, gate), LASNA_ID)
    print("SentinelReactive:", json.dumps(r))
    if r["status"] != 1 or r["contractAddress"].lower() != pre["SentinelReactive"].lower():
        raise SystemExit(f"SentinelReactive address mismatch: got {r['contractAddress']} want {pre['SentinelReactive']} — STOP")
    st["deployed"] = st.get("deployed", {})
    st["deployed"]["SentinelReactive"] = r
    save_state(st)
    print("LASNA DEPLOY OK")


def cmd_wire():
    acct = load_account()
    wb = connect(BASE_RPC)
    wl = connect(LASNA_RPC)
    st = load_state()
    pre = st["precomputed"]
    pool = Web3.to_checksum_address(pre["MockPool"])
    vault_a = Web3.to_checksum_address(pre["DemoVault"])
    gate_a = Web3.to_checksum_address(pre["RiskGate"])
    sentinel = Web3.to_checksum_address(pre["SentinelReactive"])
    out = {}

    vault = wb.eth.contract(address=vault_a, abi=artifact("DemoVault")["abi"])
    gate = wb.eth.contract(address=gate_a, abi=artifact("RiskGate")["abi"])

    r = call_fn(wb, acct, vault, "setGate", (gate_a,), BASE_ID)
    out["vault_setGate"] = r; print("vault.setGate:", r["hash"], r["status"])
    r = call_fn(wb, acct, gate, "setVault", (vault_a,), BASE_ID)
    out["gate_setVault"] = r; print("gate.setVault:", r["hash"], r["status"])
    r = call_fn(wb, acct, gate, "setAgent", (Web3.to_checksum_address(acct.address),), BASE_ID)
    out["gate_setAgent"] = r; print("gate.setAgent:", r["hash"], r["status"])
    # position already non-zero from constructor; confirm, else setPosition
    size = vault.functions.positionSize().call()
    print("vault positionSize:", size)
    if size == 0:
        r = call_fn(wb, acct, vault, "setPosition", (1_000_000,), BASE_ID)
        out["vault_setPosition"] = r; print("vault.setPosition:", r["hash"], r["status"])
    # fund RiskGate with a little Base ETH so callback pay() can settle charges
    # (proof run: pay() consumed ~0.0000015 ETH total; 0.0001 is ample and the
    # farming wallet's remaining Base Sepolia balance is tight)
    r = send(wb, acct, {"to": gate_a}, BASE_ID, value=int(0.0001e18))
    out["gate_fund_eth"] = r; print("fund RiskGate 0.0001 ETH:", r["hash"], r["status"])
    # fund SentinelReactive reserves via system contract depositTo on Lasna
    # (manual fixed-gas tx: the estimate path fails on Lasna for this call)
    r = _deposit_to(wl, acct, sentinel, int(0.1e18))
    out["sentinel_depositTo"] = r; print("system.depositTo(sentinel) 0.1 lREACT:", r["hash"], r["status"])
    st["wiring"] = out
    save_state(st)
    cmd_sentinel_state()
    cmd_vault_state()


def _vault(wb, addr):
    return wb.eth.contract(address=Web3.to_checksum_address(addr), abi=artifact("DemoVault")["abi"])


def cmd_vault_state():
    wb = connect(BASE_RPC)
    st = load_state()
    v = _vault(wb, st["precomputed"]["DemoVault"])
    s = {
        "positionSize": v.functions.positionSize().call(),
        "paused": v.functions.paused().call(),
        "stopBps": v.functions.stopBps().call(),
        "gate": v.functions.gate().call(),
    }
    print("VAULT:", json.dumps(s))
    return s


def cmd_sentinel_state():
    wl = connect(LASNA_RPC)
    st = load_state()
    sentinel = Web3.to_checksum_address(st["precomputed"]["SentinelReactive"])
    sysc = wl.eth.contract(address=Web3.to_checksum_address(SYSTEM), abi=SYSTEM_ABI)
    res = {}
    for fn in ("debt", "reserves"):
        try:
            res[fn] = sysc.functions[fn](sentinel).call()
        except Exception as e:
            res[fn] = f"ERROR: {str(e)[:100]}"
    res["contract_balance_lREACT"] = wl.eth.get_balance(sentinel) / 1e18
    res["farming_lREACT"] = wl.eth.get_balance(load_account().address) / 1e18
    print("SENTINEL:", json.dumps(res))
    return res


def cmd_swap():
    acct = load_account()
    wb = connect(BASE_RPC)
    st = load_state()
    pre = st["precomputed"]
    before = cmd_vault_state()
    st["vault_before"] = before
    pool = wb.eth.contract(address=Web3.to_checksum_address(pre["MockPool"]), abi=artifact("MockPool")["abi"])
    lasna_head = connect(LASNA_RPC).eth.block_number
    bps = int(sys.argv[2]) if len(sys.argv) > 2 else 1200
    vol = int(sys.argv[3]) if len(sys.argv) > 3 else 900_000
    r = call_fn(wb, acct, pool, "swap", (bps, vol), BASE_ID)
    print("swap tx:", json.dumps(r))
    st["swap_tx"] = r
    st["lasna_head_at_swap"] = lasna_head
    save_state(st)


def cmd_poll_signal(timeout_s=600):
    wl = connect(LASNA_RPC)
    st = load_state()
    sentinel = Web3.to_checksum_address(st["precomputed"]["SentinelReactive"])
    topic = "0x" + Web3.keccak(text="SignalRaised(address,int256,uint256)").hex()
    from_block = max(0, st.get("lasna_head_at_swap", 0) - 5)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        head = wl.eth.block_number
        logs = wl.eth.get_logs({"fromBlock": from_block, "toBlock": head,
                                "address": sentinel, "topics": [topic]})
        if logs:
            lg = logs[0]
            print("SIGNALRAISED:", json.dumps({
                "tx": lg["transactionHash"].hex(), "block": lg["blockNumber"],
                "topics": [t.hex() for t in lg["topics"]],
                "data": lg["data"].hex()}))
            st["signal_raised"] = {"tx": lg["transactionHash"].hex(), "block": lg["blockNumber"]}
            save_state(st)
            return
        print(f"[{int(time.time()-t0):4d}s] no SignalRaised yet (head {head})", flush=True)
        time.sleep(15)
    raise SystemExit("TIMEOUT waiting for SignalRaised")


def cmd_poll_final(timeout_s=900):
    wb = connect(BASE_RPC)
    st = load_state()
    gate_a = Web3.to_checksum_address(st["precomputed"]["RiskGate"])
    topics = {
        "SignalReported": "0x" + Web3.keccak(text="SignalReported(address,int256,uint256)").hex(),
        "DecisionMade": "0x" + Web3.keccak(text="DecisionMade(address,uint256,uint8)").hex(),
        "ActionExecuted": "0x" + Web3.keccak(text="ActionExecuted(address,uint8)").hex(),
    }
    swap_block = st["swap_tx"]["block"]
    found = {}
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        head = wb.eth.block_number
        for name, tp in topics.items():
            if name in found:
                continue
            logs = wb.eth.get_logs({"fromBlock": swap_block, "toBlock": head,
                                    "address": gate_a, "topics": [tp]})
            if logs:
                lg = logs[0]
                found[name] = {"tx": lg["transactionHash"].hex(), "block": lg["blockNumber"]}
                print(f"{name}: tx {found[name]['tx']} block {found[name]['block']}", flush=True)
        if "ActionExecuted" in found:
            break
        time.sleep(15)
    st["final_events"] = found
    save_state(st)
    print("EVENTS:", json.dumps(found))
    after = cmd_vault_state()
    st["vault_after"] = after
    save_state(st)
    cmd_sentinel_state()


def _deposit_to(wl, acct, sentinel, value):
    """depositTo with an explicit gas limit (send()'s estimate path fails
    on Lasna for this call; the 3M-gas fallback is far too large at
    Lasna's gas price). Returns the same dict shape as send()."""
    sysc = wl.eth.contract(address=Web3.to_checksum_address(SYSTEM), abi=SYSTEM_ABI)
    data = sysc.encode_abi("depositTo", args=[sentinel])
    tx = {
        "from": acct.address,
        "to": Web3.to_checksum_address(SYSTEM),
        "data": data,
        "chainId": LASNA_ID,
        "nonce": wl.eth.get_transaction_count(acct.address),
        "value": value,
        "gas": 100_000,  # observed usage 51_795; node checks limit*price+value
        "gasPrice": int(wl.eth.gas_price * 1.25) or 1,
    }
    signed = acct.sign_transaction(tx)
    h = wl.eth.send_raw_transaction(signed.raw_transaction)
    rcpt = wl.eth.wait_for_transaction_receipt(h, timeout=300)
    return {"hash": _hex(h), "status": rcpt["status"],
            "block": rcpt["blockNumber"], "gasUsed": rcpt["gasUsed"],
            "contractAddress": None}


def cmd_fund_reactive():
    """depositTo(SentinelReactive) with an explicit gas limit."""
    value = int(float(sys.argv[2]) * 1e18) if len(sys.argv) > 2 else int(0.1e18)
    acct = load_account()
    wl = connect(LASNA_RPC)
    st = load_state()
    sentinel = Web3.to_checksum_address(st["precomputed"]["SentinelReactive"])
    r = _deposit_to(wl, acct, sentinel, value)
    print("depositTo:", r["hash"], r["status"], "gasUsed", r["gasUsed"])
    st.setdefault("wiring", {})["sentinel_depositTo"] = {
        "hash": r["hash"], "status": r["status"], "gasUsed": r["gasUsed"]}
    save_state(st)
    cmd_sentinel_state()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {
        "status": cmd_status,
        "faucet": cmd_faucet,
        "poll-lreact": cmd_poll_lreact,
        "precompute": cmd_precompute,
        "deploy-base": cmd_deploy_base,
        "deploy-lasna": cmd_deploy_lasna,
        "wire": cmd_wire,
        "fund-reactive": cmd_fund_reactive,
        "vault-state": cmd_vault_state,
        "sentinel-state": cmd_sentinel_state,
        "swap": cmd_swap,
        "poll-signal": cmd_poll_signal,
        "poll-final": cmd_poll_final,
    }[cmd]()
