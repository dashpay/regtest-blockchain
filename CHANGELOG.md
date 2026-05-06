# Changelog

## v0.0.4 - masternode network generator

### Added
- masternode network generator producing real DIP-0024 rotating quorums for SPV masternode-list-sync integration tests ([#4](https://github.com/dashpay/regtest-blockchain/pull/4)) @xdustinface

### Assets
- `regtest-mn.tar.gz` — 4-masternode network with 8 successfully-mined DKG cycles (`llmq_test` + `llmq_test_dip0024`)
- `regtest-40000.tar.gz` — full 40K-block chain (unchanged from v0.0.3)
- `regtest-200.tar.gz` — minimal 200-block chain (unchanged from v0.0.3)

**Full Changelog**: https://github.com/dashpay/regtest-blockchain/compare/v0.0.3...v0.0.4

## v0.0.3 - add minimal 200-block test data

### Added
- `regtest-200.tar.gz` minimal 200-block chain (~1.7MB) with two wallets (`default` + `wallet`), sufficient for mempool and basic sync tests

### Assets
- `regtest-40000.tar.gz` — full 40K-block chain (unchanged from v0.0.2)
- `regtest-200.tar.gz` — minimal 200-block chain

**Full Changelog**: https://github.com/dashpay/regtest-blockchain/compare/v0.0.2...v0.0.3

## v0.0.2 - initial SPV integration tests

### Changed
- improve the generator ([#1](https://github.com/dashpay/regtest-blockchain/pull/1)) @xdustinface

### Added
- implement unit and Integration tests for the generator ([#1](https://github.com/dashpay/regtest-blockchain/pull/1)) @xdustinface
- add `pre-commit` and Github actions for basic CI ([#2](https://github.com/dashpay/regtest-blockchain/pull/2)) @xdustinface

**Full Changelog**: https://github.com/dashpay/regtest-blockchain/compare/v0.0.1...v0.0.2

## v0.0.1 - Initial test release
