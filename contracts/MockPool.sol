// SPDX-License-Identifier: MIT
pragma solidity ^0.8.29;

/// @title MockPool — dummy DEX pool that exists only as an event source for the demo.
/// @notice Anyone can call `swap()` to simulate a trade; SentinelReactive subscribes
///         to the emitted `Swap` events on Base Sepolia. No funds ever move here.
contract MockPool {
    /// @notice Emitted on every simulated swap.
    /// @param sender Whoever triggered the swap.
    /// @param amountDeltaBps Signed price move caused by the swap, in basis points.
    /// @param volume Traded volume, in dummy units.
    event Swap(address indexed sender, int256 amountDeltaBps, uint256 volume);

    /// @notice Simulate a trade. Only emits the event — that is the whole point.
    function swap(int256 amountDeltaBps, uint256 volume) external {
        emit Swap(msg.sender, amountDeltaBps, volume);
    }
}
