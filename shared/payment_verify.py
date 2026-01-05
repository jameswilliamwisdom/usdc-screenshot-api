"""
Direct USDC Payment Verification System
Monitors Base blockchain for incoming USDC transfers, bypassing x402 CDP facilitator.
"""

import os
import time
import secrets
import threading
from datetime import datetime, timezone
from typing import Optional, Any
from dataclasses import dataclass, field
from enum import Enum

from web3 import Web3


# =============================================================================
# Configuration
# =============================================================================

class Config:
    # Wallet receiving payments
    PAYMENT_WALLET = os.getenv(
        "PAYMENT_WALLET", 
        "0x6b21227Ca9Bb3590BB62ff60BA0EFbBf9Ba22ACC"
    )
    
    # Base Mainnet RPC (free, no API key needed)
    BASE_RPC_URL = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
    
    # Backup RPCs in case primary is rate-limited
    BACKUP_RPCS = [
        "https://base.llamarpc.com",
        "https://base.meowrpc.com",
        "https://1rpc.io/base",
    ]
    
    # USDC contract on Base
    USDC_CONTRACT = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    
    # ERC-20 Transfer event topic (keccak256 of "Transfer(address,address,uint256)")
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    
    # Payment expiry (seconds)
    PAYMENT_EXPIRY_SECONDS = int(os.getenv("PAYMENT_EXPIRY_SECONDS", "300"))
    
    # Block confirmations to wait (protection against reorgs)
    REQUIRED_CONFIRMATIONS = 2
    
    # Cleanup interval for expired payments (seconds)
    CLEANUP_INTERVAL = 60


# =============================================================================
# Pricing
# =============================================================================

# Prices in USDC micro-units (6 decimals)
# $0.01 = 10000, $0.05 = 50000, $0.10 = 100000
ENDPOINT_PRICES = {
    # Screenshot API
    "/screenshot": 10000,           # $0.01
    "/screenshot/full": 20000,      # $0.02
    
    # PDF API
    "/extract": 10000,              # $0.01
    "/extract/structured": 20000,   # $0.02
    
    # Crypto Sentiment API
    "/sentiment": 10000,            # $0.01
    "/sentiment/market": 50000,     # $0.05
    "/intelligence": 100000,        # $0.10
}

def get_price(endpoint: str) -> int:
    """Get price for endpoint in USDC micro-units. Returns default if not found."""
    # Normalize endpoint
    normalized = "/" + endpoint.strip("/").lower()
    
    # Try exact match first
    if normalized in ENDPOINT_PRICES:
        return ENDPOINT_PRICES[normalized]
    
    # Try case-insensitive exact match
    for key, price in ENDPOINT_PRICES.items():
        if key.lower() == normalized:
            return price
    
    # Try prefix match (for path params like /sentiment/{symbol})
    # Sort by length descending to match most specific first
    sorted_keys = sorted(ENDPOINT_PRICES.keys(), key=len, reverse=True)
    for key in sorted_keys:
        key_base = key.split("{")[0].rstrip("/")  # Remove path params
        if normalized.startswith(key_base) or normalized == key_base:
            return ENDPOINT_PRICES[key]
    
    # Default price
    return 10000  # $0.01


def usdc_to_usd(amount_usdc: int) -> str:
    """Convert USDC micro-units to USD string."""
    return f"{amount_usdc / 1_000_000:.2f}"


# =============================================================================
# Payment Status
# =============================================================================

class PaymentStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXPIRED = "expired"
    USED = "used"
    FAILED = "failed"


@dataclass
class Payment:
    payment_id: str
    endpoint: str
    params: dict
    amount_usdc: int
    created_at: datetime
    expires_at: datetime
    block_at_creation: int
    status: PaymentStatus = PaymentStatus.PENDING
    tx_hash: Optional[str] = None
    confirmed_at: Optional[datetime] = None
    from_address: Optional[str] = None
    
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at
    
    def to_dict(self) -> dict:
        return {
            "payment_id": self.payment_id,
            "endpoint": self.endpoint,
            "params": self.params,
            "amount_usdc": str(self.amount_usdc),
            "amount_usd": usdc_to_usd(self.amount_usdc),
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "status": self.status.value,
            "tx_hash": self.tx_hash,
        }


# =============================================================================
# Blockchain Client
# =============================================================================

