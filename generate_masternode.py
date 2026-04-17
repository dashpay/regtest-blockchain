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
LLMQ_TEST_SIZE = 3  # llmq_test (type 100) - 3 members out of 4 MNs
LLMQ_TEST_DIP0024_SIZE = 4  # llmq_test_dip0024 (type 103) - all 4 MNs, minSize=4
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


def _wait_for_quorum_phase(
    network,
    llmq_type_name,
    quorum_hash,
    phase,
    expected_members,
    check_received_messages=None,
    check_received_messages_count=0,
    timeout=60,
):
    """Wait for masternodes to reach a DKG phase with optional message count gating.

    Mirrors Dash Core test_framework.wait_for_quorum_phase: when
    `check_received_messages` is set, a masternode is only counted once its
    session for (llmq_type_name, quorum_hash) reports at least
    `check_received_messages_count` for that field. Without this gate, phase
    transitions advance before contributions/premature commitments have been
    exchanged, producing null DKG commitments.
    """
    start = time.time()
    while time.time() - start < timeout:
        member_count = 0
        for mn in network.masternodes:
            try:
                status = mn.rpc.call("quorum", "dkgstatus")
                for s in status.get("session", []):
                    if s.get("llmqType") != llmq_type_name:
                        continue
                    qs = s.get("status", {})
                    if qs.get("quorumHash") != quorum_hash:
                        continue
                    if qs.get("phase") == phase and (
                        check_received_messages is None
                        or qs.get(check_received_messages, 0) >= check_received_messages_count
                    ):
                        member_count += 1
                    break
            except Exception:
                pass
        if member_count >= expected_members:
            return True
        _bump_mocktime(network, 1)
        time.sleep(0.3)
    return False


def _wait_for_quorum_connections(
    network, llmq_type_name, quorum_hash, expected_members, expected_connections, timeout=60
):
    """Wait for actual TCP connections to be established for the quorum.

    Requires `expected_members` masternodes to each report at least
    `expected_connections` peers in the connected state in their
    `quorumConnections` entry for (llmq_type_name, quorum_hash).
    """
    start = time.time()
    while time.time() - start < timeout:
        ready_members = 0
        for mn in network.masternodes:
            try:
                status = mn.rpc.call("quorum", "dkgstatus")
                sessions = status.get("session", [])
                has_session = any(
                    s.get("llmqType") == llmq_type_name and s.get("status", {}).get("quorumHash") == quorum_hash
                    for s in sessions
                )
                if not has_session:
                    continue

                group = next(
                    (
                        g
                        for g in status.get("quorumConnections", [])
                        if g.get("llmqType") == llmq_type_name and g.get("quorumHash") == quorum_hash
                    ),
                    None,
                )
                if not group:
                    continue

                peers = group.get("quorumConnections", [])
                connected = sum(1 for p in peers if p.get("connected") is True)
                if connected >= expected_connections:
                    ready_members += 1
            except Exception:
                pass
        if ready_members >= expected_members:
            return True
        _bump_mocktime(network, 1)
        time.sleep(0.5)
    return False


def _wait_for_quorum_list(network, llmq_type_name, quorum_hashes, timeout=15):
    """Wait for every hash in `quorum_hashes` to appear in `quorum list` for the type.

    Calls `quorum list` without a count argument so dashd returns up to
    `signingActiveQuorumCount` quorums per type (2 for type 103, 2 for type
    100). Passing `count=1` would only return one entry, which hides the
    second quorum of a rotating DIP-0024 cycle (q_1) even when it was
    successfully mined.
    """
    rpc = network.controller.rpc
    hashes = list(quorum_hashes)
    start = time.time()
    while time.time() - start < timeout:
        try:
            qlist = rpc.call("quorum", "list")
            listed = qlist.get(llmq_type_name, [])
            if all(h in listed for h in hashes):
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
    for _ in range(5):
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
            "protx",
            "register_fund",
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
    for name in [
        "SPORK_17_QUORUM_DKG_ENABLED",
        "SPORK_21_QUORUM_ALL_CONNECTED",
        "SPORK_2_INSTANTSEND_ENABLED",
        "SPORK_19_CHAINLOCKS_ENABLED",
    ]:
        value = sporks.get(name, "unknown")
        print(f"  {name}: {value}")


