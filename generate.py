#!/usr/bin/env python3
"""
Dash Regtest Test Data Generator

Generates blockchain test data optimized for SPV wallet sync testing with:
- Automatic dashd startup in temporary directory
- Targeted transactions exercising SPV sync edge cases
- Robust error handling
- Portable operation from any directory

Usage:
    python3 generate.py --blocks 40000
    python3 generate.py --blocks 200 --dashd-path /path/to/dashd
    python3 generate.py --blocks 1000 --output-dir /tmp/output
"""

import datetime
import random
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Add generator module to path
sys.path.insert(0, str(Path(__file__).parent))

from generator.dashd_manager import DashdManager
from generator.errors import (
    ConfigError,
    DashdConnectionError,
    GeneratorError,
    InsufficientFundsError,
    RPCError,
)
from generator.rpc_client import DashRPCClient
from generator.wallet_export import collect_wallet_stats, save_wallet_file


@dataclass
class Config:
    """Configuration for test data generation"""

    target_blocks: int
    dashcli_path: str
    dashd_executable: str
    auto_start_dashd: bool
    dashd_datadir: str | None
    dashd_wallet: str
    rpc_port: int | None
    output_base: str
    # Extra dashd args (block filter index for SPV testing)
    extra_dashd_args: list[str] = field(default_factory=list)


