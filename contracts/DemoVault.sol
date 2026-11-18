// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

/// @title DemoVault — demo vault holding a dummy DEX position on Base Sepolia.
/// @notice The protective actions (pause / tightenStop / exitPosition) can only be
///         triggered by the RiskGate contract. The owner only configures the vault
///         (sets the gate, re-opens the dummy position for the next demo run).
contract DemoVault {
    address public owner;
    address public gate;

    uint256 public positionSize;
    bool public paused;
    uint256 public stopBps;

    event GateSet(address indexed gate);
    event PositionSet(uint256 size);
    event Paused();
    event StopTightened(uint256 stopBps);
    event PositionExited();

    modifier onlyOwner() {
        require(msg.sender == owner, "DemoVault: not owner");
        _;
    }

    modifier onlyGate() {
        require(msg.sender == gate, "DemoVault: not gate");
        _;
    }

    constructor(uint256 initialPositionSize_, uint256 initialStopBps_) {
        owner = msg.sender;
        positionSize = initialPositionSize_;
        stopBps = initialStopBps_;
    }

    /// @notice Point the vault at its RiskGate — the only allowed caller of the protective actions.
    function setGate(address gate_) external onlyOwner {
        gate = gate_;
        emit GateSet(gate_);
    }

    /// @notice Demo helper: (re)open the dummy position and unpause.
    function setPosition(uint256 size_) external onlyOwner {
        positionSize = size_;
        paused = false;
        emit PositionSet(size_);
    }

    /// @notice Protective action: freeze the position.
    function pause() external onlyGate {
        paused = true;
        emit Paused();
    }

    /// @notice Protective action: tighten the stop-loss threshold, in basis points.
    function tightenStop(uint256 newStopBps_) external onlyGate {
        stopBps = newStopBps_;
        emit StopTightened(newStopBps_);
    }

    /// @notice Protective action: close the position entirely.
    function exitPosition() external onlyGate {
        positionSize = 0;
        emit PositionExited();
    }
}
