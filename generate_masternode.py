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

# Maps llmq_type_name -> numeric llmq type (matches Dash Core consensus enum).
LLMQ_TYPE_NUM = {"llmq_test": 100, "llmq_test_dip0024": 103}

# Message-count gates per DKG phase. Phase 2/5 require a message from every
# member; phases 3/4 expect zero in a healthy cycle (no complaints/justifications).
# Phases 1/6 have no message gate. Mirrors Dash Core's mine_quorum /
# mine_cycle_quorum expectations.
PHASE_GATES = {
    2: ("receivedContributions", LLMQ_TEST_SIZE, LLMQ_TEST_DIP0024_SIZE),
    3: ("receivedComplaints", 0, 0),
    4: ("receivedJustifications", 0, 0),
    5: ("receivedPrematureCommitments", LLMQ_TEST_SIZE, LLMQ_TEST_DIP0024_SIZE),
}
PHASE_NAMES = {1: "init", 2: "contribute", 3: "complain", 4: "justify", 5: "commit", 6: "finalize"}


class DKGCycleError(RuntimeError):
    """Raised when a DKG cycle step fails to complete in time."""


def _find_session(status, llmq_type_name, quorum_hash):
    """Return the dkgstatus session entry for `(llmq_type_name, quorum_hash)`, or None."""
    for s in status.get("session", []):
        if s.get("llmqType") != llmq_type_name:
            continue
        if s.get("status", {}).get("quorumHash") != quorum_hash:
            continue
        return s
    return None


def _find_connection_group(status, llmq_type_name, quorum_hash):
    """Return the quorumConnections entry for `(llmq_type_name, quorum_hash)`, or None."""
    for g in status.get("quorumConnections", []):
        if g.get("llmqType") == llmq_type_name and g.get("quorumHash") == quorum_hash:
            return g
    return None


def _dump_dkg_status(network, llmq_type_name, quorum_hash):
    """Print per-masternode DKG session and commitment state for failure diagnosis."""
    type_num = LLMQ_TYPE_NUM.get(llmq_type_name)
    try:
        qlist = network.controller.rpc.call("quorum", "list", 10)
        listed = qlist.get(llmq_type_name, [])
        if quorum_hash in listed:
            print(f"      controller quorum list: {llmq_type_name} {quorum_hash[:16]} IS LISTED")
        else:
            print(f"      controller quorum list ({llmq_type_name}): {[h[:16] for h in listed]}")
    except Exception as e:
        print(f"      controller quorum list error: {e}")

    try:
        best_hash = network.controller.rpc.call("getbestblockhash")
        block = network.controller.rpc.call("getblock", best_hash, 2)
        height = block.get("height")
        for tx in block.get("tx", []):
            if tx.get("type") == 6:
                print(f"      tip block {height} qc tx txid={tx.get('txid', '')[:16]}")
    except Exception as e:
        print(f"      getblock tip error: {e}")

    for mn in network.masternodes:
        try:
            status = mn.rpc.call("quorum", "dkgstatus")
        except Exception as e:
            print(f"      {mn.name}: dkgstatus error: {e}")
            continue
        session = _find_session(status, llmq_type_name, quorum_hash)
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
        commits = status.get("minableCommitments", [])
        print(f"        minableCommitments total={len(commits)}")
        for commit in commits:
            pk = commit.get("quorumPublicKey", "?")
            match = commit.get("quorumHash") == quorum_hash and (type_num is None or commit.get("llmqType") == type_num)
            print(
                f"          type={commit.get('llmqType')} idx={commit.get('quorumIndex')} "
                f"qh={commit.get('quorumHash', '')[:16]} "
                f"signers={commit.get('signersCount')}/{commit.get('validMembersCount')} "
                f"pkHead={pk[:16]}{' <-- expected' if match else ''}"
            )
        conn_group = _find_connection_group(status, llmq_type_name, quorum_hash)
        if conn_group is not None:
            peers = conn_group.get("quorumConnections", [])
            connected = sum(1 for p in peers if p.get("connected") is True)
            print(f"        quorumConnections: {connected}/{len(peers)} connected")