def _phase_checks_for(phase):
    """Return the (field, count_for_type100, count_for_type103) message gate per phase.

    Follows Dash Core's mine_quorum / mine_cycle_quorum expectations:
    - phase 2 (contribute): receivedContributions == size
    - phase 3 (complain):   receivedComplaints == 0
    - phase 4 (justify):    receivedJustifications == 0
    - phase 5 (commit):     receivedPrematureCommitments == size
    - phases 1/6:           no message gate
    """
    if phase == 2:
        return "receivedContributions", LLMQ_TEST_SIZE, LLMQ_TEST_DIP0024_SIZE
    if phase == 3:
        return "receivedComplaints", 0, 0
    if phase == 4:
        return "receivedJustifications", 0, 0
    if phase == 5:
        return "receivedPrematureCommitments", LLMQ_TEST_SIZE, LLMQ_TEST_DIP0024_SIZE
    return None, 0, 0


def _dump_dkg_status(network, llmq_type_name, quorum_hash):
    """Print per-masternode DKG session state and commitment state for diagnosis."""
    type_num = {"llmq_test": 100, "llmq_test_dip0024": 103}.get(llmq_type_name)
    # Check if the quorum has already been mined - a non-null commitment that lands
    # in a block is removed from `minableCommitments`, so "no entry" can mean either
    # "not yet constructed" or "already committed to chain". Look at `quorum list`
    # on the controller to distinguish.
    try:
        qlist = network.controller.rpc.call("quorum", "list", 10)
        listed_for_type = qlist.get(llmq_type_name, [])
        if quorum_hash in listed_for_type:
            print(f"      controller quorum list: {llmq_type_name} {quorum_hash[:16]} IS LISTED")
        else:
            print(f"      controller quorum list ({llmq_type_name}): {[h[:16] for h in listed_for_type]}")
    except Exception as e:
        print(f"      controller quorum list error: {e}")
    # Also inspect the latest block to see if it contains a non-null commitment.
    try:
        best_hash = network.controller.rpc.call("getbestblockhash")
        block = network.controller.rpc.call("getblock", best_hash, 2)
        height = block.get("height")
        for tx in block.get("tx", []):
            if tx.get("type") == 6 or "qc" in tx or "qcTx" in tx:
                print(f"      tip block {height} has special tx type={tx.get('type')} txid={tx.get('txid', '')[:16]}")
    except Exception as e:
        print(f"      getblock tip error: {e}")
    for mn in network.masternodes:
        try:
            status = mn.rpc.call("quorum", "dkgstatus")
        except Exception as e:
            print(f"      {mn.name}: dkgstatus error: {e}")
            continue
        session = next(
            (
                s
                for s in status.get("session", [])
                if s.get("llmqType") == llmq_type_name and s.get("status", {}).get("quorumHash") == quorum_hash
            ),
            None,
        )
        if session is None:
            print(f"      {mn.name}: no {llmq_type_name} session for {quorum_hash[:16]}")
        else:
            qs = session.get("status", {})
            print(
                f"      {mn.name}: phase={qs.get('phase')} "
                f"sent=c:{qs.get('sentContributions')},co:{qs.get('sentComplaint')},"
                f"j:{qs.get('sentJustification')},pc:{qs.get('sentPrematureCommitment')} "
                f"aborted={qs.get('aborted')} bad={qs.get('badMembers')} "
                f"recv=c:{qs.get('receivedContributions', 0)},co:{qs.get('receivedComplaints', 0)},"
                f"j:{qs.get('receivedJustifications', 0)},pc:{qs.get('receivedPrematureCommitments', 0)}"
            )
        all_commits = status.get("minableCommitments", [])
        print(f"        minableCommitments total={len(all_commits)}")
        for commit in all_commits:
            pk = commit.get("quorumPublicKey", "?")
            mark = (
                " <-- expected"
                if (
                    commit.get("quorumHash") == quorum_hash and (type_num is None or commit.get("llmqType") == type_num)
                )
                else ""
            )
            print(
                f"          type={commit.get('llmqType')} idx={commit.get('quorumIndex')} "
                f"qh={commit.get('quorumHash', '')[:16]} "
                f"signers={commit.get('signersCount')}/{commit.get('validMembersCount')} "
                f"pkHead={pk[:16]}{mark}"
            )
        conn_group = next(
            (
                g
                for g in status.get("quorumConnections", [])
                if g.get("llmqType") == llmq_type_name and g.get("quorumHash") == quorum_hash
            ),
            None,
        )
        if conn_group is not None:
            peers = conn_group.get("quorumConnections", [])
            connected = sum(1 for p in peers if p.get("connected") is True)
            print(f"        quorumConnections: {connected}/{len(peers)} connected")


class DKGCycleError(RuntimeError):
    """Raised when a DKG cycle step fails to complete in time."""


