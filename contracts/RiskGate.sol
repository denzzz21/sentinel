// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

import { AbstractCallback } from "@reactive/src/base/AbstractCallback.sol";
import { IPayable } from "@reactive/src/interfaces/IPayable.sol";
import { DemoVault } from "./DemoVault.sol";

/// @title RiskGate — destination-side gate on Base Sepolia.
/// @notice Three entries:
///         1. `reportSignal`  — callback from SentinelReactive carrying a coarse risk signal.
///         2. `submitDecision` — direct call from the registered off-chain AI agent.
///         3. `executeAction` — callback from SentinelReactive that turns the stored
///            decision into a protective action on the DemoVault.
///         Both callback entries are guarded by `onlyServiceProvider` (caller must be the
///         Base Sepolia callback proxy) and `onlyCallbackSender` (the address the network
///         injects as the first payload argument must be SentinelReactive).
contract RiskGate is AbstractCallback {
    /// @notice Callback proxy for Base Sepolia (chain ID 84532).
    address internal constant BASE_SEPOLIA_CALLBACK_PROXY = 0x476F44b11F5C942c58f4c8E3d0300E5710743585;

    /// @notice Stop threshold applied when the AI picks action 2 (tightenStop), in basis points.
    uint256 public constant TIGHTENED_STOP_BPS = 200;

    // Action codes, shared with the off-chain AI agent.
    uint8 public constant ACTION_NONE = 0;
    uint8 public constant ACTION_PAUSE = 1;
    uint8 public constant ACTION_TIGHTEN_STOP = 2;
    uint8 public constant ACTION_EXIT_POSITION = 3;

    address public owner;
    /// @notice The registered AI agent — the only address allowed to call `submitDecision`.
    address public agent;
    /// @notice The DemoVault the protective actions act on.
    address public vault;

    struct Signal {
        int256 deviationBps;
        uint256 frequency;
        uint256 updatedAt;
    }

    struct Decision {
        uint256 score;
        uint8 action;
        uint256 updatedAt;
    }

    mapping(address => Signal) public signals;
    mapping(address => Decision) public decisions;

    event SignalReported(address indexed pool, int256 deviationBps, uint256 frequency);
    event DecisionMade(address indexed pool, uint256 score, uint8 action);
    event ActionExecuted(address indexed pool, uint8 action);
    event AgentSet(address indexed agent);
    event VaultSet(address indexed vault);

    modifier onlyOwner() {
        require(msg.sender == owner, "RiskGate: not owner");
        _;
    }

    modifier onlyAgent() {
        require(msg.sender == agent, "RiskGate: not agent");
        _;
    }

    /// @param reactive_ Address of SentinelReactive on Lasna — the only reactive
    ///        contract whose callbacks this gate accepts. Immutable (via AbstractCallback).
    constructor(address reactive_) AbstractCallback(IPayable(payable(BASE_SEPOLIA_CALLBACK_PROXY)), reactive_) {
        owner = msg.sender;
    }

    /// @notice Register (or replace) the AI agent address.
    function setAgent(address agent_) external onlyOwner {
        agent = agent_;
        emit AgentSet(agent_);
    }

    /// @notice Point the gate at the DemoVault it protects.
    function setVault(address vault_) external onlyOwner {
        vault = vault_;
        emit VaultSet(vault_);
    }

    /// @notice Callback entry #1: store a coarse risk signal reported by SentinelReactive.
    /// @dev The first argument is reserved: the network replaces it with the address of
    ///      the reactive contract that requested the callback, and `onlyCallbackSender`
    ///      verifies it. The payload must reserve it as `address(0)`.
    function reportSignal(address reactive_, address pool_, int256 deviationBps_, uint256 frequency_)
        external
        onlyServiceProvider
        onlyCallbackSender(reactive_)
    {
        signals[pool_] = Signal({ deviationBps: deviationBps_, frequency: frequency_, updatedAt: block.timestamp });
        emit SignalReported(pool_, deviationBps_, frequency_);
    }

    /// @notice Called directly by the off-chain AI agent to record its risk decision.
    /// @param pool_ Pool the decision is about.
    /// @param score_ AI risk score (0–100).
    /// @param action_ One of ACTION_NONE / ACTION_PAUSE / ACTION_TIGHTEN_STOP / ACTION_EXIT_POSITION.
    function submitDecision(address pool_, uint256 score_, uint8 action_) external onlyAgent {
        require(action_ <= ACTION_EXIT_POSITION, "RiskGate: bad action");
        decisions[pool_] = Decision({ score: score_, action: action_, updatedAt: block.timestamp });
        emit DecisionMade(pool_, score_, action_);
    }

    /// @notice Callback entry #2: execute the stored decision on the DemoVault.
    /// @dev Same reserved first argument as `reportSignal`.
    function executeAction(address reactive_, address pool_)
        external
        onlyServiceProvider
        onlyCallbackSender(reactive_)
    {
        uint8 action = decisions[pool_].action;

        if (action != ACTION_NONE) {
            require(vault != address(0), "RiskGate: vault not set");
            if (action == ACTION_PAUSE) {
                DemoVault(vault).pause();
            } else if (action == ACTION_TIGHTEN_STOP) {
                DemoVault(vault).tightenStop(TIGHTENED_STOP_BPS);
            } else if (action == ACTION_EXIT_POSITION) {
                DemoVault(vault).exitPosition();
            }
        }

        emit ActionExecuted(pool_, action);
    }

    /// @notice Settle this contract's debt with the callback proxy from its own balance
    ///         (the gate is funded in Base Sepolia ETH, not REACT).
    function coverDebt() external onlyOwner {
        _coverDebt();
    }
}
