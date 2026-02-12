"""Shared wallet statistics collection and export logic."""

import json
from pathlib import Path

from .errors import RPCError
from .rpc_client import DashRPCClient


def collect_wallet_stats(rpc: DashRPCClient, wallet_name: str) -> dict:
    """Collect transaction history, UTXOs, balance, and mnemonic for a wallet.

    Returns a dict with keys: wallet_name, mnemonic, transactions, utxos, balance.
    """
    transactions = []
    try:
        txs = rpc.call("listtransactions", "*", 999999999, 0, True, wallet=wallet_name)
        for tx in txs:
            transactions.append(
                {
                    "txid": tx["txid"],
                    "address": tx.get("address", ""),
                    "amount": tx["amount"],
                    "confirmations": tx.get("confirmations", 0),
                    "blockhash": tx.get("blockhash", ""),
                    "time": tx.get("time", 0),
                }
            )
    except RPCError as e:
        print(f"    Warning: Error getting transactions for {wallet_name}: {e}")

    utxos = []
    balance = 0.0
    try:
        wallet_utxos = rpc.call("listunspent", 1, 9999999, [], wallet=wallet_name)
        utxos = [
            {
                "txid": u["txid"],
                "vout": u["vout"],
                "address": u.get("address"),
                "amount": u["amount"],
                "confirmations": u.get("confirmations", 0),
            }
            for u in wallet_utxos
        ]
        balance = sum(u["amount"] for u in wallet_utxos)
    except RPCError as e:
        print(f"    Warning: Error getting UTXOs for {wallet_name}: {e}")

    mnemonic = ""
    try:
        hd_info = rpc.call("dumphdinfo", wallet=wallet_name)
        mnemonic = hd_info.get("mnemonic", "")
    except RPCError:
        pass

    return {
        "wallet_name": wallet_name,
        "mnemonic": mnemonic,
        "transactions": transactions,
        "utxos": utxos,
        "balance": balance,
    }


def save_wallet_file(wallet_data: dict, output_path: Path) -> None:
    """Save wallet statistics to a JSON file.

    wallet_data should contain: wallet_name, mnemonic, balance, transactions, utxos.
    """
    export_data = {
        "wallet_name": wallet_data["wallet_name"],
        "mnemonic": wallet_data.get("mnemonic", ""),
        "balance": wallet_data["balance"],
        "transaction_count": len(wallet_data["transactions"]),
        "unique_transaction_count": len({tx["txid"] for tx in wallet_data["transactions"]}),
        "utxo_count": len(wallet_data["utxos"]),
        "transactions": wallet_data["transactions"],
        "utxos": wallet_data["utxos"],
    }

    with open(output_path, "w") as f:
        json.dump(export_data, f, indent=2)
