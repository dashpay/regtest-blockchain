#!/usr/bin/env python3
"""
Dash Masternode Network Test Data Generator

Generates a regtest blockchain with an active masternode network for SPV
masternode list sync testing. Follows the same setup sequence as Dash Core's
test framework (test_framework.py).

Produces 5 node datadirs (1 controller + 4 MNs) with completed DKG cycles.

Usage:
    python3 generate_masternode.py --dashd-path /path/to/dashd
    python3 generate_masternode.py --dashd-path /path/to/dashd --dkg-cycles 12
"""

import argparse
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from generator.errors import RPCError
from generator.masternode_network import MasternodeNetwork
from generator.wallet_export import collect_wallet_stats, save_wallet_file

# Constants matching Dash Core test framework
TIME_GENESIS_BLOCK = 1417713337
DKG_INTERVAL = 24
NUM_MASTERNODES = 4
SPORK_PRIVATE_KEY = "cP4EKFyJsHT39LDqgdcB43Y3YXjNyjb5Fuas1GQSeAtjnZWmZEQK"

DASHD_EXTRA_ARGS = [
    "-dip3params=2:2",
    "-testactivationheight=v20@100",
    "-testactivationheight=mn_rr@100",
]


@dataclass
class MasternodeConfig:
    dashd_path: str
    dkg_cycles: int
    output_dir: str


def _all_nodes(network):
    return [network.controller] + network.masternodes


def _set_mocktime(network, mocktime):
    """Set mocktime on all nodes."""
    network._mocktime = mocktime
    for node in _all_nodes(network):
        try:
            node.rpc.call("setmocktime", mocktime)
        except Exception:
            pass


def _bump_mocktime(network, seconds=1):
    """Advance mocktime and run mockscheduler on all nodes."""
    network._mocktime += seconds
    for node in _all_nodes(network):
        try:
            node.rpc.call("setmocktime", network._mocktime)
            node.rpc.call("mockscheduler", seconds)
        except Exception:
            pass


def _move_blocks(network, count):
    """Bump mocktime, generate blocks, and wait for sync."""
    if count <= 0:
        return
    _bump_mocktime(network, 1)
    network.generate_blocks(count)
    network.wait_for_sync()


def _force_finish_mnsync(node):
    """Force a node to finish mnsync (masternodes reject connections until synced)."""
    for _ in range(20):
        try:
            status = node.rpc.call("mnsync", "status")
            if status.get("IsSynced", False):
                return
            node.rpc.call("mnsync", "next")
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)


def _wait_for_quorum_phase(network, quorum_hash, phase, expected_members=None, timeout=60):
    """Wait for masternodes to reach a DKG phase."""
    if expected_members is None:
        expected_members = len(network.masternodes)
    start = time.time()
    while time.time() - start < timeout:
        member_count = 0
        for mn in network.masternodes:
            try:
                status = mn.rpc.call("quorum", "dkgstatus")
                for s in status.get("session", []):
                    if s.get("llmqType") != "llmq_test":
                        continue
                    qs = s.get("status", {})
                    if qs.get("quorumHash") == quorum_hash and qs.get("phase") == phase:
                        member_count += 1
                        break
            except Exception:
                pass
        if member_count >= expected_members:
            return True
        _bump_mocktime(network, 1)
        time.sleep(0.3)
    return False


def _wait_for_quorum_connections(network, quorum_hash, timeout=60):
    """Wait for masternodes to establish quorum connections."""
    start = time.time()
    while time.time() - start < timeout:
        all_connected = True
        for mn in network.masternodes:
            try:
                status = mn.rpc.call("quorum", "dkgstatus")
                found_session = False
                for s in status.get("session", []):
                    if s.get("llmqType") != "llmq_test":
                        continue
                    if s.get("status", {}).get("quorumHash") == quorum_hash:
                        found_session = True
                        break
                if not found_session:
                    all_connected = False
                    break
            except Exception:
                all_connected = False
                break
        if all_connected:
            return True
        _bump_mocktime(network, 1)
        time.sleep(0.3)
    return False


def _wait_for_quorum_commitment(network, quorum_hash, timeout=30):
    """Wait for minable commitments on all masternodes."""
    start = time.time()
    while time.time() - start < timeout:
        all_ready = True
        for mn in network.masternodes:
            try:
                status = mn.rpc.call("quorum", "dkgstatus")
                found = False
                for c in status.get("minableCommitments", []):
                    if c.get("llmqType") == 100 and c.get("quorumHash") == quorum_hash:
                        if c.get("quorumPublicKey", "0" * 96) != "0" * 96:
                            found = True
                            break
                if not found:
                    all_ready = False
                    break
            except Exception:
                all_ready = False
                break
        if all_ready:
            return True
        time.sleep(0.5)
    return False