def _raise_with_diagnostic(network, message, llmq_type_name, quorum_hash):
    print(f"\n  Diagnostic for {llmq_type_name} {quorum_hash[:16]}:")
    _dump_dkg_status(network, llmq_type_name, quorum_hash)
    raise DKGCycleError(message)


def wait_for_quorum_phase(
    network,
    llmq_type_name,
    quorum_hash,
    phase,
    expected_members,
    check_received_messages=None,
    check_received_messages_count=0,
    timeout=60,
):
    """Wait for `expected_members` masternodes to reach a DKG phase.

    When `check_received_messages` is set, a masternode is only counted once
    its session for (llmq_type_name, quorum_hash) reports at least
    `check_received_messages_count` for that field. Without this gate, phase
    transitions advance before the DKG messages have been exchanged,
    producing null commitments.

    Mirrors Dash Core test_framework.wait_for_quorum_phase. Raises
    DKGCycleError on timeout.
    """
    start = time.time()
    while time.time() - start < timeout:
        member_count = 0
        for mn in network.masternodes:
            try:
                status = mn.rpc.call("quorum", "dkgstatus")
            except Exception:
                continue
            session = _find_session(status, llmq_type_name, quorum_hash)
            if session is None:
                continue
            qs = session.get("status", {})
            if qs.get("phase") != phase:
                continue
            if (
                check_received_messages is not None
                and qs.get(check_received_messages, 0) < check_received_messages_count
            ):
                continue
            member_count += 1
        if member_count >= expected_members:
            return
        network.bump_mocktime(1)
        time.sleep(0.3)
    _raise_with_diagnostic(
        network,
        f"phase {phase} timeout ({llmq_type_name} {quorum_hash[:16]})",
        llmq_type_name,
        quorum_hash,
    )


def wait_for_quorum_connections(network, llmq_type_name, quorum_hash, expected_members, timeout=60):
    """Wait until `expected_members` masternodes report the DKG peer mesh is up.

    With SPORK_21_QUORUM_ALL_CONNECTED active each member expects `size - 1`
    connected peers. Raises DKGCycleError on timeout.
    """
    expected_connections = expected_members - 1
    start = time.time()
    while time.time() - start < timeout:
        ready = 0
        for mn in network.masternodes:
            try:
                status = mn.rpc.call("quorum", "dkgstatus")
            except Exception:
                continue
            if _find_session(status, llmq_type_name, quorum_hash) is None:
                continue
            group = _find_connection_group(status, llmq_type_name, quorum_hash)
            if group is None:
                continue
            peers = group.get("quorumConnections", [])
            if sum(1 for p in peers if p.get("connected") is True) >= expected_connections:
                ready += 1
        if ready >= expected_members:
            return
        network.bump_mocktime(1)
        time.sleep(0.5)
    _raise_with_diagnostic(
        network,
        f"quorum connection timeout ({llmq_type_name} {quorum_hash[:16]})",
        llmq_type_name,
        quorum_hash,
    )


def wait_for_quorum_list(network, llmq_type_name, quorum_hashes, timeout=15):
    """Wait until every hash in `quorum_hashes` appears in `quorum list` for the type.

    `quorum list` is called without a count argument so dashd returns up to
    `signingActiveQuorumCount` quorums per type (2 for rotating types).
    Passing `count=1` would hide q_1 of a DIP-0024 cycle. Raises
    DKGCycleError on timeout.
    """
    rpc = network.controller.rpc
    hashes = list(quorum_hashes)
    start = time.time()
    while time.time() - start < timeout:
        try:
            qlist = rpc.call("quorum", "list")
            listed = qlist.get(llmq_type_name, [])
            if all(h in listed for h in hashes):
                return
        except Exception:
            pass
        time.sleep(0.3)
    _raise_with_diagnostic(
        network,
        f"{llmq_type_name} {[h[:16] for h in hashes]} missing from quorum list",
        llmq_type_name,
        hashes[0],
    )


