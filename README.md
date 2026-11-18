# Sentinel — an autonomous on-chain risk manager

**Reactive Omni Buildathon · Track: Reactive Meets AI**

Sentinel watches a trading pool around the clock. When market conditions turn
dangerous, an off-chain AI agent scores the risk — and a Reactive Network
contract carries the protective action back on-chain and executes it on a
vault: pause it, tighten its stop, or exit the position. No human in the loop,
no polling bot that has to stay awake: the chain itself wakes the system up.

## How it works

```
 Base Sepolia (84532)                     Lasna (5318007)              Base Sepolia
 ┌─────────────┐   Swap event   ┌──────────────────────┐  SignalRaised  ┌──────────────┐
 │  MockPool   │ ─────────────► │  SentinelReactive    │ ─────────────► │   AI agent   │
 └─────────────┘                │  (Reactive contract) │                │  (off-chain) │
                                └──────────────────────┘                └──────────────┘
                                         ▲                                     │
                                         │ DecisionMade              submitDecision
                                         │                                     ▼
                                ┌──────────────────────┐   executeAction ┌──────────────┐
                                │       RiskGate       │ ──────────────► │  DemoVault   │
                                │  (callback receiver) │    callback     │ pause/tighten│
                                └──────────────────────┘                 │    /exit     │
                                         ▲                             └──────────────┘
                                         └──── reportSignal (callback) ────┘
```

1. **Sense (reactive).** `SentinelReactive`, deployed on Reactive Lasna,
   subscribes to `Swap` events of a pool on Base Sepolia. Every swap is
   reduced on-chain to a coarse signal (absolute deviation + running swap
   count), emitted as `SignalRaised`, and forwarded to `RiskGate.reportSignal`
   through a Reactive callback.
2. **Decide (AI).** An off-chain agent listens for `SignalRaised`, enriches
   the signal with recent pool context (swap count and volume over the last
   200 blocks), and computes a transparent risk score from 0–100. The model is
   deliberately readable — deviation contributes up to 50 points, frequency up
   to 30, log-scaled volume up to 20 — and every decision is logged with its
   one-line reasoning. The agent records its verdict on-chain via
   `RiskGate.submitDecision(pool, score, action)`.
3. **Act (reactive).** `SentinelReactive` also subscribes to `DecisionMade`.
   Each decision triggers a second callback, `RiskGate.executeAction`, which
   applies the action to `DemoVault`: `pause()`, `tightenStop(200)`, or
   `exitPosition()`.

Action bands: score < 40 → none · 40–59 → pause · 60–79 → tighten stop ·
≥ 80 → exit position.

## Live proof (testnet, verifiable)