def _wait_for_quorum_list(network, quorum_hash, timeout=15):
    """Wait for quorum to appear in the quorum list."""
    rpc = network.controller.rpc
    start = time.time()
    while time.time() - start < timeout:
        try:
            qlist = rpc.call("quorum", "list", 1)
            if quorum_hash in qlist.get("llmq_test", []):
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def phase_1_bootstrap(network):
    """Bootstrap: create wallet, mine initial blocks, initialize mocktime."""
    print("\n" + "=" * 60)
    print("Phase 1: Bootstrap")
    print("=" * 60)

    rpc = network.controller.rpc

    # Initialize mocktime on controller (matches Dash Core test framework)
    network._mocktime = TIME_GENESIS_BLOCK
    rpc.call("setmocktime", network._mocktime)

    # Create the SPV test wallet
    try:
        rpc.call("createwallet", "wallet")
        print("  Created 'wallet' wallet")
    except RPCError as e:
        if "already exists" in str(e).lower():
            rpc.call("loadwallet", "wallet")
            print("  Loaded existing 'wallet' wallet")
        else:
            raise

    # Get a funding address for protx registration
    network.fund_address = rpc.call("getnewaddress")

    # Mine blocks for maturity + activation (need coins to be spendable)
    # Mine in batches with mocktime bumps (like Dash Core's cache setup)
    for batch in range(5):
        _bump_mocktime(network, 25 * 156)
        rpc.call("generatetoaddress", 25, network.fund_address)

    height = rpc.call("getblockcount")
    balance = rpc.call("getbalance")
    print(f"  Mined to height {height}, balance: {balance}")

    blockchain_info = rpc.call("getblockchaininfo")
    print(f"  Softforks: {list(blockchain_info.get('softforks', {}).keys())}")


def phase_2_register_masternodes(network):
    """Register masternodes via protx register_fund."""
    print("\n" + "=" * 60)
    print("Phase 2: Register masternodes")
    print("=" * 60)

    rpc = network.controller.rpc
    mn_ports = network.allocate_mn_ports()

    for i in range(NUM_MASTERNODES):
        mn_name = f"mn{i + 1}"
        service_addr = f"127.0.0.1:{mn_ports[i]}"
        print(f"\n  Registering {mn_name} (service: {service_addr})...")

        bls_result = rpc.call("bls", "generate")
        bls_public = bls_result["public"]
        bls_secret = bls_result["secret"]

        owner_addr = rpc.call("getnewaddress")
        voting_addr = rpc.call("getnewaddress")
        payout_addr = rpc.call("getnewaddress")
        collateral_addr = rpc.call("getnewaddress")

        # register_fund: collateral and fees come from fund_address
        pro_tx_hash = rpc.call(
            "protx", "register_fund",
            collateral_addr,
            [service_addr],
            owner_addr,
            bls_public,
            voting_addr,
            0,
            payout_addr,
            network.fund_address,
        )

        # Bury the protx (1 confirmation)
        _bump_mocktime(network, 601)
        rpc.call("generatetoaddress", 1, network.fund_address)

        mn_info = {
            "index": i,
            "name": mn_name,
            "pro_tx_hash": pro_tx_hash,
            "bls_public_key": bls_public,
            "bls_private_key": bls_secret,
            "owner_address": owner_addr,
            "voting_address": voting_addr,
            "payout_address": payout_addr,
        }
        network.masternode_info.append(mn_info)
        print(f"    proTxHash: {pro_tx_hash}")

    height = rpc.call("getblockcount")
    print(f"\n  All {NUM_MASTERNODES} masternodes registered (height: {height})")


