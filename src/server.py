import os
from mcp.server.fastmcp import FastMCP
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from dotenv import load_dotenv
import sys

# Load environment variables
load_dotenv()

# Configuration
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY")
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET")
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

# Initialize Server
mcp = FastMCP("Polymarket MCP")

def get_client() -> ClobClient:
    """Helper to initialize the CLOB client."""
    creds = None
    if POLYMARKET_API_KEY and POLYMARKET_API_SECRET and POLYMARKET_PASSPHRASE:
        creds = ApiCreds(
            api_key=POLYMARKET_API_KEY,
            api_secret=POLYMARKET_API_SECRET,
            api_passphrase=POLYMARKET_PASSPHRASE
        )
    
    # Determine connection type
    # 0 = EOA (Default), 1 = Poly Proxy (Magic/Google), 2 = Gnosis Safe
    # If POLYMARKET_PROXY_ADDRESS is set, we assume type 1 (Poly Proxy) for now.
    proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
    
    if proxy_address:
        # Log to stderr to avoid breaking MCP JSON-RPC protocol
        print(f"Using Proxy Wallet: {proxy_address} (Signature Type 2 - Gnosis Safe)", file=sys.stderr)
        return ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            creds=creds,
            chain_id=CHAIN_ID,
            signature_type=2, # 2 = Gnosis Safe (most common for browser wallet connections)
            funder=proxy_address # Funder is the proxy address
        )
    else:
        # Log to stderr to avoid breaking MCP JSON-RPC protocol
        print("Using EOA Wallet (Signature Type 0)", file=sys.stderr)
        # For EOA, the funder is derived from the private key automatically
        # Do NOT pass creds.api_key as funder - that's a UUID, not an address!
        return ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            creds=creds,
            chain_id=CHAIN_ID,
            signature_type=0  # 0 = EOA
            # funder parameter omitted - will be auto-derived from private key
        )

@mcp.tool()
def get_market(condition_id: str):
    """
    Get market details for a specific condition ID.
    Args:
        condition_id: The unique identifier for the market/condition.
    """
    client = get_client()
    # Note: specific method depends on library version, usually get_market or similar
    # If strictly using CLOB API, we might use get_market(condition_id)
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
    # Use Data API for reading positions
    url = f"https://data-api.polymarket.com/positions?user={address}"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        return json.dumps(resp.json())
    except Exception as e:
        return f"Error fetching positions: {str(e)}"

@mcp.tool()
def place_order(market_slug: str, side: str, size: float, price: float, token_id: str):
    """
    Place a limit order on the CLOB.
    Args:
        market_slug: Not used directly in CLOB usually, but for reference. 
                     We need token_id usually.
        side: 'BUY' or 'SELL'.
        size: Amount to buy/sell.
        price: Limit price.
        token_id: The asset ID (token ID) to trade.
    """
    client = get_client()
    if not POLYMARKET_API_KEY:
        return "Error: API Keys not configured. Cannot place orders."
    
    try:
        from py_clob_client.clob_types import OrderType
        # Define constants if not imported
        BUY = "BUY"
        SELL = "SELL"
        
        # Validate Side
        side_str = side.upper()
        if side_str not in [BUY, SELL]:
            return f"Error: Invalid side {side}. Must be BUY or SELL."

        # Round size to 2 decimal places (Polymarket requirement for maker amount)
        rounded_size = round(float(size), 2)
        
        # Enforce Polymarket's minimum order value of $1
        # For BUY orders: total value = size × price
        # For SELL orders: total value = size × (1 - price)
        min_order_value = 1.0
        
        if side_str == "BUY":
            order_value = rounded_size * float(price)
            if order_value < min_order_value:
                # Adjust size upward to meet minimum
                rounded_size = round(min_order_value / float(price), 2)
                # Ensure we round UP to avoid still being under $1
                if rounded_size * float(price) < min_order_value:
                    rounded_size = round(rounded_size + 0.01, 2)
        else:  # SELL
            order_value = rounded_size * (1.0 - float(price))
            if order_value < min_order_value:
                rounded_size = round(min_order_value / (1.0 - float(price)), 2)
                if rounded_size * (1.0 - float(price)) < min_order_value:
                    rounded_size = round(rounded_size + 0.01, 2)
        
        order_args = OrderArgs(
            price=price,
            size=rounded_size,
            side=side_str,
            token_id=token_id
        )
        
        # Use GTC (Good-Til-Canceled) as the standard order type
        # This allows partial fills and better execution than FOK
        
        # 1. Create and Sign
        signed_order = client.create_order(order_args)
        
        # 2. Post with GTC (default, most reliable)
        resp = client.post_order(
            signed_order,
            orderType=OrderType.GTC
        )
        
        # The response is an Order object/Dict, we need to serialize it
        import json
        try:
            # Try to get the ID and other details. usually resp['orderID'] or resp.id
            if isinstance(resp, dict):
                return json.dumps(resp, default=str)
            elif hasattr(resp, '__dict__'):
                return json.dumps(resp.__dict__, default=str)
            else:
                return json.dumps({"orderID": str(resp), "raw": str(resp)})
        except:
             return str(resp)
    except Exception as e:
        import traceback
        traceback.print_exc()
        
        # Check for specific Polymarket errors
        error_msg = str(e)
        if "does not exist" in error_msg:
            return f"Error: Market/orderbook does not exist (likely closed or settled). Skipping this trade."
        elif "not enough balance" in error_msg or "allowance" in error_msg:
            return f"Error: Insufficient balance or allowance in Proxy Wallet. Please fund your account."
        elif "Unauthorized" in error_msg or "Invalid api key" in error_msg:
            return f"Error: API authentication failed. Please regenerate API keys."
        else:
            return f"Error placing order: {error_msg}"

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

if __name__ == "__main__":
    mcp.run()