class BlockchainClient:
    """Web3 client for querying Base blockchain."""
    
    def __init__(self):
        self._w3: Optional[Web3] = None
        self._rpc_index = 0
        self._all_rpcs = [Config.BASE_RPC_URL] + Config.BACKUP_RPCS
    
    @property
    def w3(self) -> Web3:
        """Lazy-load Web3 connection with automatic fallback."""
        if self._w3 is None or not self._w3.is_connected():
            self._connect()
        return self._w3
    
    def _connect(self):
        """Connect to an RPC endpoint, trying backups if needed."""
        for i in range(len(self._all_rpcs)):
            rpc_url = self._all_rpcs[(self._rpc_index + i) % len(self._all_rpcs)]
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
                if w3.is_connected():
                    self._w3 = w3
                    self._rpc_index = (self._rpc_index + i) % len(self._all_rpcs)
                    print(f"[Blockchain] Connected to {rpc_url}")
                    return
            except Exception as e:
                print(f"[Blockchain] Failed to connect to {rpc_url}: {e}")
        
        raise ConnectionError("Could not connect to any Base RPC endpoint")
    
    def get_current_block(self) -> int:
        """Get current block number."""
        return self.w3.eth.block_number
    
    def check_for_payment(
        self, 
        min_amount: int, 
        since_block: int,
        to_address: str = None
    ) -> Optional[dict]:
        """
        Check for USDC transfers to our wallet since a given block.
        
        Args:
            min_amount: Minimum amount in USDC micro-units (6 decimals)
            since_block: Only check transfers after this block
            to_address: Wallet to check (defaults to Config.PAYMENT_WALLET)
        
        Returns:
            dict with tx_hash, amount, block, from_address if found, None otherwise
        """
        if to_address is None:
            to_address = Config.PAYMENT_WALLET
        
        current_block = self.get_current_block()
        
        # Don't query if we're still at the same block
        if current_block <= since_block:
            return None
        
        # Pad address to 32 bytes for topic filtering
        to_topic = "0x" + to_address[2:].lower().zfill(64)
        
        try:
            logs = self.w3.eth.get_logs({
                "address": Web3.to_checksum_address(Config.USDC_CONTRACT),
                "topics": [
                    Config.TRANSFER_TOPIC,  # Transfer event
                    None,                    # from (any)
                    to_topic,               # to (our wallet)
                ],
                "fromBlock": since_block + 1,
                "toBlock": current_block,
            })
            
            for log in logs:
                # Decode amount from data field
                amount = int(log["data"].hex(), 16)
                
                # Check if amount is sufficient
                if amount >= min_amount:
                    # Decode from address from topics
                    from_address = "0x" + log["topics"][1].hex()[-40:]
                    
                    # Check confirmations
                    confirmations = current_block - log["blockNumber"]
                    if confirmations >= Config.REQUIRED_CONFIRMATIONS:
                        return {
                            "tx_hash": log["transactionHash"].hex(),
                            "amount": amount,
                            "block": log["blockNumber"],
                            "from_address": Web3.to_checksum_address(from_address),
                            "confirmations": confirmations,
                        }
            
            return None
            
        except Exception as e:
            print(f"[Blockchain] Error querying logs: {e}")
            # Try reconnecting on next call
            self._w3 = None
            return None


# =============================================================================
# Payment Manager
# =============================================================================