def phase_3_start_masternodes(network):
    """Copy datadirs, start MN nodes, force mnsync, then connect.

    Follows the exact ordering from Dash Core's test framework:
    1. Start nodes (with mocktime on command line)
    2. Set mocktime via RPC
    3. Force mnsync completion (masternodes reject connections until synced)
    4. Connect nodes to each other
    """
    print("\n" + "=" * 60)
    print("Phase 3: Start masternode nodes")
    print("=" * 60)

    # Start all nodes (does not connect them yet)
    network.start_masternode_nodes(network.controller.datadir)

    # Re-load the "wallet" wallet (lost during controller restart)
    rpc = network.controller.rpc
    try:
        rpc.call("loadwallet", "wallet")
    except RPCError as e:
        if "already loaded" not in str(e).lower():
            raise

    # Set mocktime on all nodes via RPC (in addition to -mocktime= cmd arg)
    _set_mocktime(network, network._mocktime)

    # Force mnsync on all nodes (must happen before connecting)
    print("  Forcing mnsync completion on controller...")
    _force_finish_mnsync(network.controller)
    for mn in network.masternodes:
        print(f"  Forcing mnsync completion on {mn.name}...")
        _force_finish_mnsync(mn)
    print("  All nodes mnsync complete")

    # Now connect all nodes (mnsync must be done first)
    print("  Connecting nodes...")
    network.connect_all()

    # Mine 8 blocks for masternode maturity
    _move_blocks(network, 8)

    # Verify masternode status
    mn_list = rpc.call("masternode", "list")
    enabled_count = sum(1 for v in mn_list.values() if "ENABLED" in str(v))
    print(f"  Masternodes ENABLED: {enabled_count}/{NUM_MASTERNODES}")

    if enabled_count < NUM_MASTERNODES:
        for _ in range(10):
            _move_blocks(network, 4)
            time.sleep(1)
            mn_list = rpc.call("masternode", "list")
            enabled_count = sum(1 for v in mn_list.values() if "ENABLED" in str(v))
            if enabled_count >= NUM_MASTERNODES:
                break
        print(f"  Final ENABLED count: {enabled_count}/{NUM_MASTERNODES}")

    height = rpc.call("getblockcount")
    print(f"  Height after MN maturity: {height}")


def phase_4_enable_sporks(network):
    """Enable sporks for DKG, InstantSend, ChainLocks."""
    print("\n" + "=" * 60)
    print("Phase 4: Enable sporks")
    print("=" * 60)

    rpc = network.controller.rpc

    rpc.call("sporkupdate", "SPORK_17_QUORUM_DKG_ENABLED", 0)
    rpc.call("sporkupdate", "SPORK_21_QUORUM_ALL_CONNECTED", 0)
    rpc.call("sporkupdate", "SPORK_2_INSTANTSEND_ENABLED", 0)
    rpc.call("sporkupdate", "SPORK_3_INSTANTSEND_BLOCK_FILTERING", 0)
    rpc.call("sporkupdate", "SPORK_19_CHAINLOCKS_ENABLED", 0)

    # Wait for spork propagation
    time.sleep(3)
    _bump_mocktime(network, 1)

    sporks = rpc.call("spork", "show")
    for name in ["SPORK_17_QUORUM_DKG_ENABLED", "SPORK_21_QUORUM_ALL_CONNECTED",
                  "SPORK_2_INSTANTSEND_ENABLED", "SPORK_19_CHAINLOCKS_ENABLED"]:
        value = sporks.get(name, "unknown")
        print(f"  {name}: {value}")


