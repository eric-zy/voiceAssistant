import asyncio
import re
import time
import html
from urllib.parse import urljoin
import httpx

async def probe(base):
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            page = await client.get(base)
            links = re.findall(r'href=["\']([^"\']+)["\']', page.text)
            chosen = next((urljoin(base,html.unescape(link)) for link in links if 'torch-2.3.1' in link and 'cp311-cp311-win_amd64' in link),None)
            print(base, 'wheel', chosen, flush=True)
            if chosen:
                start=time.monotonic()
                async with client.stream('GET',chosen.split('#')[0],headers={'Range':'bytes=0-1048575'}) as response:
                    count=0
                    async for block in response.aiter_bytes():
                        count+=len(block)
                        if count>=1048576: break
                    print(base, response.status_code, response.url.host, count, round(time.monotonic()-start,2),flush=True)
    except Exception as error: print(base,type(error).__name__,flush=True)

async def main():
    await asyncio.gather(*(probe(base) for base in [
        'https://mirrors.aliyun.com/pytorch-wheels/cu121/']))
asyncio.run(main())
