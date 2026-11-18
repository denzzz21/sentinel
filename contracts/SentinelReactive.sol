// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { AbstractReactive } from "@reactive/src/base/AbstractReactive.sol";
import { IReactive } from "@reactive/src/interfaces/IReactive.sol";
import { ISystemContract } from "@reactive/src/interfaces/ISystemContract.sol";

/// @title SentinelReactive — the reactive half of Sentinel, deployed once on Reactive Lasna.
/// @notice It subscribes to two event streams on Base Sepolia:
///         1. `Swap` events from MockPool — each swap is reduced to a coarse signal
///            (absolute price deviation + running swap count) and forwarded to
///            RiskGate.reportSignal via callback.
///         2. `DecisionMade` events from RiskGate — emitted when the off-chain AI agent
///            records a decision; each one triggers a RiskGate.executeAction callback,
///            which applies the AI's protective action to the DemoVault.
///         The contract is funded with lREACT: execution fees are charged against it,
///         and any outstanding debt halts execution until `coverDebt()` settles it.
contract SentinelReactive is AbstractReactive {
    uint256 internal constant BASE_SEPOLIA_CHAIN_ID = 84532;

    // Callback gas limits. The network ignores limits below 100_000; both entries do
    // storage writes (and executeAction calls into the vault), so leave headroom.
    uint64 internal constant REPORT_SIGNAL_GAS_LIMIT = 400_000;
    uint64 internal constant EXECUTE_ACTION_GAS_LIMIT = 300_000;

    /// @notice The demo pool whose swaps are watched (Base Sepolia).
    address public immutable mockPool;
    /// @notice The gate that receives this contract's callbacks (Base Sepolia).
    address public immutable riskGate;

    uint256 internal immutable swapTopic;
    uint256 internal immutable decisionMadeTopic;

    /// @notice Running count of swaps seen per pool — the coarse "frequency" signal.
    mapping(address => uint256) public swapCount;

    /// @notice Emitted on Lasna for every swap signal, before the callback is requested.
    ///         The off-chain AI agent listens to this event.
    event SignalRaised(address indexed pool, int256 deviationBps, uint256 frequency);

    /// @param mockPool_ MockPool address on Base Sepolia.
    /// @param riskGate_ RiskGate address on Base Sepolia.
    /// @dev The constructor is payable so the contract can be funded with lREACT at deploy time.
    constructor(address mockPool_, address riskGate_) payable {
        mockPool = mockPool_;
        riskGate = riskGate_;
        swapTopic = uint256(keccak256("Swap(address,int256,uint256)"));
        decisionMadeTopic = uint256(keccak256("DecisionMade(address,uint256,uint8)"));

        // Watch the demo pool's swaps on Base Sepolia.
        SYSTEM.subscribe(
            BASE_SEPOLIA_CHAIN_ID, mockPool_, swapTopic, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE
        );
        // Watch the AI agent's decisions, recorded by RiskGate on Base Sepolia.
        SYSTEM.subscribe(
            BASE_SEPOLIA_CHAIN_ID, riskGate_, decisionMadeTopic, REACTIVE_IGNORE, REACTIVE_IGNORE, REACTIVE_IGNORE
        );
    }

    /// @inheritdoc IReactive
    function react(LogRecord memory log_) external override onlySystem {
        if (log_.topic0 == swapTopic && log_.contractAddress == mockPool) {
            _onSwap(log_);
        } else if (log_.topic0 == decisionMadeTopic && log_.contractAddress == riskGate) {
            _onDecision(log_);
        }
    }

    /// @dev Reduce a swap to a coarse signal and forward it to RiskGate.reportSignal.
    function _onSwap(LogRecord memory log_) internal {
        (int256 amountDeltaBps,) = abi.decode(log_.data, (int256, uint256));

        address pool = log_.contractAddress;
        uint256 frequency = ++swapCount[pool];
        int256 deviationBps = amountDeltaBps >= 0 ? amountDeltaBps : -amountDeltaBps;

        emit SignalRaised(pool, deviationBps, frequency);

        // The first payload argument is reserved: the network replaces it with this
        // contract's address, which RiskGate checks via onlyCallbackSender.
        bytes memory payload = abi.encodeWithSignature(
            "reportSignal(address,address,int256,uint256)", address(0), pool, deviationBps, frequency
        );

        SYSTEM.requestCallbackV_1_0(
            ISystemContract.CallbackConfiguration_V_1_0({
                chainId: BASE_SEPOLIA_CHAIN_ID,
                recipient: riskGate,
                gasLimit: REPORT_SIGNAL_GAS_LIMIT,
                payload: payload
            })
        );
    }

    /// @dev Forward a recorded AI decision to RiskGate.executeAction.
    function _onDecision(LogRecord memory log_) internal {
        // DecisionMade has the pool as its indexed argument, i.e. topic1.
        address pool = address(uint160(log_.topic1));

        bytes memory payload = abi.encodeWithSignature("executeAction(address,address)", address(0), pool);

        SYSTEM.requestCallbackV_1_0(
            ISystemContract.CallbackConfiguration_V_1_0({
                chainId: BASE_SEPOLIA_CHAIN_ID,
                recipient: riskGate,
                gasLimit: EXECUTE_ACTION_GAS_LIMIT,
                payload: payload
            })
        );
    }

    /// @notice Settle this contract's outstanding debt with the system contract from
    ///         its own lREACT balance, so execution can resume.
    function coverDebt() external {
        _coverDebt();
    }
}