def phase_5_mine_dkg_cycles(network, num_cycles):
    """Mine DKG cycles following Dash Core's mine_quorum() pattern."""
    print("\n" + "=" * 60)
    print(f"Phase 5: Mine {num_cycles} DKG cycles")
    print("=" * 60)

    rpc = network.controller.rpc
    completed_cycles = 0
    # LLMQ_TEST has 3 members out of our 4 MNs
    expected_members = 3

    for cycle in range(num_cycles):
        height = rpc.call("getblockcount")

        # Move to next DKG cycle start
        skip_count = DKG_INTERVAL - (height % DKG_INTERVAL)
        if skip_count != 0 and skip_count != DKG_INTERVAL:
            _bump_mocktime(network, 1)
            network.generate_blocks(skip_count)
            network.wait_for_sync()

        quorum_hash = rpc.call("getbestblockhash")
        height = rpc.call("getblockcount")
        print(f"\n  Cycle {cycle + 1}/{num_cycles} at height {height}")

        # Phase 1: Init - wait for quorum connections
        print("    Phase 1 (init)...", end="", flush=True)
        if not _wait_for_quorum_phase(network, quorum_hash, 1, expected_members, timeout=60):
            print(" timeout")
            # Debug output
            for mn in network.masternodes:
                try:
                    status = mn.rpc.call("quorum", "dkgstatus")
                    sessions = status.get("session", [])
                    print(f"      {mn.name}: time={status.get('time')}, sessions={len(sessions)}, "
                          f"tip={status.get('session', [{}])[0].get('status', {}).get('quorumHash', 'none')[:16] if sessions else 'none'}")
                except Exception as e:
                    print(f"      {mn.name}: error {e}")
            _move_blocks(network, DKG_INTERVAL)
            continue
        _wait_for_quorum_connections(network, quorum_hash, timeout=60)
        print(" ok")
        _move_blocks(network, 2)

        # Phase 2: Contribute
        print("    Phase 2 (contribute)...", end="", flush=True)
        if not _wait_for_quorum_phase(network, quorum_hash, 2, expected_members, timeout=30):
            print(" timeout")
            _move_blocks(network, DKG_INTERVAL)
            continue
        print(" ok")
        _move_blocks(network, 2)

        # Phase 3: Complain
        print("    Phase 3 (complain)...", end="", flush=True)
        if not _wait_for_quorum_phase(network, quorum_hash, 3, expected_members, timeout=30):
            print(" timeout")
            _move_blocks(network, DKG_INTERVAL)
            continue
        print(" ok")
        _move_blocks(network, 2)

        # Phase 4: Justify
        print("    Phase 4 (justify)...", end="", flush=True)
        if not _wait_for_quorum_phase(network, quorum_hash, 4, expected_members, timeout=30):
            print(" timeout")
            _move_blocks(network, DKG_INTERVAL)
            continue
        print(" ok")
        _move_blocks(network, 2)

        # Phase 5: Commit
        print("    Phase 5 (commit)...", end="", flush=True)
        if not _wait_for_quorum_phase(network, quorum_hash, 5, expected_members, timeout=30):
            print(" timeout")
            _move_blocks(network, DKG_INTERVAL)
            continue
        print(" ok")
        _move_blocks(network, 2)

        # Phase 6: Mining
        print("    Phase 6 (mining)...", end="", flush=True)
        if not _wait_for_quorum_phase(network, quorum_hash, 6, expected_members, timeout=30):
            print(" timeout")
            _move_blocks(network, DKG_INTERVAL)
            continue
        print(" ok")

        # Wait for final commitment
        print("    Waiting for commitment...", end="", flush=True)
        if not _wait_for_quorum_commitment(network, quorum_hash, timeout=30):
            print(" timeout")
            _move_blocks(network, DKG_INTERVAL)
            continue
        print(" ok")

        # Mine the commitment block (getblocktemplate triggers CreateNewBlock)
        _bump_mocktime(network, 1)
        rpc.call("getblocktemplate")
        _move_blocks(network, 1)

        # Verify quorum appeared in the list
        if _wait_for_quorum_list(network, quorum_hash, timeout=15):
            # Mine 8 blocks for quorum maturity
            _move_blocks(network, 8)
            completed_cycles += 1
            total = len(rpc.call("quorum", "list").get("llmq_test", []))
            print(f"    Quorum formed (total: {total})")
        else:
            print(f"    Quorum not in list")

    height = rpc.call("getblockcount")
    quorum_list = rpc.call("quorum", "list")
    print(f"\n  Completed {completed_cycles}/{num_cycles} DKG cycles (height: {height})")
    print(f"  Quorums: llmq_test={len(quorum_list.get('llmq_test', []))}, "
          f"llmq_test_dip0024={len(quorum_list.get('llmq_test_dip0024', []))}")
    return completed_cycles


def phase_6_generate_test_transactions(network):
    """Send transactions to the SPV test wallet."""
    print("\n" + "=" * 60)
    print("Phase 6: Generate SPV test transactions")
    print("=" * 60)

    rpc = network.controller.rpc

    addresses = []
    for _ in range(10):
        addr = rpc.call("getnewaddress", wallet="wallet")
        addresses.append(addr)

    amounts = [1.0, 5.0, 10.0, 0.5, 25.0, 0.1, 50.0, 2.5]
    for i, amount in enumerate(amounts):
        addr = addresses[i % len(addresses)]
        rpc.call("sendtoaddress", addr, amount)
        if (i + 1) % 3 == 0:
            _move_blocks(network, 1)

    _move_blocks(network, 6)

    height = rpc.call("getblockcount")
    print(f"  Generated {len(amounts)} test transactions (height: {height})")


