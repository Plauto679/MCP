import os
from mcp.server.fastmcp import FastMCP
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, OrderArgsV2, MarketOrderArgsV2, OrderPayload, OrderType
from dotenv import load_dotenv
import sys
from eth_account import Account
import httpx
import py_clob_client_v2.http_helpers.helpers as clob_http_helpers

# Load environment variables
load_dotenv()

# The v2 client default HTTP/2 session can timeout on POST /order under load.
# Use a longer HTTP/1.1 client so short BTC windows do not miss entries.
clob_http_helpers._http_client = httpx.Client(
    http2=False,
    timeout=httpx.Timeout(20.0, connect=5.0),
)

# Configuration
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Initialize Server
mcp = FastMCP("Polymarket MCP")


def _clob_health_detail() -> str:
    try:
        with httpx.Client(http2=False, timeout=httpx.Timeout(5.0, connect=3.0)) as client:
            resp = client.get(f"{HOST}/ok")
        return f"GET /ok status={resp.status_code} body={resp.text[:80]}"
    except Exception as exc:
        return f"GET /ok failed: {type(exc).__name__}: {exc}"


def _append_connectivity_detail(error_msg: str) -> str:
    if "Request exception" not in str(error_msg):
        return str(error_msg)
    return f"{error_msg} | CLOB connectivity: {_clob_health_detail()}"

def _resolve_signature_type(proxy_address: str | None) -> int:
    """
    Resolve signature type with sane defaults:
    - EOA wallet (no proxy): 0
    - Proxy wallet configured: 1 (POLY_PROXY)
    """
    default_sig_type = 1 if proxy_address else 0
    sig_type_str = os.getenv("POLYMARKET_SIGNATURE_TYPE")
    if not sig_type_str:
        return default_sig_type
    try:
        sig_type = int(sig_type_str)
    except ValueError:
        return default_sig_type
    return sig_type


def get_client() -> ClobClient:
    """Helper to initialize the CLOB v2 client."""
    creds = None
    if POLYMARKET_API_KEY and POLYMARKET_API_SECRET and POLYMARKET_PASSPHRASE:
        creds = ApiCreds(
            api_key=POLYMARKET_API_KEY,
            api_secret=POLYMARKET_API_SECRET,
            api_passphrase=POLYMARKET_PASSPHRASE
        )

    proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
    sig_type = _resolve_signature_type(proxy_address)
    signer_address = Account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else None

    if proxy_address:
        print(
            f"Using Proxy Wallet: {proxy_address} | Signer: {signer_address} | Signature Type {sig_type}",
            file=sys.stderr
        )
        return ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            creds=creds,
            chain_id=CHAIN_ID,
            signature_type=sig_type,
            funder=proxy_address
        )
    else:
        print(f"Using EOA Wallet: {signer_address} (Signature Type {sig_type})", file=sys.stderr)
        return ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            creds=creds,
            chain_id=CHAIN_ID,
            signature_type=sig_type
        )

@mcp.tool()
def get_market(condition_id: str):
    """
    Get market details for a specific condition ID.
    Args:
        condition_id: The unique identifier for the market/condition.
    """
    client = get_client()
    try:
        return client.get_market(condition_id)
    except Exception as e:
        return f"Error fetching market: {str(e)}"

@mcp.tool()
def get_wallet_positions(address: str):
    """
    Get the current positions for a specific wallet address.
    Useful for observing target wallets (Copy Trading).
    """
    import requests
    import json
    url = f"https://data-api.polymarket.com/positions?user={address}"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        return json.dumps(resp.json())
    except Exception as e:
        return f"Error fetching positions: {str(e)}"

def _resolve_order_type(order_type: str):
    order_type_str = str(order_type or "GTC").upper()
    mapping = {
        "GTC": OrderType.GTC,
        "GTD": OrderType.GTD,
        "FAK": OrderType.FAK,
        "FOK": OrderType.FOK,
    }
    return mapping.get(order_type_str), order_type_str


