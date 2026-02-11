import os
import sys
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON

def create_keys():
    load_dotenv()
    
    # 1. Get Private Key
    key = os.getenv("PRIVATE_KEY")
    if not key:
        print("PRIVATE_KEY not found in .env file.")
        key = input("Please paste your Wallet Private Key (hex): ").strip()
        if not key:
            print("No key provided. Exiting.")
            return

    # Ensure key has 0x prefix if missing
    if not key.startswith("0x"):
        key = "0x" + key

    print(f"Using Private Key: {key[:6]}...{key[-4:]}")
    
    # 2. Get Proxy Address (Optional)
    proxy_address = os.getenv("POLYMARKET_PROXY_ADDRESS")
    
    try:
        # 3. Initialize Client
        # We use the Polygon chain ID (137)
        if proxy_address:
            print(f"Using Proxy Wallet: {proxy_address}")
            client = ClobClient(
                host="https://clob.polymarket.com",
                key=key, 
                chain_id=137,
                signature_type=1,
                funder=proxy_address
            )
        else:
            print("Using EOA Wallet (No Proxy)")
            client = ClobClient(
                host="https://clob.polymarket.com",
                key=key, 
                chain_id=137,
                signature_type=0
            )
        
        print("\nRequesting API Credentials from Polymarket...")
        # 4. Create or Derive API Key
        # This will return existing keys if they exist, or create new ones
        resp = client.create_or_derive_api_creds()
        
        print("\nSUCCESS! Here are your API Credentials:")
        print("------------------------------------------------")
        print(f"POLYMARKET_API_KEY={resp.api_key}")
        print(f"POLYMARKET_API_SECRET={resp.api_secret}")
        print(f"POLYMARKET_PASSPHRASE={resp.api_passphrase}")
        print("------------------------------------------------")
        print("\nPlease copy these values into your .env file.")
        
    except Exception as e:
        print(f"\nError generating keys: {e}")
        print("Ensure your private key is correct (and matches the Proxy owner if using a Proxy).")

if __name__ == "__main__":
    create_keys()