def phase_1_bootstrap(network):
    """Bootstrap: create wallet, mine initial blocks, initialize mocktime."""
    print("\n" + "=" * 60)
    print("Phase 1: Bootstrap")
    print("=" * 60)

    rpc = network.controller.rpc
    network.set_mocktime(TIME_GENESIS_BLOCK)

    try:
        rpc.call("createwallet", "wallet")
        print("  Created 'wallet' wallet")
    except RPCError as e:
        if "already exists" in str(e).lower():
            rpc.call("loadwallet", "wallet")
            print("  Loaded existing 'wallet' wallet")
        else:
            raise

    # Funding address for mining rewards + later protx registration fees.
    network.fund_address = rpc.call("getnewaddress")

    # Mine in batches with mocktime bumps (like Dash Core's cache setup) to
    # accumulate spendable coins past the coinbase maturity window.
    for _ in range(5):
        network.bump_mocktime(25 * 156)
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

        bls = rpc.call("bls", "generate")
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
            bls["public"],
            voting_addr,
            0,
            payout_addr,
            network.fund_address,
        )

        # Bury the protx (1 confirmation)
        network.bump_mocktime(601)
        rpc.call("generatetoaddress", 1, network.fund_address)

        network.masternode_info.append(
            {
                "index": i,
                "name": mn_name,
                "pro_tx_hash": pro_tx_hash,
                "bls_public_key": bls["public"],
                "bls_private_key": bls["secret"],
                "owner_address": owner_addr,
                "voting_address": voting_addr,
                "payout_address": payout_addr,
            }
        )
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

    network.start_masternode_nodes()

    # Re-load the "wallet" wallet (lost during controller restart).
    rpc = network.controller.rpc
    try:
        rpc.call("loadwallet", "wallet")
    except RPCError as e:
        if "already loaded" not in str(e).lower():
            raise

    # Re-apply mocktime via RPC on every node (the cmdline `-mocktime=` only
    # seeds it; RPC `setmocktime` is what the DKG scheduler consults).
    network.set_mocktime(network.mocktime)

    print("  Forcing mnsync completion on controller...")
    network.controller.force_finish_mnsync()
    for mn in network.masternodes:
        print(f"  Forcing mnsync completion on {mn.name}...")
        mn.force_finish_mnsync()
    print("  All nodes mnsync complete")

    print("  Connecting nodes...")
    network.connect_all()

    # Mine 8 blocks for masternode maturity
    network.move_blocks(8)

    mn_list = rpc.call("masternode", "list")
    enabled_count = sum(1 for v in mn_list.values() if "ENABLED" in str(v))
    print(f"  Masternodes ENABLED: {enabled_count}/{NUM_MASTERNODES}")

    if enabled_count < NUM_MASTERNODES:
        for _ in range(10):
            network.move_blocks(4)
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
    sporks_to_enable = [
        "SPORK_17_QUORUM_DKG_ENABLED",
        "SPORK_21_QUORUM_ALL_CONNECTED",
        "SPORK_2_INSTANTSEND_ENABLED",
        "SPORK_3_INSTANTSEND_BLOCK_FILTERING",
        "SPORK_19_CHAINLOCKS_ENABLED",
    ]
    for name in sporks_to_enable:
        rpc.call("sporkupdate", name, 0)

    # Wait for spork propagation
    time.sleep(3)
    network.bump_mocktime(1)

    sporks = rpc.call("spork", "show")
    for name in sporks_to_enable:
        print(f"  {name}: {sporks.get(name, 'unknown')}")


