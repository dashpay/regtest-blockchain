# regtest-blockchain

Pre-generated Dash regtest blockchain data for integration testing.

Contains blockchain state and wallet files with known addresses and transaction history, useful for testing wallet sync, transaction parsing, and block validation without running a live network.

## Quick Start

Download the latest release:

```bash
curl -LO https://github.com/dashpay/regtest-blockchain/releases/latest/download/regtest-15000.tar.gz
tar -xzf regtest-15000.tar.gz
```

Start dashd with this data:

```bash
dashd -datadir=./regtest-15000 -regtest -daemon
```

## What's Included

Each dataset contains a full dashd data directory and exported wallet JSON files:

```
regtest-<height>/
├── dash.conf
├── regtest/            # blockchain data (blocks, chainstate, wallet dirs)
│   ├── blocks/
│   ├── chainstate/
│   ├── indexes/
│   └── <wallet_name>/  # dashd wallet directories
└── wallets/            # exported JSON wallet files with mnemonics, addresses, UTXOs
```

The wallet JSON files contain HD mnemonics, derived addresses, transaction history, and UTXO sets for verifying sync against the blockchain.

## Generating

Generate test data with the included script:

```bash
# Generate 15000 blocks (outputs to data/regtest-15000/)
python3 generate.py --blocks 15000

# Specify a custom dashd binary
python3 generate.py --blocks 15000 --dashd-path /path/to/dashd

# Custom output directory
python3 generate.py --blocks 15000 --output-dir /tmp/output
```

The `wallet-sync` strategy (default) creates a blockchain optimized for SPV wallet sync testing with targeted transactions at specific address indices, gap limit boundaries, filter batch boundaries, and coinbase reward ranges.

Requires `dashd` and `dash-cli` in PATH (or specify with `--dashd-path`). Minimum block height is 120 (coinbase maturity requirement).

## Re-exporting Wallet Data

Re-export wallet statistics from an existing dataset without regenerating the blockchain:

```bash
python3 export_wallets.py data/regtest-15000
```

This starts a temporary dashd instance, loads all wallets found in the data directory, and writes updated JSON files to `wallets/`.

## Project Structure

```
├── generate.py           # main generation script
├── export_wallets.py     # re-export wallet data from existing blockchain
├── contrib/
│   └── setup-dashd.py    # cross-platform dashd binary download for CI
├── generator/            # generation library
│   ├── dashd_manager.py  # dashd process lifecycle management
│   ├── rpc_client.py     # dash-cli RPC wrapper
│   ├── wallet_export.py  # wallet statistics collection and JSON export
│   └── errors.py         # error types
├── tests/                # unit and integration tests
├── data/                 # generated datasets (git-tracked)
└── .github/workflows/    # CI configuration
```

## Development

Install pre-commit hooks:

```bash
pip install pre-commit
pre-commit install
```

Run tests:

```bash
python3 -m pytest
```

Integration tests require a dashd binary. Set `DASHD_PATH` to run them:

```bash
DASHD_PATH=/path/to/dashd python3 -m pytest -m integration
```
