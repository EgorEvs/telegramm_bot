import httpx
from typing import Any

BASE = "https://aclient.1c-hosting.com/1R75512/1R75512_UNF30_a49qeit0xq/odata/standard.odata"
AUTH = ("AvtoTexZap_bot", "AvtoTexZap_bot")

async def fetch(entity: str, top: int = 5) -> Any:
    url = f"{BASE}/{entity}?$top={top}&$format=json"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, auth=AUTH, timeout=20)
        r.raise_for_status()
        return r.json()
