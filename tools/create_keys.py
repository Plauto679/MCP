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
    
    try:
        # 2. Initialize Client (Temporary connection to generate keys)
        # We use the Polygon chain ID (137)
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=key, 
            chain_id=137
        )
        
        print("\nRequesting new API Credentials from Polymarket...")
        # 3. Create API Key
        resp = client.create_api_key()
        
        print("\nSUCCESS! Here are your new API Credentials:")
        print("------------------------------------------------")
        print(f"POLYMARKET_API_KEY={resp.api_key}")
        print(f"POLYMARKET_API_SECRET={resp.secret}")
        print(f"POLYMARKET_PASSPHRASE={resp.passphrase}")
        print("------------------------------------------------")
        print("\nPlease copy these values into your .env file.")
        
    except Exception as e:
        print(f"\nError generating keys: {e}")
        print("Ensure your private key is correct and has a small amount of MATIC for any potential signing requirements (though creating keys is usually off-chain signing).")

if __name__ == "__main__":
    create_keys()
