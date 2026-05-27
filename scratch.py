from py_order_utils.model import OrderData
from py_order_utils.model.sides import BUY, SELL
from py_order_utils.model.signatures import EOA, POLY_GNOSIS_SAFE, POLY_PROXY

def validate(data):
    return not (
        data.maker is None
        or data.tokenId is None
        or data.makerAmount is None
        or data.takerAmount is None
        or data.side is None
        or data.side not in [BUY, SELL]
        or not data.feeRateBps.isnumeric()
        or int(data.feeRateBps) < 0
        or not data.nonce.isnumeric()
        or int(data.nonce) < 0
        or not data.expiration.isnumeric()
        or int(data.expiration) < 0
        or data.signatureType is None
        or data.signatureType not in [EOA, POLY_GNOSIS_SAFE, POLY_PROXY]
    )

def print_failures(data):
    if data.maker is None: print("maker is None")
    if data.tokenId is None: print("tokenId is None")
    if data.makerAmount is None: print("makerAmount is None")
    if data.takerAmount is None: print("takerAmount is None")
    if data.side is None: print("side is None")
    if data.side not in [BUY, SELL]: print(f"side {data.side} not in {[BUY, SELL]}")
    if not str(data.feeRateBps).isnumeric(): print(f"feeRateBps '{data.feeRateBps}' not numeric")
    elif int(data.feeRateBps) < 0: print("feeRateBps < 0")
    if not str(data.nonce).isnumeric(): print(f"nonce '{data.nonce}' not numeric")
    elif int(data.nonce) < 0: print("nonce < 0")
    if not str(data.expiration).isnumeric(): print(f"expiration '{data.expiration}' not numeric")
    elif int(data.expiration) < 0: print("expiration < 0")
    if data.signatureType is None: print("signatureType is None")
    if data.signatureType not in [EOA, POLY_GNOSIS_SAFE, POLY_PROXY]: print(f"signatureType {data.signatureType} not in {[EOA, POLY_GNOSIS_SAFE, POLY_PROXY]}")

data = OrderData(
    maker="0x...",
    taker="0x0000000000000000000000000000000000000000",
    tokenId="13677181594827528597691565761490893811230238996962475563806027816235946371099",
    makerAmount="100000",
    takerAmount="200000",
    side=0,
    feeRateBps="0",
    nonce="0",
    signer="0x...",
    expiration="0",
    signatureType=3
)
print("Valid?", validate(data))
print_failures(data)