@mcp.tool()
def place_order(
    market_slug: str,
    side: str,
    size: float,
    price: float,
    token_id: str,
    order_type: str = "GTC",
    post_only: bool = False,
    defer_exec: bool = False,
):
    """
    Place a limit order on the CLOB (v2).
    Args:
        market_slug: For reference only.
        side: 'BUY' or 'SELL'.
        size: Amount to buy/sell (in conditional tokens).
        price: Limit price (0–1).
        token_id: The asset ID (token ID) to trade.
    """
    client = get_client()
    if not POLYMARKET_API_KEY:
        return "Error: API Keys not configured. Cannot place orders."

    try:
        import json

        side_str = side.upper()
        if side_str not in ["BUY", "SELL"]:
            return f"Error: Invalid side {side}. Must be BUY or SELL."

        resolved_order_type, order_type_str = _resolve_order_type(order_type)
        if resolved_order_type is None:
            return f"Error: Invalid order_type {order_type}. Must be GTC, GTD, FAK, or FOK."

        # Round size to 2 decimal places (Polymarket requirement)
        rounded_size = round(float(size), 2)

        # Enforce Polymarket's minimum order value of $1
        min_order_value = 1.0
        if side_str == "BUY":
            order_value = rounded_size * float(price)
            if order_value < min_order_value:
                rounded_size = round(min_order_value / float(price), 2)
                if rounded_size * float(price) < min_order_value:
                    rounded_size = round(rounded_size + 0.01, 2)
        else:  # SELL
            order_value = rounded_size * (1.0 - float(price))
            if order_value < min_order_value:
                rounded_size = round(min_order_value / (1.0 - float(price)), 2)
                if rounded_size * (1.0 - float(price)) < min_order_value:
                    rounded_size = round(rounded_size + 0.01, 2)

        order_args = OrderArgsV2(
            price=float(price),
            size=rounded_size,
            side=side_str,
            token_id=token_id
        )

        # create_and_post_order handles both sign + submit atomically (v2)
        resp = client.create_and_post_order(
            order_args=order_args,
            order_type=resolved_order_type,
            post_only=bool(post_only),
            defer_exec=bool(defer_exec),
        )

        try:
            if isinstance(resp, dict):
                return json.dumps(resp, default=str)
            elif hasattr(resp, '__dict__'):
                return json.dumps(resp.__dict__, default=str)
            else:
                return json.dumps({"orderID": str(resp), "raw": str(resp)})
        except Exception:
            return str(resp)

    except Exception as e:
        import traceback
        traceback.print_exc()

        error_msg = str(e)
        if "does not exist" in error_msg:
            return "Error: Market/orderbook does not exist (likely closed or settled). Skipping this trade."
        elif "not enough balance" in error_msg or "allowance" in error_msg:
            return "Error: Insufficient balance or allowance in Proxy Wallet. Please fund your account."
        elif "Unauthorized" in error_msg or "Invalid api key" in error_msg:
            return "Error: API authentication failed. Please regenerate API keys."
        elif "invalid signature" in error_msg.lower():
            proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
            sig_type = _resolve_signature_type(proxy_address)
            signer_address = Account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else "N/A"
            return (
                "Error: invalid signature. "
                f"Signer={signer_address}, Proxy={proxy_address or 'N/A'}, SignatureType={sig_type}. "
                "For Proxy wallet use SignatureType=1 and ensure PRIVATE_KEY controls that proxy. "
                "If using EOA directly, unset POLYMARKET_PROXY_ADDRESS and use SignatureType=0."
            )
        elif "order_version_mismatch" in error_msg:
            return "Error: order_version_mismatch — check that py-clob-client-v2 is installed correctly."
        else:
            return f"Error placing order: {_append_connectivity_detail(error_msg)}"

