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
    
    # We prefer using Creds + Key (for signing) if available
    return ClobClient(
        host=HOST,
        key=PRIVATE_KEY,
        creds=creds,
        chain_id=CHAIN_ID
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
    # Use Data API for reading positions
    url = f"https://data-api.polymarket.com/positions?user={address}"
    try:
        resp = requests.get(url)
        resp.raise_for_status()
        return resp.json()
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
        order_args = OrderArgs(
            price=price,
            size=size,
            side=side.upper(),
            token_id=token_id
        )
        resp = client.create_order(order_args)
        return resp
    except Exception as e:
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