class PaymentManager:
    """
    Manages payment lifecycle: creation, verification, expiration.
    Thread-safe with automatic cleanup of expired payments.
    """
    
    def __init__(self):
        self._payments: dict[str, Payment] = {}
        self._used_payments: set[str] = set()  # Replay protection
        self._lock = threading.Lock()
        self._blockchain = BlockchainClient()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._running = False
    
    def start(self):
        """Start background cleanup thread."""
        if self._running:
            return
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        print("[PaymentManager] Started cleanup thread")
    
    def stop(self):
        """Stop background cleanup thread."""
        self._running = False
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5)
    
    def _cleanup_loop(self):
        """Periodically clean up expired payments."""
        while self._running:
            time.sleep(Config.CLEANUP_INTERVAL)
            self._cleanup_expired()
    
    def _cleanup_expired(self):
        """Remove expired payments from memory."""
        with self._lock:
            expired_ids = [
                pid for pid, payment in self._payments.items()
                if payment.is_expired() and payment.status == PaymentStatus.PENDING
            ]
            for pid in expired_ids:
                self._payments[pid].status = PaymentStatus.EXPIRED
                print(f"[PaymentManager] Payment {pid} expired")
    
    def _generate_payment_id(self) -> str:
        """Generate unique payment ID."""
        timestamp = int(time.time())
        random_suffix = secrets.token_hex(6)
        return f"PAY-{timestamp}-{random_suffix}"
    
    def create_payment(self, endpoint: str, params: dict = None) -> Payment:
        """
        Create a new payment request.
        
        Args:
            endpoint: API endpoint being purchased
            params: Parameters for the API call
        
        Returns:
            Payment object with all details for the client
        """
        if params is None:
            params = {}
        
        payment_id = self._generate_payment_id()
        amount_usdc = get_price(endpoint)
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(
            now.timestamp() + Config.PAYMENT_EXPIRY_SECONDS,
            tz=timezone.utc
        )
        
        # Get current block for payment window
        try:
            current_block = self._blockchain.get_current_block()
        except Exception as e:
            print(f"[PaymentManager] Warning: Could not get block number: {e}")
            current_block = 0
        
        payment = Payment(
            payment_id=payment_id,
            endpoint=endpoint,
            params=params,
            amount_usdc=amount_usdc,
            created_at=now,
            expires_at=expires_at,
            block_at_creation=current_block,
        )
        
        with self._lock:
            self._payments[payment_id] = payment
        
        print(f"[PaymentManager] Created payment {payment_id} for {endpoint} ({usdc_to_usd(amount_usdc)} USDC)")
        return payment
    
    def verify_payment(self, payment_id: str) -> tuple[PaymentStatus, Optional[Payment]]:
        """
        Verify if a payment has been received.
        
        Args:
            payment_id: The payment ID to verify
        
        Returns:
            Tuple of (status, payment) - payment is None if not found
        """
        with self._lock:
            payment = self._payments.get(payment_id)
            
            if payment is None:
                # Check if it was already used (replay protection)
                if payment_id in self._used_payments:
                    return PaymentStatus.USED, None
                return PaymentStatus.FAILED, None
            
            # Check if already confirmed or used
            if payment.status in (PaymentStatus.CONFIRMED, PaymentStatus.USED):
                return payment.status, payment
            
            # Check if expired
            if payment.is_expired():
                payment.status = PaymentStatus.EXPIRED
                return PaymentStatus.EXPIRED, payment
        
        # Check blockchain for payment (outside lock to avoid blocking)
        result = self._blockchain.check_for_payment(
            min_amount=payment.amount_usdc,
            since_block=payment.block_at_creation,
        )
        
        if result:
            with self._lock:
                payment.status = PaymentStatus.CONFIRMED
                payment.tx_hash = result["tx_hash"]
                payment.from_address = result["from_address"]
                payment.confirmed_at = datetime.now(timezone.utc)
            
            print(f"[PaymentManager] Payment {payment_id} confirmed: {result['tx_hash']}")
            return PaymentStatus.CONFIRMED, payment
        
        return PaymentStatus.PENDING, payment
    
    def mark_used(self, payment_id: str) -> bool:
        """
        Mark a payment as used (after serving the data).
        Provides replay protection.
        
        Returns:
            True if successfully marked, False if already used or not found
        """
        with self._lock:
            payment = self._payments.get(payment_id)
            
            if payment is None:
                return False
            
            if payment.status == PaymentStatus.USED:
                return False
            
            if payment.status != PaymentStatus.CONFIRMED:
                return False
            
            payment.status = PaymentStatus.USED
            self._used_payments.add(payment_id)
            
            # Keep in memory for a while for status queries, then cleanup will remove
            print(f"[PaymentManager] Payment {payment_id} marked as used")
            return True
    
    def get_payment(self, payment_id: str) -> Optional[Payment]:
        """Get payment by ID without verification."""
        with self._lock:
            return self._payments.get(payment_id)
    
    def get_payment_response(self, payment: Payment) -> dict:
        """Format payment for API response."""
        return {
            "payment_id": payment.payment_id,
            "amount_usd": usdc_to_usd(payment.amount_usdc),
            "amount_usdc": str(payment.amount_usdc),
            "pay_to": Config.PAYMENT_WALLET,
            "network": "Base",
            "token": "USDC",
            "token_contract": Config.USDC_CONTRACT,
            "expires_at": payment.expires_at.isoformat(),
            "status": payment.status.value,
        }


# =============================================================================
# Global Instance
# =============================================================================

# Singleton payment manager
_payment_manager: Optional[PaymentManager] = None

def get_payment_manager() -> PaymentManager:
    """Get or create the global PaymentManager instance."""
    global _payment_manager
    if _payment_manager is None:
        _payment_manager = PaymentManager()
        _payment_manager.start()
    return _payment_manager


# =============================================================================
# Convenience Functions
# =============================================================================

def create_payment_request(endpoint: str, params: dict = None) -> dict:
    """
    Create a payment request and return formatted response.
    
    Usage:
        response = create_payment_request("/screenshot", {"url": "https://example.com"})
    """
    manager = get_payment_manager()
    payment = manager.create_payment(endpoint, params)
    return manager.get_payment_response(payment)


def verify_payment_request(payment_id: str) -> dict:
    """
    Verify a payment and return status.
    
    Usage:
        result = verify_payment_request("PAY-1704567890-abc123")
        if result["status"] == "confirmed":
            # Serve the data
    """
    manager = get_payment_manager()
    status, payment = manager.verify_payment(payment_id)
    
    if payment is None:
        return {
            "status": status.value,
            "message": "Payment not found" if status == PaymentStatus.FAILED else "Payment already used",
        }
    
    response = {
        "status": status.value,
        "payment_id": payment.payment_id,
    }
    
    if status == PaymentStatus.CONFIRMED:
        response["tx_hash"] = payment.tx_hash
        response["from_address"] = payment.from_address
    elif status == PaymentStatus.PENDING:
        response["message"] = "Payment not yet detected"
        response["expires_at"] = payment.expires_at.isoformat()
    elif status == PaymentStatus.EXPIRED:
        response["message"] = "Payment window closed"
    
    return response


def get_stored_params(payment_id: str) -> Optional[tuple[str, dict]]:
    """
    Get the stored endpoint and params for a confirmed payment.
    Returns (endpoint, params) tuple or None.
    """
    manager = get_payment_manager()
    payment = manager.get_payment(payment_id)
    
    if payment and payment.status == PaymentStatus.CONFIRMED:
        return payment.endpoint, payment.params
    
    return None


def mark_payment_used(payment_id: str) -> bool:
    """Mark payment as used after serving data."""
    manager = get_payment_manager()
    return manager.mark_used(payment_id)
