import os
from mcp.server.fastmcp import FastMCP
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs
from dotenv import load_dotenv

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
        print(f"Using Proxy Wallet: {proxy_address} (Signature Type 1)")
        return ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            creds=creds,
            chain_id=CHAIN_ID,
            signature_type=1, # 1 = Polymarket Proxy
            funder=proxy_address # Funder is the proxy address
        )
    else:
        print("Using EOA Wallet (Signature Type 0)")
        return ClobClient(
            host=HOST,
            key=PRIVATE_KEY,
            creds=creds,
            chain_id=CHAIN_ID,
            signature_type=0, # 0 = EOA
            funder=creds.api_key # Actually for EOA funder is just implied or we can explicitly pass the EOA address if needed, but py-clob-client handles basic EOA well. 
            # Note: For EOA, 'funder' arg in ClobClient constructor might be optional or should be the EOA address. 
            # Safest is to let default be standard EOA behavior or explicit.
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

        order_args = OrderArgs(
            price=price,
            size=size,
            side=side_str,
            token_id=token_id
        )
        
        # Use FOK (Fill Or Kill) to ensure immediate execution (mimic Market Order behavior)
        # The create_and_post_order helper in this version doesn't support order_type arg.
        # We must split it: create (sign) -> post
        
        # 1. Create and Sign
        signed_order = client.create_order(order_args)
        
        # 2. Post with FOK
        resp = client.post_order(
            signed_order,
            orderType=OrderType.FOK
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
        return f"Error placing order: {str(e)}"

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
