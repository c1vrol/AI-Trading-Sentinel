import asyncio
import aiohttp

async def get_fapi_oi_hist(symbol):
    # Binance endpoint: GET /futures/data/openInterestHist
    # Requires symbol, period, limit
    # e.g. symbol="BTCUSDT", period="15m", limit=2
    url = f"https://fapi.binance.com/futures/data/openInterestHist"
    params = {
        "symbol": symbol.replace('/', ''),
        "period": "15m",
        "limit": 2
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as resp:
            data = await resp.json()
            print(data)

asyncio.run(get_fapi_oi_hist("BTCUSDT"))