def phase_7_export(network, config, dkg_cycles_completed):
    """Export all node data and metadata."""
    print("\n" + "=" * 60)
    print("Phase 7: Export")
    print("=" * 60)

    rpc = network.controller.rpc
    chain_height = rpc.call("getblockcount")

    output_dir = Path(config.output_dir) / "regtest-mn-v0.0.1"
    if output_dir.exists():
        print(f"  Removing existing output: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    # Collect wallet stats while nodes are running
    print("  Collecting wallet statistics...")
    wallet_stats = collect_wallet_stats(rpc, "wallet")
    wallets_dir = output_dir / "wallets"
    wallets_dir.mkdir()
    save_wallet_file(wallet_stats, wallets_dir / "wallet.json")
    print(f"    wallet.json: {len(wallet_stats['transactions'])} txs, balance: {wallet_stats['balance']:.8f}")

    # Stop all nodes cleanly
    print("  Stopping all nodes...")
    network.stop_all()
    time.sleep(2)

    # Copy datadirs
    print("  Copying controller datadir...")
    controller_dest = output_dir / "controller"
    shutil.copytree(network.controller.datadir / "regtest", controller_dest / "regtest")

    for i, mn in enumerate(network.masternodes):
        mn_name = f"mn{i + 1}"
        print(f"  Copying {mn_name} datadir...")
        mn_dest = output_dir / mn_name
        shutil.copytree(mn.datadir / "regtest", mn_dest / "regtest")

    # Write network.json
    network_metadata = {
        "version": "0.0.1",
        "chain_height": chain_height,
        "dkg_cycles_completed": dkg_cycles_completed,
        "dkg_interval": DKG_INTERVAL,
        "controller": {
            "datadir": "controller",
            "wallet": "wallet",
        },
        "masternodes": [
            {
                "index": mn["index"],
                "datadir": mn["name"],
                "pro_tx_hash": mn["pro_tx_hash"],
                "bls_private_key": mn["bls_private_key"],
                "bls_public_key": mn["bls_public_key"],
                "owner_address": mn["owner_address"],
                "voting_address": mn["voting_address"],
                "payout_address": mn["payout_address"],
            }
            for mn in network.masternode_info
        ],
        "spork_private_key": SPORK_PRIVATE_KEY,
        "dashd_extra_args": DASHD_EXTRA_ARGS,
    }

    with open(output_dir / "network.json", "w") as f:
        json.dump(network_metadata, f, indent=2)

    total_size = sum(f.stat().st_size for f in output_dir.rglob("*") if f.is_file())
    print(f"\n  Exported to {output_dir}")
    print(f"  Total size: {total_size / 1024 / 1024:.1f} MB")
    print(f"  Chain height: {chain_height}")
    print(f"  DKG cycles: {dkg_cycles_completed}")

    return output_dir


def main():
    parser = argparse.ArgumentParser(description="Generate masternode network test data")
    parser.add_argument("--dashd-path", required=True, help="Path to dashd binary")
    parser.add_argument("--dkg-cycles", type=int, default=8, help="Number of DKG cycles (default: 8)")
    parser.add_argument("--output-dir", default=str(Path(__file__).parent / "data"), help="Output directory")
    args = parser.parse_args()

    config = MasternodeConfig(
        dashd_path=args.dashd_path,
        dkg_cycles=args.dkg_cycles,
        output_dir=args.output_dir,
    )

    dashd_bin = Path(config.dashd_path)
    if not dashd_bin.exists():
        print(f"dashd not found: {dashd_bin}")
        sys.exit(1)

    extra_args = list(DASHD_EXTRA_ARGS)
    extra_args.append(f"-sporkkey={SPORK_PRIVATE_KEY}")

    network = MasternodeNetwork(
        dashd_path=config.dashd_path,
        num_masternodes=NUM_MASTERNODES,
        base_extra_args=extra_args,
    )

    try:
        # Set initial mocktime on network object before any node starts
        network._mocktime = TIME_GENESIS_BLOCK

        network.start_controller(extra_args=[f"-mocktime={TIME_GENESIS_BLOCK}"])
        phase_1_bootstrap(network)
        phase_2_register_masternodes(network)
        phase_3_start_masternodes(network)
        phase_4_enable_sporks(network)
        dkg_cycles = phase_5_mine_dkg_cycles(network, config.dkg_cycles)
        phase_6_generate_test_transactions(network)
        output_dir = phase_7_export(network, config, dkg_cycles)

        print("\n" + "=" * 60)
        print("Generation complete!")
        print("=" * 60)
        print(f"Output: {output_dir}")

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nGeneration failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        network.cleanup()


if __name__ == "__main__":
    main()