@mcp.tool()
def place_market_order(
    market_slug: str,
    side: str,
    amount: float,
    token_id: str,
    order_type: str = "FAK",
    defer_exec: bool = False,
):
    """
    Place an immediate marketable order on the CLOB (v2).
    BUY amount is USDC. SELL amount is shares.
    """
    client = get_client()
    if not POLYMARKET_API_KEY:
        return "Error: API Keys not configured. Cannot place orders."

    try:
        import json

        side_str = side.upper()
        if side_str not in ["BUY", "SELL"]:
            return f"Error: Invalid side {side}. Must be BUY or SELL."

        order_type_str = order_type.upper()
        if order_type_str not in ["FAK", "FOK"]:
            return f"Error: Invalid order_type {order_type}. Must be FAK or FOK."
        resolved_order_type = OrderType.FAK if order_type_str == "FAK" else OrderType.FOK

        rounded_amount = round(float(amount), 2)
        if rounded_amount <= 0:
            return "Error: Invalid amount. Must be greater than 0."

        order_args = MarketOrderArgsV2(
            token_id=token_id,
            amount=rounded_amount,
            side=side_str,
            order_type=resolved_order_type,
        )

        resp = client.create_and_post_market_order(
            order_args=order_args,
            order_type=resolved_order_type,
            defer_exec=bool(defer_exec),
        )

        try:
            if isinstance(resp, dict):
                return json.dumps(resp, default=str)
            elif hasattr(resp, '__dict__'):
                return json.dumps(resp.__dict__, default=str)
            else:
                return json.dumps({"orderID": str(resp), "raw": str(resp)})
        except Exception:
            return str(resp)

    except Exception as e:
        import traceback
        traceback.print_exc()

        error_msg = str(e)
        if "not enough balance" in error_msg or "allowance" in error_msg:
            return "Error: Insufficient balance or allowance in Proxy Wallet. Please fund your account."
        elif "Unauthorized" in error_msg or "Invalid api key" in error_msg:
            return "Error: API authentication failed. Please regenerate API keys."
        elif "invalid signature" in error_msg.lower():
            proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
            sig_type = _resolve_signature_type(proxy_address)
            signer_address = Account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else "N/A"
            return (
                "Error: invalid signature. "
                f"Signer={signer_address}, Proxy={proxy_address or 'N/A'}, SignatureType={sig_type}."
            )
        else:
            return f"Error placing market order: {_append_connectivity_detail(error_msg)}"

@mcp.tool()
def get_order(order_id: str):
    """
    Fetch a single CLOB order by ID.
    """
    client = get_client()
    if not POLYMARKET_API_KEY:
        return "Error: API Keys not configured."
    try:
        import json

        resp = client.get_order(str(order_id))
        if isinstance(resp, dict):
            return json.dumps(resp, default=str)
        if hasattr(resp, "__dict__"):
            return json.dumps(resp.__dict__, default=str)
        return json.dumps({"raw": str(resp)})
    except Exception as e:
        return f"Error fetching order: {str(e)}"


@mcp.tool()
def cancel_order(order_id: str):
    """
    Cancel a single CLOB order by ID.
    """
    client = get_client()
    if not POLYMARKET_API_KEY:
        return "Error: API Keys not configured."
    try:
        import json

        resp = client.cancel_order(OrderPayload(orderID=str(order_id)))
        if isinstance(resp, dict):
            return json.dumps(resp, default=str)
        if hasattr(resp, "__dict__"):
            return json.dumps(resp.__dict__, default=str)
        return json.dumps({"raw": str(resp)})
    except Exception as e:
        return f"Error cancelling order: {str(e)}"


@mcp.tool()
def cancel_all():
    """
    Cancel all open orders. Safety switch.
    """
    client = get_client()
    if not POLYMARKET_API_KEY:
        return "Error: API Keys not configured."
    try:
        return client.cancel_all()
    except Exception as e:
        return f"Error cancelling orders: {str(e)}"

@mcp.tool()
def get_balance():
    """
    Get the USDC balance/allowance of the bot's wallet (Proxy or EOA).
    """
    client = get_client()
    if not POLYMARKET_API_KEY:
        return "Error: API Keys not configured."
    try:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        resp = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        return str(resp)
    except Exception as e:
        return f"Error fetching balance: {str(e)}"

if __name__ == "__main__":
    mcp.run()