def _wait_for_dkg_phase(network, q_0, q_1, phase):
    """Drive one DKG phase transition on both type-100 and type-103 rotating sessions.

    At this point the chain is at `cycle+2*(phase-1)-1` for phases >= 2 — i.e.
    one block before q_0 enters `phase`. Mines two blocks total (one to enter
    phase on q_0, one to enter phase on q_1) with gating on the expected DKG
    message count so the chain never advances past a phase before real
    messages have been exchanged.
    """
    # Phase 6 (finalize) has no message gate; only phases 2-5 do.
    field, count_test, count_dip0024 = PHASE_GATES.get(phase, (None, 0, 0))
    network.move_blocks(1)  # enter phase on q_0 (and on the type-100 session)

    print(f"    Phase {phase} ({PHASE_NAMES[phase]}) q_0...", end="", flush=True)
    wait_for_quorum_phase(network, "llmq_test", q_0, phase, LLMQ_TEST_SIZE, field, count_test, timeout=45)
    wait_for_quorum_phase(
        network, "llmq_test_dip0024", q_0, phase, LLMQ_TEST_DIP0024_SIZE, field, count_dip0024, timeout=45
    )
    print(" ok", end="", flush=True)

    network.move_blocks(1)  # enter phase on q_1 (rotating only)
    print("  q_1...", end="", flush=True)
    wait_for_quorum_phase(
        network, "llmq_test_dip0024", q_1, phase, LLMQ_TEST_DIP0024_SIZE, field, count_dip0024, timeout=45
    )
    print(" ok")


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

    # Align to the next cycle boundary (staying put if already on one), adding
    # the DIP-0024 3-cycle warmup on the first call.
    height = rpc.call("getblockcount")
    skip_count = DKG_INTERVAL - (height % DKG_INTERVAL)
    if skip_count == DKG_INTERVAL:
        skip_count = 0
    warmup_blocks = 0 if cycle_quorum_is_ready else DKG_INTERVAL * 3
    total_move = warmup_blocks + skip_count
    if total_move > 0:
        if warmup_blocks > 0:
            print(f"  DIP-0024 warmup: mining {total_move} blocks before first cycle...")
        network.move_blocks(total_move)

    q_0 = rpc.call("getbestblockhash")
    height = rpc.call("getblockcount")
    print(f"\n  Cycle {cycle_idx + 1}/{num_cycles} at height {height} q_0={q_0[:16]}...")

    # Phase 1 (init) + peer mesh for q_0 on both llmq_test and llmq_test_dip0024.
    print("    Phase 1 (init) q_0...", end="", flush=True)
    wait_for_quorum_phase(network, "llmq_test", q_0, 1, LLMQ_TEST_SIZE, timeout=60)
    wait_for_quorum_phase(network, "llmq_test_dip0024", q_0, 1, LLMQ_TEST_DIP0024_SIZE, timeout=60)
    wait_for_quorum_connections(network, "llmq_test", q_0, LLMQ_TEST_SIZE, timeout=60)
    wait_for_quorum_connections(network, "llmq_test_dip0024", q_0, LLMQ_TEST_DIP0024_SIZE, timeout=60)
    print(" ok")

    # Advance 1 block -> q_1 (the rotating pair's second quorum) enters phase 1.
    network.move_blocks(1)
    q_1 = rpc.call("getbestblockhash")
    print(f"    Phase 1 (init) q_1={q_1[:16]}...", end="", flush=True)
    wait_for_quorum_phase(network, "llmq_test_dip0024", q_1, 1, LLMQ_TEST_DIP0024_SIZE, timeout=60)
    wait_for_quorum_connections(network, "llmq_test_dip0024", q_1, LLMQ_TEST_DIP0024_SIZE, timeout=60)
    print(" ok")

    # Phases 2-6 block-by-block. Each iteration mines 2 blocks: one to enter
    # the phase on q_0 (and the type-100 session, which shares even offsets),
    # and one to enter the phase on q_1.
    for phase in range(2, 7):
        _wait_for_dkg_phase(network, q_0, q_1, phase)

    # Mine the commit block. At cycle+12 the controller creates a block that
    # includes the real finalCommitment txs for type 100 (window [cycle+10,
    # cycle+18]) and for both type 103 rotating quorums (window [cycle+12,
    # cycle+20]). Mirrors the final mining step in test_framework's
    # mine_cycle_quorum (getblocktemplate + generate(1)).
    network.bump_mocktime(1)
    rpc.call("getblocktemplate")
    network.move_blocks(1)

    # `quorum list` reflects commitments stored in evoDB by ProcessCommitment,
    # which only writes non-null commitments. If a hash is missing, the
    # corresponding commitment was null in the mined block.
    wait_for_quorum_list(network, "llmq_test", [q_0])
    wait_for_quorum_list(network, "llmq_test_dip0024", [q_0, q_1])

    # Mine 8 blocks (SIGN_HEIGHT_OFFSET) for signing-window maturity, matching
    # the tail of test_framework.mine_cycle_quorum.
    network.move_blocks(8)

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

    cycle_quorum_is_ready = False
    for cycle in range(num_cycles):
        _run_single_dkg_cycle(network, cycle, num_cycles, cycle_quorum_is_ready)
        # Warmup is applied on the first call; subsequent cycles skip it.
        cycle_quorum_is_ready = True

    rpc = network.controller.rpc
    height = rpc.call("getblockcount")
    qlist = rpc.call("quorum", "list")
    print(f"\n  Completed {num_cycles}/{num_cycles} DKG cycles (height: {height})")
    print(
        f"  Quorums: llmq_test={len(qlist.get('llmq_test', []))}, "
        f"llmq_test_dip0024={len(qlist.get('llmq_test_dip0024', []))}"
    )


