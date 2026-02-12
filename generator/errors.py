"""
Custom exception hierarchy for test data generation.

Provides detailed error types for better error handling and user feedback.
"""


class GeneratorError(Exception):
    """Base exception for all generator errors"""

    pass


class RPCError(GeneratorError):
    """RPC communication failures"""

    def __init__(self, message: str, code: int = None):
        super().__init__(message)
        self.code = code


class InsufficientFundsError(GeneratorError):
    """
    UTXO pool depleted.

    This should never happen with proper UTXO management.
    """

    pass


class ConfigError(GeneratorError):
    """Invalid configuration"""

    pass


class DashdConnectionError(RPCError):
    """Cannot connect to dashd"""

    pass


class TransactionCreationError(GeneratorError):
    """Transaction creation failed"""

    def __init__(self, message: str, tx_type: str = None):
        super().__init__(message)
        self.tx_type = tx_type
