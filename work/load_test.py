"""Several people talking at once: how much slower does each one's reply get?

Registers throwaway accounts on a TEST server (never production: it creates accounts), then every simulated user
sends the same recorded sentence as push-to-talk at the same moment, for a few rounds, and the script reports
how long each stage took: recognized (transcript), first reply text, first reply audio, done.

  python work/load_test.py --url http://127.0.0.1:18765 --pcm speech16k.pcm --users 1 5 --rounds 3

--pcm is raw 16 kHz mono PCM16 of one spoken sentence. The server's reply model decides the text stages; run it
against a server with a fixed fake model to measure this machine alone, or with the real key for the whole path.
"""
import argparse
import asyncio
import json
import secrets
import statistics
import time

import httpx
import websockets

STAGES = ("transcript", "first_token", "first_audio", "done")


async def sign_up(client, url):
    email = f"load-{secrets.token_hex(4)}@example.test"
    response = await client.post(url + "/api/register", json={"email": email, "password": secrets.token_urlsafe(16)},
                                 headers={"origin": url})
    response.raise_for_status()
    return "; ".join(f"{name}={value}" for name, value in client.cookies.items())


async def one_user(url, cookie, pcm, rounds, start, results):
    ws_url = url.replace("http", "ws", 1) + "/ws"
    async with websockets.connect(ws_url, additional_headers={"Cookie": cookie, "Origin": url}, max_size=2**23) as socket:
        await start.wait()
        for number in range(1, rounds + 1):
            began, seen = time.perf_counter(), {}
            await socket.send(json.dumps({"type": "audio_start", "request_id": number}))
            for offset in range(0, len(pcm), 32000):
                await socket.send(pcm[offset:offset + 32000])
            await socket.send(json.dumps({"type": "audio_end", "request_id": number}))
            while "done" not in seen:
                message = json.loads(await asyncio.wait_for(socket.recv(), 120))
                if message.get("request_id") != number:
                    continue
                kind = message["type"]
                stage = {"transcript": "transcript", "token": "first_token", "audio": "first_audio", "done": "done", "error": "done"}.get(kind)
                if stage and stage not in seen:
                    seen[stage] = time.perf_counter() - began
                if kind == "error":
                    seen["error"] = message.get("text", "")
            results.append(seen)


async def run(url, pcm, users, rounds):
    async with httpx.AsyncClient(timeout=30) as client:
        cookies = []
        for _ in range(users):
            client.cookies.clear()
            cookies.append(await sign_up(client, url))
    start, results = asyncio.Event(), []
    tasks = [asyncio.create_task(one_user(url, cookie, pcm, rounds, start, results)) for cookie in cookies]
    await asyncio.sleep(1)
    start.set()
    await asyncio.gather(*tasks)
    return results


def summary(results):
    rows = {}
    for stage in STAGES:
        values = sorted(result[stage] for result in results if stage in result)
        if values:
            rows[stage] = (statistics.median(values), values[min(len(values) - 1, int(.95 * len(values)))], values[-1])
    return rows, sum("error" in result for result in results)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:18765")
    parser.add_argument("--pcm", required=True)
    parser.add_argument("--users", type=int, nargs="+", default=[1, 5])
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    pcm = open(args.pcm, "rb").read()
    print(f"sentence: {len(pcm) / 32000:.1f} s of audio, {args.rounds} rounds per user")
    print(f"{'users':>5}  {'stage':<12} {'p50 s':>7} {'p95 s':>7} {'max s':>7}")
    for users in args.users:
        rows, errors = summary(asyncio.run(run(args.url.rstrip("/"), pcm, users, args.rounds)))
        for stage, (p50, p95, worst) in rows.items():
            print(f"{users:>5}  {stage:<12} {p50:>7.2f} {p95:>7.2f} {worst:>7.2f}")
        if errors:
            print(f"{users:>5}  errors: {errors}")


if __name__ == "__main__":
    main()
