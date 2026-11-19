import argparse
import asyncio
import json
import math
import statistics
import time
from getpass import getpass
from urllib.parse import quote

import websockets


async def connect_client(index, uri, connect_timeout, max_size, sem):
    async with sem:
        started = time.perf_counter()
        ws = None
        try:
            ws = await asyncio.wait_for(
                websockets.connect(uri, open_timeout=connect_timeout, max_size=max_size, ping_interval=20, ping_timeout=20),
                timeout=connect_timeout + 2,
            )
            # The backend sends 'init' and then 'cursors'; only count an initialized connection.
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=connect_timeout)
                message = json.loads(raw)
                if message.get('type') == 'init':
                    return (ws, (time.perf_counter() - started) * 1000, None)
                if message.get('type') == 'error':
                    raise RuntimeError(str(message.get('message', 'server error')))
        except Exception as exc:
            if ws is not None:
                try:
                    await ws.close()
                except Exception:
                    pass
            return (None, None, f'{type(exc).__name__}: {str(exc)[:140]}')

async def one_level(n, uri, hold_seconds, timeout, batch_size, max_size):
    sockets = []
    errors = []
    latencies = []
    semaphore = asyncio.Semaphore(batch_size)
    begun = time.perf_counter()
    try:
        tasks = [connect_client(i, uri, timeout, max_size, semaphore) for i in range(n)]
        outcomes = await asyncio.gather(*tasks)
        for ws, latency, error in outcomes:
            if ws is not None:
                sockets.append(ws)
                latencies.append(latency)
            else:
                errors.append(error)
        print(f'\nTarget connections: {n}')
        print(f'Initialized: {len(sockets)}/{n} ({len(sockets) / n * 100:.1f}%)')
        print(f'Connection round duration: {time.perf_counter()-begun:.1f}s')
        if latencies:
            ordered = sorted(latencies)
            p95 = ordered[math.ceil(0.95 * len(ordered))-1]
            print(f'Init latency: median {statistics.median(latencies):.0f} ms; p95 {p95:.0f} ms')
        if errors:
            print('Sample connection errors:')
            for e in errors[:5]:
                print('  -', e)
        if sockets:
            print(f'Holding connections for {hold_seconds}s without editing...')
            # No document editing. Server may broadcast messages while held.
            await asyncio.sleep(hold_seconds)
            alive = sum(1 for ws in sockets if ws.state.name == 'OPEN')
            print(f'Sockets still OPEN after hold: {alive}/{len(sockets)}')
    finally:
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)

async def main(args):
    token = getpass('Paste test-account JWT (input hidden): ').strip()
    if not token:
        raise SystemExit('Token required')
    uri = f'{args.base.rstrip("/")}/ws/{quote(args.document_id, safe="")}?token={quote(token, safe="")}'
    print('Read-only test; no edits or document changes will be made.')
    print('Note: connections reuse ONE authenticated account, not unique users.')
    for n in args.levels:
        await one_level(n, uri, args.hold, args.timeout, args.batch, args.max_message_mib * 1024 * 1024)
    print('\nDone. Inspect Docker metrics and verify the document normally afterward.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='ws://localhost:8000', help='WebSocket base URL')
    parser.add_argument('--document-id', required=True)
    parser.add_argument('--levels', nargs='+', type=int, default=[25, 50, 100, 150])
    parser.add_argument('--hold', type=int, default=20)
    parser.add_argument('--timeout', type=int, default=15)
    parser.add_argument('--batch', type=int, default=10)
    parser.add_argument('--max-message-mib', type=int, default=25)
    args = parser.parse_args()
    if any(x <= 0 or x > 500 for x in args.levels):
        parser.error('Level sizes must be between 1 and 500')
    asyncio.run(main(args))