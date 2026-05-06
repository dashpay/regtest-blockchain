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

## Masternode Network

A separate generator produces a regtest chain with a fully-active 4-masternode network and real DIP-0024 rotating quorums, intended for SPV masternode-list-sync integration tests:

```bash
curl -LO https://github.com/dashpay/regtest-blockchain/releases/latest/download/regtest-mn.tar.gz
tar -xzf regtest-mn.tar.gz
```

The exported directory contains 5 dashd datadirs (1 controller + 4 masternodes), the `network.json` metadata file, and exported wallet stats:

```
regtest-mn/
├── network.json          # MN keys, proTxHashes, chain_height, dkg_cycles_completed, dashd args
├── controller/regtest/   # controller datadir (full chain + wallet)
├── mn1/regtest/          # masternode 1 datadir
├── mn2/regtest/
├── mn3/regtest/
├── mn4/regtest/
└── wallets/wallet.json   # exported wallet stats (mnemonics, addresses, txs)
```

Consumers (e.g. `rust-dashcore/dash-spv` integration tests) point at the directory via env var:

```bash
DASHD_PATH=/path/to/dashd \
DASHD_MN_DATADIR=/path/to/regtest-mn \
cargo test -p dash-spv --test dashd_masternode
```

The chain ships with 8 successfully-mined DKG cycles for both `llmq_test` (type 100, 3 members) and `llmq_test_dip0024` (type 103, 4 members, rotating). Every commit is real (non-zero `quorumPublicKey`); the exit tip lands in the DKG Idle gap so consumers can drive a fresh `mine_dkg_cycle` from phase 1 cleanly.

Generate locally:

```bash
python3 generate_masternode.py --dashd-path /path/to/dashd

# Custom number of DKG cycles (default: 8)
python3 generate_masternode.py --dashd-path /path/to/dashd --dkg-cycles 12

# Custom output directory
python3 generate_masternode.py --dashd-path /path/to/dashd --output-dir /tmp/output
```

End-to-end generation takes a few minutes — masternodes run as separate dashd processes and walk through DKG phases 1-6 with message-count gating that mirrors Dash Core's `mine_quorum` / `mine_cycle_quorum` test helpers.

## Project Structure

```
├── generate.py                  # block-mining + wallet generation script
├── generate_masternode.py       # masternode-network + DKG cycle generation script
├── export_wallets.py            # re-export wallet data from existing blockchain
├── contrib/
│   └── setup-dashd.py           # cross-platform dashd binary download for CI
├── generator/                   # generation library
│   ├── dashd_manager.py         # dashd process lifecycle management
│   ├── masternode_network.py    # multi-node masternode network manager
│   ├── rpc_client.py            # dash-cli RPC wrapper
│   ├── wallet_export.py         # wallet statistics collection and JSON export
│   └── errors.py                # error types
├── tests/                       # unit and integration tests
├── data/                        # generated datasets (git-tracked)
└── .github/workflows/           # CI configuration
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
