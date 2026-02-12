"""Tests for the WalletSyncGenerator and Config."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from generate import Config, Generator, WalletSyncGenerator


def create_test_config(**overrides):
    """Create a Config with defaults for testing."""
    defaults = dict(
        target_blocks=200,
        dashcli_path="dash-cli",
        dashd_executable="dashd",
        auto_start_dashd=False,
        dashd_datadir=None,
        dashd_wallet="default",
        rpc_port=None,
        output_base="/tmp",
    )
    defaults.update(overrides)
    return Config(**defaults)


def create_wallet_sync_generator(**config_overrides):
    """Create a WalletSyncGenerator instance for testing."""
    config = create_test_config(**config_overrides)
    return WalletSyncGenerator(config)


# --- WalletSyncGenerator tests ---


class TestWalletSyncConfig:
    """Test WalletSyncGenerator configuration."""

    def test_wallet_name(self):
        """Verify the test wallet is named 'wallet'."""
        assert WalletSyncGenerator.WALLET_NAME == "wallet"

    def test_address_count(self):
        """Verify 50 addresses are pre-generated."""
        assert WalletSyncGenerator.NUM_ADDRESSES == 50

    def test_strategy_name(self):
        """Verify strategy name is 'wallet-sync'."""
        gen = create_wallet_sync_generator()
        assert gen.strategy_name() == "wallet-sync"


class TestBatchBoundaryCalculation:
    """Test batch boundary height calculation."""

    def test_40k_blocks(self):
        """Verify boundaries for 40000-block target."""
        boundaries = WalletSyncGenerator._calculate_batch_boundaries(200, 40000)
        assert len(boundaries) == 8, f"Expected 8 boundaries, got {len(boundaries)}: {boundaries}"
        assert boundaries[0] == 4999
        assert boundaries[-1] == 39999

    def test_10k_blocks(self):
        """Verify boundaries for 10000-block target."""
        boundaries = WalletSyncGenerator._calculate_batch_boundaries(200, 10000)
        assert len(boundaries) == 2, f"Expected 2 boundaries, got {len(boundaries)}: {boundaries}"
        assert boundaries[0] == 4999
        assert boundaries[1] == 9999

    def test_1k_blocks(self):
        """Verify no boundaries for 1000-block target (too short)."""
        boundaries = WalletSyncGenerator._calculate_batch_boundaries(200, 1000)
        assert len(boundaries) == 0, f"Expected 0 boundaries for 1000 blocks, got {len(boundaries)}"

    def test_boundaries_above_current_height(self):
        """Verify all boundaries are above current height."""
        boundaries = WalletSyncGenerator._calculate_batch_boundaries(5500, 40000)
        for b in boundaries:
            assert b > 5500, f"Boundary {b} should be above current height 5500"

    def test_boundaries_below_target(self):
        """Verify all boundaries are below target height."""
        boundaries = WalletSyncGenerator._calculate_batch_boundaries(200, 40000)
        for b in boundaries:
            assert b < 40000, f"Boundary {b} should be below target 40000"

    def test_boundary_spacing(self):
        """Verify boundaries are spaced ~5000 apart."""
        boundaries = WalletSyncGenerator._calculate_batch_boundaries(200, 40000)
        for i in range(1, len(boundaries)):
            spacing = boundaries[i] - boundaries[i - 1]
            assert spacing == 5000, f"Spacing between boundaries should be 5000, got {spacing}"

    def test_exact_boundary_start(self):
        """Verify behavior when current height is exactly at a boundary."""
        boundaries = WalletSyncGenerator._calculate_batch_boundaries(5000, 15000)
        assert len(boundaries) == 2, f"Expected 2 boundaries, got {len(boundaries)}: {boundaries}"
        assert boundaries[0] == 9999
        assert boundaries[1] == 14999

    def test_small_target_with_one_boundary(self):
        """Verify a target that spans exactly one boundary."""
        boundaries = WalletSyncGenerator._calculate_batch_boundaries(200, 5500)
        assert len(boundaries) == 1
        assert boundaries[0] == 4999


class TestGeneratorBase:
    """Test base Generator class."""

    def test_wallet_sync_is_subclass(self):
        """Verify WalletSyncGenerator is a Generator subclass."""
        gen = WalletSyncGenerator(create_test_config())
        assert gen.strategy_name() == "wallet-sync"
        assert isinstance(gen, Generator)

    def test_base_generator_raises(self):
        """Verify base Generator raises NotImplementedError for abstract methods."""
        gen = Generator(create_test_config())

        with pytest.raises(NotImplementedError):
            gen._load_addresses()

        with pytest.raises(NotImplementedError):
            gen._initialize_utxo_pool()

        with pytest.raises(NotImplementedError):
            gen._generate_blocks()


class TestConfigExtraArgs:
    """Test extra_dashd_args in Config."""

    def test_default_empty(self):
        """Verify extra_dashd_args defaults to empty list."""
        config = create_test_config()
        assert config.extra_dashd_args == []

    def test_custom_args(self):
        """Verify extra_dashd_args can be set."""
        config = create_test_config(extra_dashd_args=["-blockfilterindex=1"])
        assert config.extra_dashd_args == ["-blockfilterindex=1"]


class TestWalletSyncAddressIndices:
    """Test that WalletSyncGenerator targets the right address indices."""

    def test_wallet_addresses_dict(self):
        """Verify wallet_addresses dict is initialized empty."""
        gen = create_wallet_sync_generator()
        assert gen.wallet_addresses == {}

    def test_phase_coverage(self):
        """Verify all phases exist as methods."""
        gen = create_wallet_sync_generator()
        assert hasattr(gen, "_phase_normal_activity")
        assert hasattr(gen, "_phase_gap_limit_boundary")
        assert hasattr(gen, "_phase_beyond_gap_limit")
        assert hasattr(gen, "_phase_transaction_variety")
        assert hasattr(gen, "_phase_bulk_generation")


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
