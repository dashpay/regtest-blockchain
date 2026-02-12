"""Integration tests that exercise generate.py via the CLI.

Requires the DASHD_PATH environment variable pointing to the dashd binary.
These tests invoke generate.py as a subprocess to verify the full CLI interface.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

GENERATE_PY = str(Path(__file__).parent.parent / "generate.py")
PYTHON = sys.executable


def get_dashd_path():
    path = os.environ.get("DASHD_PATH", "")
    assert path, "DASHD_PATH environment variable is not set (run contrib/setup-dashd.py first)"
    dashd = Path(path)
    assert dashd.is_file(), f"dashd binary not found at {path}"
    assert os.access(path, os.X_OK), f"dashd binary not executable at {path}"
    return path


def run_generate(*args, timeout=120):
    """Run generate.py with the given arguments and return the CompletedProcess."""
    cmd = [PYTHON, GENERATE_PY, "--dashd-path", get_dashd_path(), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --- CLI argument validation ---


class TestCLIArguments:
    """Test generate.py CLI argument parsing and validation."""

    def test_help(self):
        """Verify --help works and shows expected options."""
        result = subprocess.run([PYTHON, GENERATE_PY, "--help"], capture_output=True, text=True)
        assert result.returncode == 0
        assert "--strategy" in result.stdout
        assert "--blocks" in result.stdout
        assert "--dashd-path" in result.stdout
        assert "--output-dir" in result.stdout
        assert "--no-auto-start" in result.stdout
        assert "--rpc-port" in result.stdout
        assert "--keep-temp" in result.stdout

    def test_blocks_below_minimum_rejected(self):
        """Verify --blocks below 120 is rejected."""
        result = subprocess.run(
            [PYTHON, GENERATE_PY, "--blocks", "50"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert "120" in result.stdout or "120" in result.stderr

    def test_blocks_at_minimum_accepted(self, tmp_path):
        """Verify --blocks 120 is accepted and runs successfully."""
        result = run_generate("--blocks", "120", "--output-dir", str(tmp_path))
        assert result.returncode == 0, f"generate.py failed:\n{result.stderr}\n{result.stdout}"

    def test_invalid_dashd_path_rejected(self, tmp_path):
        """Verify a nonexistent --dashd-path causes failure."""
        result = subprocess.run(
            [
                PYTHON,
                GENERATE_PY,
                "--dashd-path",
                "/nonexistent/dashd",
                "--blocks",
                "120",
                "--output-dir",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0

    def test_output_dir_argument(self, tmp_path):
        """Verify --output-dir controls where data is written."""
        result = run_generate("--blocks", "120", "--output-dir", str(tmp_path))
        assert result.returncode == 0, f"generate.py failed:\n{result.stderr}\n{result.stdout}"

        data_dir = tmp_path / "regtest-120"
        assert data_dir.is_dir(), f"Output not written to specified --output-dir: {tmp_path}"


# --- End-to-end generation ---


@pytest.fixture(scope="module")
def generated_data(tmp_path_factory):
    """Run generate.py once for all end-to-end tests in this module."""
    output_dir = tmp_path_factory.mktemp("integration")
    result = run_generate("--blocks", "200", "--output-dir", str(output_dir))
    assert result.returncode == 0, f"generate.py failed:\n{result.stderr}\n{result.stdout}"

    data_dir = output_dir / "regtest-200"
    return {"data_dir": data_dir, "output": result.stdout}


class TestWalletSyncGeneration:
    """End-to-end test of the wallet-sync generation via generate.py CLI."""

    def test_output_directory_created(self, generated_data):
        """Verify the generator creates the expected output directory."""
        assert generated_data["data_dir"].is_dir()

    def test_completion_message(self, generated_data):
        """Verify generate.py prints completion output."""
        output = generated_data["output"]
        assert "Generation complete!" in output
        assert "Blocks:" in output

    def test_regtest_blockchain_data(self, generated_data):
        """Verify blockchain data files exist."""
        regtest_dir = generated_data["data_dir"] / "regtest"
        assert regtest_dir.is_dir(), "regtest directory missing"

        blocks_dir = regtest_dir / "blocks"
        assert blocks_dir.is_dir(), "blocks directory missing"

        chainstate_dir = regtest_dir / "chainstate"
        assert chainstate_dir.is_dir(), "chainstate directory missing"

        block_files = list(blocks_dir.glob("blk*.dat"))
        assert len(block_files) > 0, "No block data files found"
        for bf in block_files:
            assert bf.stat().st_size > 0, f"Block file is empty: {bf.name}"

    def test_wallet_json_files(self, generated_data):
        """Verify wallet JSON files are created with valid structure."""
        wallets_dir = generated_data["data_dir"] / "wallets"
        assert wallets_dir.is_dir(), "wallets directory missing"

        # wallet-sync strategy creates: default (faucet) and wallet (test)
        for wallet_name in ["default", "wallet"]:
            wallet_file = wallets_dir / f"{wallet_name}.json"
            assert wallet_file.is_file(), f"Wallet file missing: {wallet_name}.json"

            with open(wallet_file) as f:
                data = json.load(f)

            assert data["wallet_name"] == wallet_name
            assert "balance" in data
            assert isinstance(data["transactions"], list)
            assert isinstance(data["utxos"], list)
            assert "transaction_count" in data
            assert "utxo_count" in data

    def test_wallet_mnemonic(self, generated_data):
        """Verify the test wallet has an HD mnemonic."""
        wallet_file = generated_data["data_dir"] / "wallets" / "wallet.json"
        with open(wallet_file) as f:
            data = json.load(f)

        assert data.get("mnemonic"), "Test wallet should have a mnemonic"
        words = data["mnemonic"].strip().split()
        assert len(words) >= 12, f"Mnemonic too short: {len(words)} words"

    def test_wallet_has_transactions(self, generated_data):
        """Verify the test wallet received transactions from the generator."""
        wallet_file = generated_data["data_dir"] / "wallets" / "wallet.json"
        with open(wallet_file) as f:
            data = json.load(f)

        assert data["transaction_count"] > 0, "Test wallet should have received transactions"
        assert data["utxo_count"] > 0, "Test wallet should have UTXOs"
        assert data["balance"] > 0, "Test wallet should have a positive balance"

    def test_faucet_has_balance(self, generated_data):
        """Verify the faucet wallet has a positive balance from mining rewards."""
        wallet_file = generated_data["data_dir"] / "wallets" / "default.json"
        with open(wallet_file) as f:
            data = json.load(f)

        assert data["balance"] > 0, "Faucet wallet should have balance from mining"
        assert data["utxo_count"] > 0, "Faucet wallet should have UTXOs"

    def test_wallet_directory_in_regtest(self, generated_data):
        """Verify wallet directories are copied into the regtest data."""
        regtest_dir = generated_data["data_dir"] / "regtest"
        wallet_dir = regtest_dir / "wallet"
        assert wallet_dir.is_dir(), "Wallet directory 'wallet' not found in regtest data"