def phase_6_generate_test_transactions(network):
    """Send transactions to the SPV test wallet."""
    print("\n" + "=" * 60)
    print("Phase 6: Generate SPV test transactions")
    print("=" * 60)

    rpc = network.controller.rpc
    addresses = [rpc.call("getnewaddress", wallet="wallet") for _ in range(10)]
    amounts = [1.0, 5.0, 10.0, 0.5, 25.0, 0.1, 50.0, 2.5]
    for i, amount in enumerate(amounts):
        rpc.call("sendtoaddress", addresses[i % len(addresses)], amount)
        if (i + 1) % 3 == 0:
            network.move_blocks(1)

    network.move_blocks(6)

    height = rpc.call("getblockcount")
    print(f"  Generated {len(amounts)} test transactions (height: {height})")


def phase_7_export(network, output_base_dir, dkg_cycles):
    """Export all node data and metadata."""
    print("\n" + "=" * 60)
    print("Phase 7: Export")
    print("=" * 60)

    rpc = network.controller.rpc
    chain_height = rpc.call("getblockcount")

    output_dir = Path(output_base_dir) / "regtest-mn-v0.0.1"
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

    print("  Stopping all nodes...")
    network.stop_all()
    time.sleep(2)

    print("  Copying controller datadir...")
    shutil.copytree(network.controller.datadir / "regtest", output_dir / "controller" / "regtest")

    for i, mn in enumerate(network.masternodes):
        mn_name = f"mn{i + 1}"
        print(f"  Copying {mn_name} datadir...")
        shutil.copytree(mn.datadir / "regtest", output_dir / mn_name / "regtest")

    network_metadata = {
        "version": "0.0.1",
        "chain_height": chain_height,
        "dkg_cycles_completed": dkg_cycles,
        "dkg_interval": DKG_INTERVAL,
        "controller": {"datadir": "controller", "wallet": "wallet"},
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
    print(f"  DKG cycles: {dkg_cycles}")

    return output_dir


def main():
    parser = argparse.ArgumentParser(description="Generate masternode network test data")
    parser.add_argument("--dashd-path", required=True, help="Path to dashd binary")
    parser.add_argument("--dkg-cycles", type=int, default=8, help="Number of DKG cycles (default: 8)")
    parser.add_argument("--output-dir", default=str(Path(__file__).parent / "data"), help="Output directory")
    args = parser.parse_args()

    dashd_bin = Path(args.dashd_path)
    if not dashd_bin.exists():
        print(f"dashd not found: {dashd_bin}")
        sys.exit(1)

    extra_args = [*DASHD_EXTRA_ARGS, f"-sporkkey={SPORK_PRIVATE_KEY}"]
    network = MasternodeNetwork(
        dashd_path=args.dashd_path,
        num_masternodes=NUM_MASTERNODES,
        base_extra_args=extra_args,
    )

    try:
        # Seed mocktime before any node starts so `-mocktime=` matches the
        # tracked value when the controller first launches.
        network.mocktime = TIME_GENESIS_BLOCK
        network.start_controller(extra_args=[f"-mocktime={TIME_GENESIS_BLOCK}"])

        phase_1_bootstrap(network)
        phase_2_register_masternodes(network)
        phase_3_start_masternodes(network)
        phase_4_enable_sporks(network)
        phase_5_mine_dkg_cycles(network, args.dkg_cycles)
        phase_6_generate_test_transactions(network)
        output_dir = phase_7_export(network, args.output_dir, args.dkg_cycles)

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