class Generator:
    """Base generator with shared infrastructure.

    Subclasses must implement:
    - _load_addresses(): wallet setup
    - _initialize_utxo_pool(): funding
    - _generate_blocks(): block generation logic
    """

    def __init__(self, config: Config, keep_temp: bool = False):
        self.config = config
        self.keep_temp = keep_temp
        self.dashd_manager: DashdManager | None = None
        self.rpc: DashRPCClient | None = None
        self.wallets = []  # List of wallet dictionaries with name, mnemonic, addresses
        self.utxo_count = 0
        self.output_dir: Path | None = None
        self.mining_address: str | None = None  # Faucet address for mining rewards
        self.stats = {"blocks_generated": 0, "transactions_created": 0, "coinbase_rewards": 0, "utxo_replenishments": 0}

    def generate(self):
        """Main generation workflow"""
        print("=" * 60)
        print("Dash Regtest Test Data Generator")
        print("=" * 60)
        print(f"Strategy: {self.strategy_name()}")
        print(f"Target blocks: {self.config.target_blocks}")
        print()

        generation_start_time = time.time()

        try:
            self._ensure_dashd_running()
            self._initialize_rpc_client()
            self._verify_dashd()
            self._load_addresses()
            self._initialize_utxo_pool()
            self._generate_blocks()
            self._export_data()

            generation_duration = time.time() - generation_start_time
            duration_str = str(datetime.timedelta(seconds=int(generation_duration)))

            print("\n" + "=" * 60)
            print("Generation complete!")
            print("=" * 60)
            print(f"Blocks: {self.stats['blocks_generated']}")
            print(f"Transactions: {self.stats['transactions_created']}")
            print(f"Coinbase rewards: {self.stats['coinbase_rewards']}")
            print(f"UTXO replenishments: {self.stats['utxo_replenishments']}")
            print(f"Total duration: {duration_str}")

        except KeyboardInterrupt:
            print("\n\nGeneration interrupted by user")
            raise GeneratorError("User interrupted") from None
        finally:
            # Cleanup if not already done
            if self.dashd_manager:
                if self.dashd_manager.process:
                    self.dashd_manager.stop()
                elif self.dashd_manager.temp_dir and self.dashd_manager.should_cleanup:
                    # Process stopped but temp dir not cleaned up yet
                    try:
                        shutil.rmtree(self.dashd_manager.temp_dir, ignore_errors=True)
                    except OSError:
                        pass

    def strategy_name(self) -> str:
        """Return the name of this strategy"""
        return "base"

    def _ensure_dashd_running(self):
        """Start dashd if auto_start is enabled"""
        if not self.config.auto_start_dashd:
            return

        print("\n" + "=" * 60)
        print("DASHD AUTO-START")
        print("=" * 60)

        self.dashd_manager = DashdManager(
            dashd_executable=self.config.dashd_executable,
            rpc_port=self.config.rpc_port,
            extra_args=self.config.extra_dashd_args,
        )

        rpc_port, temp_dir = self.dashd_manager.start(keep_temp=self.keep_temp)

        # Update config with actual values
        self.config.rpc_port = rpc_port
        self.config.dashd_datadir = str(temp_dir)

        print("=" * 60)
        print()

    def _initialize_rpc_client(self):
        """Initialize RPC client with appropriate settings"""
        self.rpc = DashRPCClient(
            dashcli_path=self.config.dashcli_path, datadir=self.config.dashd_datadir, rpc_port=self.config.rpc_port
        )

    def _verify_dashd(self):
        """Verify dashd is running and responsive, create wallet if needed"""
        print("Verifying dashd connection...")
        try:
            block_count = self.rpc.call("getblockcount")
            print(f"  Connected to dashd (current height: {block_count})")
        except DashdConnectionError as e:
            print(f"  Cannot connect to dashd: {e}")
            print("\nPlease ensure dashd is running in regtest mode:")
            print("  dashd -regtest -daemon")
            raise

        # Ensure wallet exists and is loaded
        try:
            # Try to load the wallet if it exists but isn't loaded
            self.rpc.call("loadwallet", self.config.dashd_wallet)
            print(f"  Loaded wallet: {self.config.dashd_wallet}")
        except RPCError as e:
            error_msg = str(e).lower()
            if "already loaded" in error_msg:
                print(f"  Wallet already loaded: {self.config.dashd_wallet}")
            elif "not found" in error_msg or "does not exist" in error_msg:
                # Wallet doesn't exist, create it
                print(f"  Creating new wallet: {self.config.dashd_wallet}")
                self.rpc.call("createwallet", self.config.dashd_wallet)
                print(f"  Created wallet: {self.config.dashd_wallet}")
            else:
                print(f"  Unexpected wallet error: {e}")
                raise

    def _load_addresses(self):
        """Wallet setup - must be implemented by subclass"""
        raise NotImplementedError

    def _initialize_utxo_pool(self):
        """UTXO funding - must be implemented by subclass"""
        raise NotImplementedError

    def _generate_blocks(self):
        """Block generation - must be implemented by subclass"""
        raise NotImplementedError

    def _collect_wallet_statistics(self):
        """Collect transaction history, UTXOs, and balance for each wallet (including faucet)"""
        print("\n  Collecting wallet statistics...")

        for wallet in self.wallets:
            wallet_name = wallet["wallet_name"]
            print(f"    Processing {wallet_name}...")
            stats = collect_wallet_stats(self.rpc, wallet_name)

            wallet["transactions"] = stats["transactions"]
            wallet["utxos"] = stats["utxos"]
            wallet["balance"] = stats["balance"]
            if stats.get("mnemonic"):
                wallet["mnemonic"] = stats["mnemonic"]

            print(
                f"      {len(stats['transactions'])} txs, "
                f"{len(stats['utxos'])} UTXOs, balance: {stats['balance']:.8f} DASH"
            )

    def _save_wallet_files(self):
        """Save each wallet to a separate JSON file in wallets/ directory"""
        print("\n  Saving wallet files...")

        wallets_dir = self.output_dir / "wallets"
        wallets_dir.mkdir(parents=True, exist_ok=True)

        for wallet in self.wallets:
            wallet_file = wallets_dir / f"{wallet['wallet_name']}.json"
            save_wallet_file(wallet, wallet_file)

            print(
                f"    {wallet['wallet_name']}.json: "
                f"{len(wallet.get('addresses', []))} addrs, {len(wallet['transactions'])} txs, "
                f"{len(wallet['utxos'])} UTXOs, balance: {wallet['balance']:.8f} DASH"
            )

    def _export_data(self):
        """Export blockchain data"""
        print("\nExporting blockchain data...")

        # Verify we reached target height
        final_height = self.rpc.call("getblockcount")
        if final_height != self.config.target_blocks:
            print(f"  Warning: Final height ({final_height}) differs from target ({self.config.target_blocks})")

        self.output_dir = Path(self.config.output_base) / f"regtest-{self.config.target_blocks}"

        # Clean output directory if it exists to avoid leftover files
        if self.output_dir.exists():
            print(f"  Removing existing output directory: {self.output_dir}")
            shutil.rmtree(self.output_dir)

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Collect wallet statistics first (while dashd is still running)
        self._collect_wallet_statistics()
        self._save_wallet_files()

        # Copy datadir before stopping dashd (while temp dir still exists)
        if self.dashd_manager:
            print("\n  Stopping dashd to copy blockchain data...")
            # Stop dashd process but don't cleanup temp dir yet
            if self.dashd_manager.process:
                try:
                    self.dashd_manager.process.terminate()
                    self.dashd_manager.process.wait(timeout=10)
                except Exception as e:
                    print(f"  Warning: Error stopping dashd: {e}")
                finally:
                    self.dashd_manager.process = None

            # Wait a moment for clean shutdown
            time.sleep(2)

        # Copy the entire dashd datadir for direct use in tests
        self._copy_dashd_datadir(self.output_dir)

        # Now cleanup temp dir if needed
        if self.dashd_manager and self.dashd_manager.temp_dir and self.dashd_manager.should_cleanup:
            print(f"\n  Cleaning up temporary directory: {self.dashd_manager.temp_dir}")
            try:
                shutil.rmtree(self.dashd_manager.temp_dir, ignore_errors=True)
            except Exception as e:
                print(f"  Warning: Could not remove temp directory: {e}")
            self.dashd_manager.temp_dir = None

        print(f"\n  Exported to {self.output_dir}")

    def _copy_dashd_datadir(self, output_dir: Path):
        """Copy dashd datadir for direct use in tests.

        Wallet directory names are derived from self.wallets.
        """
        if not self.config.dashd_datadir:
            print("  No dashd datadir to copy (not using auto-start)")
            return

        source_dir = Path(self.config.dashd_datadir)
        if not source_dir.exists():
            print(f"  Source datadir does not exist: {source_dir}")
            return

        print(f"  Copying dashd datadir from {source_dir}...")

        regtest_source = source_dir / "regtest"
        if regtest_source.exists():
            print("    Copying regtest directory...")

            # Copy regtest/ directory to output_dir/regtest/ (preserve directory structure)
            regtest_dest = output_dir / "regtest"
            if regtest_dest.exists():
                shutil.rmtree(regtest_dest)

            shutil.copytree(regtest_source, regtest_dest, symlinks=False)

            total_size = sum(f.stat().st_size for f in regtest_dest.rglob("*") if f.is_file())
            size_mb = total_size / 1024 / 1024

            print(f"    Copied regtest data ({size_mb:.1f} MB)")

            # Derive expected wallet names from self.wallets
            expected_wallets = [w["wallet_name"] for w in self.wallets]
            found_wallets = []
            for wallet_name in expected_wallets:
                wallet_dir = regtest_dest / wallet_name
                if wallet_dir.exists() and wallet_dir.is_dir():
                    found_wallets.append(wallet_name)

            if found_wallets:
                print(f"    Wallet directories copied ({len(found_wallets)} wallets: {', '.join(found_wallets)})")
            else:
                print("    No wallet directories found in regtest")
        else:
            print(f"  No regtest directory found in {source_dir}")


