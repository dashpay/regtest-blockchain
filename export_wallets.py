#!/usr/bin/env python3
"""Export wallet statistics from existing blockchain data."""

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

# Add generator module to path
sys.path.insert(0, str(Path(__file__).parent))

from generator.dashd_manager import dashd_preexec_fn
from generator.errors import RPCError
from generator.rpc_client import DashRPCClient
from generator.wallet_export import collect_wallet_stats, save_wallet_file


def find_free_port(start=19998):
    import socket

    for port in range(start, start + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError("No free port found")


def main():
    parser = argparse.ArgumentParser(description="Re-export wallet statistics from existing blockchain data")
    parser.add_argument("datadir", type=str, help="Path to dashd data directory (contains network subdirectory)")
    parser.add_argument("--dashd-path", type=str, help="Path to dashd executable (default: dashd in PATH)")
    parser.add_argument(
        "--network",
        type=str,
        default="regtest",
        choices=["regtest", "testnet", "mainnet"],
        help="Dash network (default: regtest)",
    )
    args = parser.parse_args()

    datadir = Path(args.datadir)
    if not datadir.exists():
        print(f"Directory not found: {datadir}")
        sys.exit(1)

    # dashd stores chain data in a network-named subdirectory
    network_subdirs = {"regtest": "regtest", "testnet": "testnet3", "mainnet": ""}
    network_subdir_name = network_subdirs[args.network]
    network_subdir = datadir / network_subdir_name if network_subdir_name else datadir
    if network_subdir_name and not network_subdir.exists():
        print(f"No {network_subdir_name}/ subdirectory found in {datadir}")
        sys.exit(1)

    wallets_dir = datadir / "wallets"
    wallets_dir.mkdir(exist_ok=True)

    # Determine dashd and dash-cli paths
    if args.dashd_path:
        dashd_executable = args.dashd_path
        dashcli_path = str(Path(args.dashd_path).parent / "dash-cli")
    else:
        dashd_executable = "dashd"
        dashcli_path = "dash-cli"

    # Find free ports
    rpc_port = find_free_port(19998)
    p2p_port = find_free_port(rpc_port + 1)

    print(f"Starting dashd ({args.network}) on RPC port {rpc_port}...")

    cmd = [
        dashd_executable,
        f"-{args.network}" if args.network != "mainnet" else "",
        f"-datadir={datadir}",
        f"-port={p2p_port}",
        f"-rpcport={rpc_port}",
        "-server=1",
        "-daemon=0",
        "-rpcbind=127.0.0.1",
        "-rpcallowip=127.0.0.1",
        "-listen=0",
    ]
    # Remove empty strings from cmd (mainnet needs no network flag)
    cmd = [c for c in cmd if c]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, preexec_fn=dashd_preexec_fn)
    except FileNotFoundError:
        print(f"dashd executable not found: {dashd_executable}")
        sys.exit(1)
    except OSError as e:
        print(f"Failed to start dashd ({dashd_executable}): {e}")
        sys.exit(1)

    def cleanup(exit_code=0):
        print("\nStopping dashd...")
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        sys.exit(exit_code)

    signal.signal(signal.SIGINT, lambda sig, frame: cleanup(1))

    rpc = DashRPCClient(dashcli_path=dashcli_path, datadir=str(datadir), network=args.network, rpc_port=rpc_port)

    print("Waiting for dashd to start...")
    for _ in range(30):
        try:
            height = rpc.call("getblockcount")
            print(f"Connected! Block height: {height}")
            break
        except Exception:
            time.sleep(1)
    else:
        print("Failed to connect to dashd")
        cleanup(1)

    # Discover and load all wallets from the datadir
    wallet_names = []
    try:
        wallet_dir_info = rpc.call("listwalletdir")
        wallet_names = [w["name"] for w in wallet_dir_info.get("wallets", [])]
    except RPCError:
        # Fallback: scan filesystem for wallet directories
        wallets_path = network_subdir / "wallets"
        if wallets_path.exists():
            wallet_names = [d.name for d in wallets_path.iterdir() if d.is_dir()]

    if not wallet_names:
        print("No wallets found in datadir")
        cleanup(1)

    print(f"Found {len(wallet_names)} wallet(s): {', '.join(wallet_names)}")

    for name in wallet_names:
        try:
            rpc.call("loadwallet", name)
            print(f"  Loaded wallet: {name}")
        except RPCError as e:
            if "already loaded" in str(e).lower():
                print(f"  Wallet already loaded: {name}")
            else:
                print(f"  Warning: Could not load {name}: {e}")

    # Collect and export stats for each wallet
    print("\nCollecting wallet statistics...")

    for wallet_name in wallet_names:
        print(f"  Processing {wallet_name}...")
        stats = collect_wallet_stats(rpc, wallet_name)

        unique_txs = len({tx["txid"] for tx in stats["transactions"]})
        print(
            f"    {len(stats['transactions'])} entries, {unique_txs} unique txs, "
            f"{len(stats['utxos'])} UTXOs, balance: {stats['balance']:.8f} DASH"
        )

        wallet_file = wallets_dir / f"{wallet_name}.json"
        save_wallet_file(stats, wallet_file)
        print(f"    Saved to {wallet_file}")

    print("\nDone! Stopping dashd...")
    cleanup()


if __name__ == "__main__":
    main()