Final deployment — Base Sepolia: [MockPool](https://sepolia.basescan.org/address/0xF99e43cc38F5b66cEd4a457089ACC9905b9C6Dfa) ·
[DemoVault](https://sepolia.basescan.org/address/0x8f9C45C14eb2E19330C7B05543aA731c656AD68c) ·
[RiskGate](https://sepolia.basescan.org/address/0x5C7921954F97d747D7de4D475610767e1D91022c) ·
Lasna: [SentinelReactive](https://lasna.reactscan.net/address/0x0287E29eA6B29B7d45E66d88FfEf6F3392A6e4ba)

A three-act demo was executed against this deployment. Every leg — both
Reactive callbacks included — landed on-chain:

| Act | Trigger | Agent score → action | On-chain result |
|---|---|---|---|
| Calm market | swap 50 bps, vol 1k | **6 → none** | vault untouched |
| First shock | swap 1,200 bps, vol 900k | **56 → pause** | `DemoVault.paused = true` |
| Repeated shock | swap 1,200 bps, vol 900k | **79 → tightenStop** | `DemoVault.stopBps 500 → 200` |

Key transactions (Base Sepolia unless noted):

- Act 1: [swap](https://sepolia.basescan.org/tx/0xdd5b1f194ab9628ec5c2c656a182d3d52acafa849418d393274b6baf9290299e) ·
  [SignalRaised (Lasna)](https://lasna.reactscan.net/tx/0x6b42bb1b96aff20ca827f24788b8734cb067918948156995467c6b1f555d1b9d) ·
  [SignalReported](https://sepolia.basescan.org/tx/0xee749d6c12ad8e8605fad0263acc4eafe7be8d47e06ba403b316b37555bbd264) ·
  [decision](https://sepolia.basescan.org/tx/0x7cd1c2325d58c0e34eab9726231dd0a8ed5947c9cf182b29271021904618b31c) ·
  [ActionExecuted](https://sepolia.basescan.org/tx/0xf326a9112737d90f21c967cd623ddc82b9c2c8820133d70b525152a9519ec0ba)
- Act 2: [swap](https://sepolia.basescan.org/tx/0xfc681cd9fd785ecf9d3705594f2e7a9359ebede323fa476456473480b20ca32e) ·
  [SignalRaised (Lasna)](https://lasna.reactscan.net/tx/0xec6d3ddad0155feb5df98d48d3105bc503157e039f41a07f2c76c506d0ea8065) ·
  [SignalReported](https://sepolia.basescan.org/tx/0x2d5301a8269ae30b591018763bb158cd6549a789966c4e223397b31ef2bf1e9c) ·
  [decision](https://sepolia.basescan.org/tx/0x547d6cc3eb03f94d855c38fbc087bc67ccc19d1eccbf765c335ea3c60351f82a) ·
  [ActionExecuted](https://sepolia.basescan.org/tx/0x7719157d7d6704ae9a4e388da7c7d62c1e6a83ce24baf09fcd1c4c6f836b5ec5)
- Act 3: [swap](https://sepolia.basescan.org/tx/0xfc8436eefa820470819f5dc0ec2eb9fbe0919e6b155e1f117fd1216c3f64a728) ·
  [SignalRaised (Lasna)](https://lasna.reactscan.net/tx/0x6ed0232940a9d49f026f22d49b5139d8248f093e209b2c34c90e314118fbab34) ·
  [SignalReported](https://sepolia.basescan.org/tx/0xf03e8b3ab39548db9afaad77aef7c6c465877239519f03ed9ea601afb649e497) ·
  [decision](https://sepolia.basescan.org/tx/0xcfb12315b7071de305c098a14be0bf115c98888d2b727bc5f9520b2e29aa54c9) ·
  [ActionExecuted](https://sepolia.basescan.org/tx/0x90a6d241a437ac1a232a528af269ba3f7879d295b49749cf6bd26568ad64950a)

After the run, `SentinelReactive` showed **zero debt** and positive lREACT
reserves — it stayed active throughout.

## Repository layout

- `contracts/` — `MockPool.sol` (demo event source), `DemoVault.sol` (the
  protected vault), `RiskGate.sol` (callback receiver + decision registry,
  Base Sepolia), `SentinelReactive.sol` (the Reactive contract, Lasna).
  Built with Foundry against `reactive-lib-omni`.
- `agent/sentinel_agent.py` — the AI agent: Lasna listener, context fetcher,
  scoring model, decision submitter. `python sentinel_agent.py --self-test`
  runs three synthetic signals through the scorer; `--dry-run` decides
  without sending.
- `scripts/deploy.py` — the exact deployment/wiring/demo tooling used for
  the live proof (CREATE-address precomputation resolves the
  RiskGate ↔ SentinelReactive address cycle across two chains).

## Running it yourself

1. `forge build` (Foundry; `foundry.toml` pins the Omni library remapping).
2. Fund a testnet wallet: Base Sepolia ETH for the destination side, and
   lREACT via the Lasna faucet contract (send Base Sepolia ETH to
   `0x2afaFD298b23b62760711756088F75B7409f5967`; 1 ETH → 100 lREACT).
3. Deploy with `scripts/deploy.py` (`precompute` → `deploy-base` →
   `deploy-lasna` → `wire`), which also funds the reactive contract's
   reserves through the system contract's `depositTo`.
4. Start the agent with the deployed addresses and trigger a swap on
   MockPool. Watch the loop close on both explorers.

## Design notes & honest limitations

- **Economics are real.** Reactive executions are billed against the
  contract's lREACT reserves (≈0.013 lREACT per reaction on Lasna at proof
  time), and any outstanding debt halts execution — Sentinel's deployment
  funds reserves up front and exposes `coverDebt()`. Destination callbacks
  are paid by `RiskGate` in Base Sepolia ETH.
- **Callback gas matters.** Our first deployment's `reportSignal` callback
  (200k gas) was delivered by the proxy but its inner call never executed;
  raising the limit to 400k fixed it — the final deployment above includes
  the fix, and all three `SignalReported` events landed.
- **The pool and vault are demos.** `MockPool` emits synthetic swaps so the
  demo is triggerable on demand; `DemoVault` holds a notional position. The
  architecture — reactive sensing, off-chain judgment, reactive execution —
  is the product; wiring it to a real DEX pool and a real vault is a matter
  of pointing the subscription and the gate at production contracts.
- **The AI is a transparent scorer, not a black box.** For a risk circuit
  breaker, auditability beats cleverness: every score decomposes into
  deviation/frequency/volume points and is logged with its reasoning before
  it is written on-chain.

## License

MIT