def _require(cond, message, network=None, llmq_type_name=None, quorum_hash=None):
    if cond:
        return
    if network is not None and quorum_hash is not None:
        print(f"\n  Diagnostic for {llmq_type_name} {quorum_hash[:16]}:")
        _dump_dkg_status(network, llmq_type_name or "llmq_test", quorum_hash)
    raise DKGCycleError(message)


def _run_single_dkg_cycle(network, cycle_idx, num_cycles, cycle_quorum_is_ready):
    """Mine one DKG cycle producing real type-100 AND rotating type-103 quorums.

    Aligns to the next DKG boundary, then walks blocks one at a time so the two
    DIP-0024 rotating sessions (q_0 at cycle start, q_1 one block later) can
    be interleaved in phase checks alongside the single llmq_test session.
    All phase transitions are gated on the expected DKG message counts so the
    chain never advances past a phase before real messages have been exchanged.

    On the first call (`cycle_quorum_is_ready=False`), mines 3 extra DKG cycles
    as required by DIP-0024. Per feature_llmq_rotation.py and the `extra_blocks`
    branch in test_framework.mine_cycle_quorum, the first three "quarters" after
    v20/mn_rr activation are built without a DKG session, so the chain must
    advance past H+3C before the first rotating quorum can form.

    Raises DKGCycleError on any timeout or missing quorum.
    """
    rpc = network.controller.rpc

    # Align to the next cycle boundary, with 3-cycle warmup on the first call.
    height = rpc.call("getblockcount")
    skip_count = DKG_INTERVAL - (height % DKG_INTERVAL)
    if skip_count == DKG_INTERVAL:
        skip_count = 0
    warmup_blocks = 0 if cycle_quorum_is_ready else DKG_INTERVAL * 3
    total_move = warmup_blocks + skip_count
    if total_move > 0:
        if warmup_blocks > 0:
            print(f"  DIP-0024 warmup: mining {total_move} blocks before first cycle...")
        _move_blocks(network, total_move)

    q_0 = rpc.call("getbestblockhash")
    height = rpc.call("getblockcount")
    print(f"\n  Cycle {cycle_idx + 1}/{num_cycles} at height {height} q_0={q_0[:16]}...")

    # Phase 1 (init) on q_0 for both types, plus connections
    print("    Phase 1 (init) q_0...", end="", flush=True)
    _require(
        _wait_for_quorum_phase(network, "llmq_test", q_0, 1, LLMQ_TEST_SIZE, timeout=60),
        "phase 1 timeout (llmq_test q_0)",
        network,
        "llmq_test",
        q_0,
    )
    _require(
        _wait_for_quorum_phase(network, "llmq_test_dip0024", q_0, 1, LLMQ_TEST_DIP0024_SIZE, timeout=60),
        "phase 1 timeout (llmq_test_dip0024 q_0)",
        network,
        "llmq_test_dip0024",
        q_0,
    )
    _require(
        _wait_for_quorum_connections(network, "llmq_test", q_0, LLMQ_TEST_SIZE, LLMQ_TEST_SIZE - 1, timeout=60),
        "quorum connection timeout (llmq_test q_0)",
        network,
        "llmq_test",
        q_0,
    )
    _require(
        _wait_for_quorum_connections(
            network, "llmq_test_dip0024", q_0, LLMQ_TEST_DIP0024_SIZE, LLMQ_TEST_DIP0024_SIZE - 1, timeout=60
        ),
        "quorum connection timeout (llmq_test_dip0024 q_0)",
        network,
        "llmq_test_dip0024",
        q_0,
    )
    print(" ok")

    # Advance 1 block -> q_1 (the rotating pair's second quorum) enters phase 1
    _move_blocks(network, 1)
    q_1 = rpc.call("getbestblockhash")
    print(f"    Phase 1 (init) q_1={q_1[:16]}...", end="", flush=True)
    _require(
        _wait_for_quorum_phase(network, "llmq_test_dip0024", q_1, 1, LLMQ_TEST_DIP0024_SIZE, timeout=60),
        "phase 1 timeout (llmq_test_dip0024 q_1)",
        network,
        "llmq_test_dip0024",
        q_1,
    )
    _require(
        _wait_for_quorum_connections(
            network, "llmq_test_dip0024", q_1, LLMQ_TEST_DIP0024_SIZE, LLMQ_TEST_DIP0024_SIZE - 1, timeout=60
        ),
        "quorum connection timeout (llmq_test_dip0024 q_1)",
        network,
        "llmq_test_dip0024",
        q_1,
    )
    print(" ok")

    # Walk phases 2-6 block-by-block, alternating q_0 and q_1 checks.
    # At each even block of the cycle, q_0 enters the next phase (together
    # with the llmq_test session). At each odd block, q_1 enters it.
    for phase in range(2, 7):
        field, count_test, count_dip0024 = _phase_checks_for(phase)
        _move_blocks(network, 1)  # enter phase on q_0 + type 100

        phase_name = {2: "contribute", 3: "complain", 4: "justify", 5: "commit", 6: "finalize"}[phase]
        print(f"    Phase {phase} ({phase_name}) q_0...", end="", flush=True)
        _require(
            _wait_for_quorum_phase(network, "llmq_test", q_0, phase, LLMQ_TEST_SIZE, field, count_test, timeout=45),
            f"phase {phase} timeout (llmq_test q_0)",
            network,
            "llmq_test",
            q_0,
        )
        _require(
            _wait_for_quorum_phase(
                network, "llmq_test_dip0024", q_0, phase, LLMQ_TEST_DIP0024_SIZE, field, count_dip0024, timeout=45
            ),
            f"phase {phase} timeout (llmq_test_dip0024 q_0)",
            network,
            "llmq_test_dip0024",
            q_0,
        )
        print(" ok", end="", flush=True)

        _move_blocks(network, 1)  # enter phase on q_1
        print("  q_1...", end="", flush=True)
        _require(
            _wait_for_quorum_phase(
                network, "llmq_test_dip0024", q_1, phase, LLMQ_TEST_DIP0024_SIZE, field, count_dip0024, timeout=45
            ),
            f"phase {phase} timeout (llmq_test_dip0024 q_1)",
            network,
            "llmq_test_dip0024",
            q_1,
        )
        print(" ok")

    # Mine the commit block. At cycle+12 the controller creates a block that
    # includes the real finalCommitment txs for type 100 (window [cycle+10,
    # cycle+18]) and for both type 103 rotating quorums (window [cycle+12,
    # cycle+20]). Mirrors the final mining step in test_framework's
    # mine_cycle_quorum (getblocktemplate + generate(1)).
    _bump_mocktime(network, 1)
    rpc.call("getblocktemplate")
    _move_blocks(network, 1)

    # Confirm all three real quorums were recorded by the chain. `quorum list`
    # reflects commitments stored in evoDB by ProcessCommitment, which only
    # writes non-null commitments. If any quorum is missing, its commitment
    # was null in the mined block.
    _require(
        _wait_for_quorum_list(network, "llmq_test", [q_0], timeout=15),
        "llmq_test q_0 missing from quorum list after commit block",
        network,
        "llmq_test",
        q_0,
    )
    _require(
        _wait_for_quorum_list(network, "llmq_test_dip0024", [q_0, q_1], timeout=15),
        "llmq_test_dip0024 q_0/q_1 missing from quorum list after commit block",
        network,
        "llmq_test_dip0024",
        q_0,
    )

    # Mine 8 blocks (SIGN_HEIGHT_OFFSET) for signing-window maturity, matching
    # the tail of test_framework.mine_cycle_quorum.
    _move_blocks(network, 8)

    qlist = rpc.call("quorum", "list")
    print(
        f"    Cycle complete: llmq_test={len(qlist.get('llmq_test', []))}, "
        f"llmq_test_dip0024={len(qlist.get('llmq_test_dip0024', []))}"
    )


def phase_5_mine_dkg_cycles(network, num_cycles):
    """Mine `num_cycles` combined DKG cycles (type 100 + rotating type 103).

    Each cycle is gated on real DKG message exchange, so every produced
    commitment has signers>0, validMembers>0, and a non-zero quorumPublicKey.
    """
    print("\n" + "=" * 60)
    print(f"Phase 5: Mine {num_cycles} DKG cycles")
    print("=" * 60)

    completed_cycles = 0
    cycle_quorum_is_ready = False
    for cycle in range(num_cycles):
        _run_single_dkg_cycle(network, cycle, num_cycles, cycle_quorum_is_ready)
        # Warmup is applied on the first call; subsequent cycles skip it.
        cycle_quorum_is_ready = True
        completed_cycles += 1

    rpc = network.controller.rpc
    height = rpc.call("getblockcount")
    quorum_list = rpc.call("quorum", "list")
    print(f"\n  Completed {completed_cycles}/{num_cycles} DKG cycles (height: {height})")
    print(
        f"  Quorums: llmq_test={len(quorum_list.get('llmq_test', []))}, "
        f"llmq_test_dip0024={len(quorum_list.get('llmq_test_dip0024', []))}"
    )
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