class WalletSyncGenerator(Generator):
    """Generates a blockchain optimized for SPV wallet sync testing.

    Creates ~40K blocks with very few targeted transactions (~50-80)
    that exercise every critical SPV sync edge case:
    - Address discovery at various indices
    - Gap limit boundary and extension
    - Change address activity (spending from wallet)
    - Immature and mature coinbase rewards
    - Dust and large value transactions
    - Batched payments (sendmany) and consolidation (raw tx)
    - Empty block stretches
    - Address reuse
    - Transactions at filter batch boundaries
    """

    WALLET_NAME = "wallet"
    NUM_ADDRESSES = 50

    def __init__(self, config: Config, keep_temp: bool = False):
        super().__init__(config, keep_temp)
        # address index -> address string
        self.wallet_addresses: dict[int, str] = {}

    def strategy_name(self) -> str:
        return "wallet-sync"

    def _load_addresses(self):
        """Create a single test wallet with pre-generated addresses."""
        print(f"\nCreating test wallet '{self.WALLET_NAME}' with {self.NUM_ADDRESSES} addresses...")

        # Initialize faucet wallet placeholder
        self.wallets.append(
            {
                "wallet_name": self.config.dashd_wallet,
                "mnemonic": "",
                "addresses": [],
                "tier": "faucet",
                "transactions": [],
                "utxos": [],
                "balance": 0,
            }
        )

        # Create the test wallet in dashd
        try:
            self.rpc.call("createwallet", self.WALLET_NAME)
            print(f"  Created dashd wallet: {self.WALLET_NAME}")
        except RPCError as e:
            error_msg = str(e).lower()
            if "already exists" in error_msg or "already loaded" in error_msg:
                print(f"  Wallet already exists: {self.WALLET_NAME}")
            else:
                raise

        # Get HD wallet mnemonic
        hd_info = self.rpc.call("dumphdinfo", wallet=self.WALLET_NAME)
        mnemonic = hd_info.get("mnemonic", "")

        # Pre-generate addresses at specific indices
        # dashd generates addresses sequentially, so generating N addresses
        # gives us indices 0 through N-1
        addresses = []
        for i in range(self.NUM_ADDRESSES):
            address = self.rpc.call("getnewaddress", f"addr_{i}", wallet=self.WALLET_NAME)
            self.wallet_addresses[i] = address
            addresses.append({"address": address, "index": i})

        self.wallets.append(
            {
                "wallet_name": self.WALLET_NAME,
                "mnemonic": mnemonic,
                "addresses": addresses,
                "tier": "test",
                "transactions": [],
                "utxos": [],
                "balance": 0,
            }
        )

        print(f"  Generated {len(self.wallet_addresses)} addresses")
        print(f"  Mnemonic: {mnemonic}")

    def _initialize_utxo_pool(self):
        """Mine initial blocks for coinbase maturity and split faucet UTXOs."""
        print("\nBootstrap: mining initial blocks and creating faucet UTXOs...")

        self.mining_address = self.rpc.call("getnewaddress", wallet=self.config.dashd_wallet)
        self.rpc.call("generatetoaddress", 110, self.mining_address)

        current_height = self.rpc.call("getblockcount")
        print(f"  Mined 110 blocks (height: {current_height})")

        # Split into ~50 UTXOs for funding operations
        print("  Splitting faucet into ~50 UTXOs...")
        recipients = {}
        for _ in range(50):
            addr = self.rpc.call("getnewaddress", wallet=self.config.dashd_wallet)
            recipients[addr] = 10.0
        self.rpc.call("sendmany", "", recipients, wallet=self.config.dashd_wallet)
        self.rpc.call("generatetoaddress", 1, self.mining_address)

        utxo_count = len(self.rpc.call("listunspent", 1, wallet=self.config.dashd_wallet))
        print(f"  Faucet UTXO pool: {utxo_count} UTXOs")

    def _generate_blocks(self):
        """Execute phased block generation."""
        current_height = self.rpc.call("getblockcount")
        target = self.config.target_blocks

        print(f"\nGenerating blocks to reach height {target}...")
        print(f"  Current height: {current_height}")

        start_time = time.time()

        # Phase 2: Normal activity (heights ~116-200)
        self._phase_normal_activity()

        # Phase 3: Gap limit boundary (heights ~201-230)
        self._phase_gap_limit_boundary()

        # Phase 4: Beyond gap limit (heights ~231-260)
        self._phase_beyond_gap_limit()

        # Phase 5: Transaction variety (heights ~261-320)
        self._phase_transaction_variety()

        # Phase 6: Bulk generation with boundary transactions
        self._phase_bulk_generation()

        elapsed = time.time() - start_time
        final_height = self.rpc.call("getblockcount")
        self.stats["blocks_generated"] = final_height - current_height

        print("\n  Completed all phases")
        print(f"  Final height: {final_height}")
        print(f"  Transactions to test wallet: {self.stats['transactions_created']}")
        print(f"  Coinbase rewards to test wallet: {self.stats['coinbase_rewards']}")
        print(f"  Duration: {datetime.timedelta(seconds=int(elapsed))}")

    def _send_to_wallet(self, index: int, amount: float, description: str = ""):
        """Send funds from faucet to the test wallet at a specific address index."""
        address = self.wallet_addresses[index]
        self.rpc.call("sendtoaddress", address, amount, wallet=self.config.dashd_wallet)
        self.stats["transactions_created"] += 1
        if description:
            print(f"    Sent {amount} DASH to index {index} ({description})")
        else:
            print(f"    Sent {amount} DASH to index {index}")

    def _mine_blocks(self, count: int, address: str | None = None):
        """Mine blocks to the given address (or faucet if not specified)."""
        if address is None:
            address = self.mining_address
        self.rpc.call("generatetoaddress", count, address)

    def _mine_and_log(self, count: int, description: str = ""):
        """Mine blocks to faucet and log progress."""
        self._mine_blocks(count)
        height = self.rpc.call("getblockcount")
        if description:
            print(f"    Mined {count} blocks -> height {height} ({description})")

    def _phase_normal_activity(self):
        """Phase 2: Normal transaction activity to various address indices."""
        print("\n  Phase 2: Normal activity")

        # Basic address discovery at various indices
        # Dust transaction
        self._send_to_wallet(0, 0.00001, "dust")
        self._mine_blocks(2)

        # Small amounts
        self._send_to_wallet(2, 0.05, "small")
        self._send_to_wallet(5, 0.5, "medium")
        self._mine_blocks(2)

        # Medium amounts
        self._send_to_wallet(8, 1.0, "medium")
        self._send_to_wallet(12, 2.5, "medium")
        self._mine_blocks(2)

        # Large value
        self._send_to_wallet(15, 100.0, "large")
        self._send_to_wallet(20, 0.1, "small")
        self._mine_blocks(2)

        # Address reuse: send again to index 5
        self._send_to_wallet(5, 0.25, "address reuse")
        self._mine_blocks(1)

        # Batched payment (sendmany) hitting multiple indices
        recipients = {
            self.wallet_addresses[3]: 0.1,
            self.wallet_addresses[7]: 0.2,
            self.wallet_addresses[14]: 0.3,
        }
        self.rpc.call("sendmany", "", recipients, wallet=self.config.dashd_wallet)
        self.stats["transactions_created"] += 1
        print("    Sendmany to indices 3, 7, 14")
        self._mine_blocks(1)

        self._mine_and_log(10, "padding after normal activity")

        height = self.rpc.call("getblockcount")
        print(f"  Phase 2 complete at height {height}")

    def _phase_gap_limit_boundary(self):
        """Phase 3: Transactions at the gap limit boundary (indices 27, 28, 29)."""
        print("\n  Phase 3: Gap limit boundary")

        # Gap limit in most HD wallets is 20-30 (typically 30 for external addresses)
        # Indices 27, 28, 29 are the last addresses within the initial gap of 30
        self._send_to_wallet(27, 0.3, "gap limit -3")
        self._mine_blocks(3)

        self._send_to_wallet(28, 0.4, "gap limit -2")
        self._mine_blocks(3)

        self._send_to_wallet(29, 0.5, "gap limit -1 (last in initial gap)")
        self._mine_blocks(3)

        self._mine_and_log(10, "padding after gap limit")

        height = self.rpc.call("getblockcount")
        print(f"  Phase 3 complete at height {height}")

    def _phase_beyond_gap_limit(self):
        """Phase 4: Transactions beyond initial gap limit.

        These are only discoverable after index 29 triggers gap extension to index 59.
        """
        print("\n  Phase 4: Beyond gap limit")

        self._send_to_wallet(32, 0.6, "beyond gap (discoverable after rescan)")
        self._mine_blocks(5)

        self._send_to_wallet(35, 0.7, "beyond gap (discoverable after rescan)")
        self._mine_blocks(5)

        self._mine_and_log(10, "padding after beyond-gap")

        height = self.rpc.call("getblockcount")
        print(f"  Phase 4 complete at height {height}")

    def _phase_transaction_variety(self):
        """Phase 5: Various transaction types - spend from wallet, consolidation."""
        print("\n  Phase 5: Transaction variety")

        # First fund the wallet enough to be able to spend from it
        self._send_to_wallet(0, 5.0, "funding for spend-from-wallet")
        self._mine_blocks(1)

        # Spend FROM the test wallet (generates change to internal address)
        # Send from test wallet to faucet
        faucet_addr = self.rpc.call("getnewaddress", wallet=self.config.dashd_wallet)
        try:
            self.rpc.call("sendtoaddress", faucet_addr, 1.0, wallet=self.WALLET_NAME)
            self.stats["transactions_created"] += 1
            print("    Spent 1.0 DASH from test wallet (generates change output)")
        except RPCError as e:
            print(f"    Warning: Failed to spend from wallet: {e}")

        self._mine_blocks(3)

        # Consolidation: raw transaction merging wallet UTXOs
        try:
            wallet_utxos = self.rpc.call("listunspent", 1, 9999999, [], wallet=self.WALLET_NAME)
            if len(wallet_utxos) >= 2:
                # Pick 2 small UTXOs to consolidate
                selected = sorted(wallet_utxos, key=lambda u: u["amount"])[:2]
                total = sum(u["amount"] for u in selected)
                fee = 0.0001

                if total > fee:
                    inputs = [{"txid": u["txid"], "vout": u["vout"]} for u in selected]
                    dest = self.rpc.call("getnewaddress", wallet=self.WALLET_NAME)
                    outputs = {dest: round(total - fee, 8)}

                    raw_tx = self.rpc.call("createrawtransaction", inputs, outputs)
                    signed = self.rpc.call("signrawtransactionwithwallet", raw_tx, wallet=self.WALLET_NAME)

                    if signed.get("complete", False):
                        self.rpc.call("sendrawtransaction", signed["hex"])
                        self.stats["transactions_created"] += 1
                        print(f"    Consolidation tx: merged {len(selected)} UTXOs")
        except RPCError as e:
            print(f"    Warning: Consolidation failed: {e}")

        self._mine_blocks(3)

        self._mine_and_log(10, "padding after transaction variety")

        height = self.rpc.call("getblockcount")
        print(f"  Phase 5 complete at height {height}")

    def _phase_bulk_generation(self):
        """Phase 6: Generate remaining blocks in large batches.

        Places transactions at filter batch boundaries (every 5000 blocks)
        and coinbase rewards near the end.
        """
        current_height = self.rpc.call("getblockcount")
        target = self.config.target_blocks

        blocks_remaining = target - current_height
        if blocks_remaining <= 0:
            print("\n  Phase 6: skipped (already at target height)")
            return

        print(f"\n  Phase 6: Bulk generation ({blocks_remaining} blocks remaining)")

        # Calculate batch boundary heights (every 5000 blocks)
        batch_boundaries = self._calculate_batch_boundaries(current_height, target)
        if batch_boundaries:
            print(f"    Batch boundaries to hit: {batch_boundaries}")

        # Address index counter for boundary transactions
        boundary_addr_index = 40

        # Calculate coinbase mining ranges
        # Mature coinbase: blocks at target-200 to target-101
        mature_coinbase_start = target - 200
        mature_coinbase_end = target - 101
        # Immature coinbase: blocks at target-99 to target
        immature_coinbase_start = target - 99

        # Track which coinbase address to use
        coinbase_wallet_addr = self.wallet_addresses[0]

        # Periodic sends to test wallet (roughly every ~1000 blocks)
        # Uses a rotating set of address indices and varying amounts
        periodic_interval = 1000
        next_periodic_height = current_height + periodic_interval
        periodic_addresses = [1, 4, 6, 9, 10, 13, 16, 18, 21, 23, 25, 30, 33, 36, 38]
        periodic_amounts = [0.02, 0.15, 0.5, 1.0, 0.001, 3.0, 0.08, 0.25, 0.75, 2.0, 0.005, 0.4, 1.5, 0.03, 0.1]
        periodic_counter = 0

        start_time = time.time()
        batch_size = 500

        while current_height < target:
            # Calculate how many blocks to generate in this batch
            next_important_height = target
            for boundary in batch_boundaries:
                if boundary > current_height:
                    next_important_height = min(next_important_height, boundary)

            # Check if we need to handle mature coinbase range
            if current_height < mature_coinbase_start and mature_coinbase_start < next_important_height:
                next_important_height = mature_coinbase_start
            if current_height < mature_coinbase_end and mature_coinbase_end < next_important_height:
                next_important_height = mature_coinbase_end
            if current_height < immature_coinbase_start and immature_coinbase_start < next_important_height:
                next_important_height = immature_coinbase_start

            blocks_to_mine = min(next_important_height - current_height, batch_size)
            blocks_to_mine = max(blocks_to_mine, 1)

            # Determine if this batch includes special blocks
            batch_end = current_height + blocks_to_mine

            # Check if we're in the mature coinbase range
            if mature_coinbase_start <= current_height < mature_coinbase_end:
                # Mine some blocks to test wallet for mature coinbase
                wallet_blocks = min(5, batch_end - current_height)
                self._mine_blocks(wallet_blocks, coinbase_wallet_addr)
                self.stats["coinbase_rewards"] += wallet_blocks
                current_height += wallet_blocks
                print(f"    Mined {wallet_blocks} blocks to wallet (mature coinbase) at height {current_height}")

                # Mine remaining to faucet
                remaining = batch_end - current_height
                if remaining > 0:
                    self._mine_blocks(remaining)
                    current_height += remaining
                continue

            # Check if we're in the immature coinbase range
            if immature_coinbase_start <= current_height:
                # Mine some blocks to test wallet for immature coinbase
                wallet_blocks = min(5, target - current_height)
                self._mine_blocks(wallet_blocks, coinbase_wallet_addr)
                self.stats["coinbase_rewards"] += wallet_blocks
                current_height += wallet_blocks
                print(f"    Mined {wallet_blocks} blocks to wallet (immature coinbase) at height {current_height}")

                # Mine remaining to faucet
                remaining = target - current_height
                if remaining > 0:
                    self._mine_blocks(remaining)
                    current_height += remaining
                continue

            # Normal bulk mining
            self._mine_blocks(blocks_to_mine)
            current_height += blocks_to_mine

            # Place batch boundary transaction if we just passed one
            for boundary in list(batch_boundaries):
                if boundary <= current_height:
                    if boundary_addr_index < self.NUM_ADDRESSES:
                        self._send_to_wallet(boundary_addr_index, 0.01, f"batch boundary near height {boundary}")
                        self._mine_blocks(1)
                        current_height += 1
                        boundary_addr_index += 1
                    batch_boundaries.remove(boundary)

            # Periodic send to test wallet (~every 1000 blocks)
            if current_height >= next_periodic_height:
                idx = periodic_addresses[periodic_counter % len(periodic_addresses)]
                amt = periodic_amounts[periodic_counter % len(periodic_amounts)]
                try:
                    self._send_to_wallet(idx, amt, f"periodic at height {current_height}")
                    self._mine_blocks(1)
                    current_height += 1
                except RPCError:
                    pass
                periodic_counter += 1
                next_periodic_height = current_height + periodic_interval

            # Occasional faucet self-send for filter variety
            if random.random() < 0.01:
                try:
                    faucet_addr = self.rpc.call("getnewaddress", wallet=self.config.dashd_wallet)
                    self.rpc.call("sendtoaddress", faucet_addr, 1.0, wallet=self.config.dashd_wallet)
                    self._mine_blocks(1)
                    current_height += 1
                except RPCError:
                    pass

            # Progress logging
            elapsed = time.time() - start_time
            rate = (current_height - (target - blocks_remaining)) / elapsed if elapsed > 0 else 0
            remaining_blocks = target - current_height
            eta_seconds = remaining_blocks / rate if rate > 0 else 0
            eta = datetime.timedelta(seconds=int(eta_seconds))

            if current_height % 5000 < batch_size or current_height >= target:
                print(f"    Height {current_height}/{target} ({rate:.0f} blocks/sec, ETA: {eta})")

        # Verify final height
        actual = self.rpc.call("getblockcount")
        if actual > target:
            print(f"    Warning: overshot target by {actual - target} blocks (height: {actual})")
        elif actual < target:
            # Mine remaining blocks
            self._mine_blocks(target - actual)
            actual = self.rpc.call("getblockcount")

        print(f"  Phase 6 complete at height {actual}")

    @staticmethod
    def _calculate_batch_boundaries(current_height: int, target: int) -> list[int]:
        """Calculate filter batch boundary heights between current and target.

        Boundaries are at every 5000 blocks. We place a transaction just before
        each boundary (at boundary - 1).
        """
        boundaries = []
        # Start from next 5000 boundary above current height
        first_boundary = ((current_height // 5000) + 1) * 5000
        for boundary in range(first_boundary, target + 1, 5000):
            # Place transaction just before the boundary
            height = boundary - 1
            if height > current_height and height < target:
                boundaries.append(height)
        return boundaries


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate Dash regtest test data for SPV wallet sync testing")
    parser.add_argument(
        "--strategy",
        type=str,
        choices=["wallet-sync"],
        default="wallet-sync",
        help="Generation strategy (default: wallet-sync)",
    )
    parser.add_argument("--blocks", type=int, default=200, help="Target blockchain height (minimum: 120, default: 200)")
    parser.add_argument("--dashd-path", type=str, help="Path to dashd executable (default: dashd in PATH)")
    parser.add_argument("--no-auto-start", action="store_true", help="Disable automatic dashd startup")
    parser.add_argument("--rpc-port", type=int, help="RPC port to use (default: auto-detect)")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary directory after completion")
    parser.add_argument("--output-dir", type=str, help="Output base directory (default: data/ next to generate.py)")

    args = parser.parse_args()

    # Validate minimum block count (need 100+ for coinbase maturity plus setup)
    min_blocks = 120
    if args.blocks < min_blocks:
        print(f"ERROR: --blocks must be at least {min_blocks} (coinbase maturity requirement)")
        sys.exit(1)

    # Determine dashd and dash-cli paths
    if args.dashd_path:
        dashd_executable = args.dashd_path
        dashd_dir = Path(args.dashd_path).parent
        dashcli_path = str(dashd_dir / "dash-cli")
    else:
        dashd_executable = "dashd"
        dashcli_path = "dash-cli"

    # Output directory
    if args.output_dir:
        output_base = args.output_dir
    else:
        script_dir = Path(__file__).parent.resolve()
        output_base = str(script_dir / "data")

    config = Config(
        target_blocks=args.blocks,
        dashcli_path=dashcli_path,
        dashd_executable=dashd_executable,
        auto_start_dashd=not args.no_auto_start,
        dashd_datadir=None,
        dashd_wallet="default",
        rpc_port=args.rpc_port,
        output_base=output_base,
        extra_dashd_args=[
            "-blockfilterindex=1",
            "-peerblockfilters=1",
        ],
    )

    strategies = {
        "wallet-sync": WalletSyncGenerator,
    }
    generator = strategies[args.strategy](config, keep_temp=args.keep_temp)

    try:
        generator.generate()

    except ConfigError as e:
        print(f"ERROR: Configuration problem: {e}")
        sys.exit(1)
    except DashdConnectionError as e:
        print(f"ERROR: Cannot connect to dashd: {e}")
        sys.exit(2)
    except InsufficientFundsError as e:
        print(f"ERROR: Insufficient funds: {e}")
        sys.exit(3)
    except GeneratorError as e:
        print(f"ERROR: {e}")
        sys.exit(4)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
