"""
Efficient RPC client with retry logic and error handling.
"""

import json
import subprocess
import time
from typing import Any

from .errors import DashdConnectionError, InsufficientFundsError, RPCError


class DashRPCClient:
    """
    RPC client for dashd with robust error handling and retry logic.

    All paths are relative or configurable via constructor.
    """

    def __init__(
        self,
        dashcli_path: str = "dash-cli",
        datadir: str | None = None,
        network: str = "regtest",
        rpc_timeout: int = 120,
        max_retries: int = 3,
        rpc_port: int | None = None,
    ):
        self.dashcli = dashcli_path
        self.datadir = datadir
        self.network = network
        self.rpc_timeout = rpc_timeout
        self.max_retries = max_retries
        self.rpc_port = rpc_port

    def call(self, method: str, *params, wallet: str | None = None) -> Any:
        """
        Call RPC method with retry logic and comprehensive error handling.
        """
        for attempt in range(self.max_retries):
            try:
                return self._execute(method, params, wallet)
            except subprocess.TimeoutExpired:
                if attempt == self.max_retries - 1:
                    raise RPCError(f"RPC timeout after {self.rpc_timeout}s: {method}", code=-1) from None
                time.sleep(2**attempt)
            except DashdConnectionError:
                if attempt == self.max_retries - 1:
                    raise
                time.sleep(2**attempt)

    def _execute(self, method: str, params: tuple, wallet: str | None) -> Any:
        """Execute single RPC call"""
        cmd = [self.dashcli, f"-{self.network}"]

        if self.datadir:
            cmd.append(f"-datadir={self.datadir}")

        if self.rpc_port:
            cmd.append(f"-rpcport={self.rpc_port}")

        if wallet:
            cmd.append(f"-rpcwallet={wallet}")

        cmd.append(method)
        for p in params:
            if isinstance(p, bool):
                cmd.append("true" if p else "false")
            elif isinstance(p, (dict, list)):
                cmd.append(json.dumps(p))
            else:
                cmd.append(str(p))

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.rpc_timeout)

        if result.returncode != 0:
            self._handle_error(method, result.stderr)

        if not result.stdout:
            return None

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()

    def _handle_error(self, method: str, stderr: str):
        """Parse and raise appropriate error based on stderr"""
        stderr_lower = stderr.lower()

        if "error code: -6" in stderr_lower or "insufficient funds" in stderr_lower:
            raise InsufficientFundsError(
                f"UTXO pool depleted during {method}. This indicates a bug in UTXO management."
            )

        if "error code: -28" in stderr_lower:
            raise RPCError(f"dashd still loading: {method}", code=-28)

        if "could not connect" in stderr_lower or "connection refused" in stderr_lower:
            raise DashdConnectionError(f"Cannot connect to dashd for {method}")

        raise RPCError(f"{method} failed: {stderr}")
