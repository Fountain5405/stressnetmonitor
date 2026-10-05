"""Reference nodes: the canonical chain and when each block first appeared.

The hub polls each reference's RPC directly (they're on the operator LAN).
`get_info` every `poll_interval` seconds; on a new tip, headers for the new
heights are fetched with get_block_headers_range. ZMQ chain_main events from
references (if configured) tighten first-seen times.

Canonical chain = the fresh reference with the highest cumulative difficulty.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp

from .tables import ParquetSink

log = logging.getLogger(__name__)


@dataclass
class RefConfig:
    id: str
    rpc: str
    rpc_login: str | None = None
    zmq: str | None = None


@dataclass
class RefState:
    cfg: RefConfig
    top_height: int | None = None          # height of the top block
    top_hash: str | None = None
    cum_diff: int = 0
    last_ok: float = 0.0
    last_error: str | None = None
    hashes: dict[int, str] = field(default_factory=dict)
    headers: dict[int, dict] = field(default_factory=dict)  # recent headers, for metrics


class Chain:
    def __init__(self, refs: list[RefConfig], sink: ParquetSink, poll_interval: float = 2.0,
                 backfill: int = 720, stale_after: float = 60.0):
        self.refs = {r.id: RefState(r) for r in refs}
        self.sink = sink
        self.poll_interval = poll_interval
        self.backfill = backfill
        self.stale_after = stale_after
        self.first_seen: dict[tuple[int, str], float] = {}
        self._wake = asyncio.Event()

    # ------------------------------------------------------------- queries ---

    def canonical(self) -> RefState | None:
        now = time.time()
        fresh = [r for r in self.refs.values() if r.top_height is not None and now - r.last_ok < self.stale_after]
        return max(fresh, key=lambda r: (r.cum_diff, r.top_height)) if fresh else None

    def tip(self) -> tuple[int | None, str | None]:
        c = self.canonical()
        return (c.top_height, c.top_hash) if c else (None, None)

    def hash_at(self, height: int) -> str | None:
        c = self.canonical()
        if c is None:
            return None
        h = c.hashes.get(height)
        if h is None:
            for r in self.refs.values():
                if r is not c and height in r.hashes and r.top_height is not None and r.top_height >= height:
                    return r.hashes[height]
        return h

    def first_seen_at(self, height: int, block_hash: str | None = None) -> float | None:
        h = block_hash or self.hash_at(height)
        return self.first_seen.get((height, h)) if h else None

    def header(self, height: int) -> dict | None:
        c = self.canonical()
        return c.headers.get(height) if c else None

    # ------------------------------------------------------------- polling ---

    def note_block(self, ref_id: str | None, height: int, block_hash: str, t: float) -> None:
        key = (height, block_hash)
        if key not in self.first_seen or t < self.first_seen[key]:
            self.first_seen[key] = t
        self._wake.set()

    async def _rpc(self, session: aiohttp.ClientSession, ref: RefState, method: str, params=None):
        payload = {"jsonrpc": "2.0", "id": "0", "method": method}
        if params is not None:
            payload["params"] = params
        async with session.post(ref.cfg.rpc.rstrip("/") + "/json_rpc", json=payload) as resp:
            resp.raise_for_status()
            doc = await resp.json(content_type=None)
        if "error" in doc:
            raise RuntimeError(f"{method}: {doc['error']}")
        return doc["result"]

    async def _poll(self, session: aiohttp.ClientSession, ref: RefState) -> None:
        now = time.time()
        info = await self._rpc(session, ref, "get_info")
        top = int(info["height"]) - 1
        top_hash = info["top_block_hash"]
        try:
            ref.cum_diff = int(info.get("wide_cumulative_difficulty", "0x0"), 16)
        except ValueError:
            ref.cum_diff = int(info.get("cumulative_difficulty", 0))
        ref.last_ok = now
        ref.last_error = None
        if ref.top_hash == top_hash and ref.top_height == top:
            return
        initial = ref.top_height is None
        if initial:
            start = max(0, top - self.backfill + 1)
        else:
            # Walk back from the previous tip to find where this tip connects.
            start = min(ref.top_height, top) + 1
            start = max(0, start - 10)
        while start <= top:
            end = min(top, start + 499)
            res = await self._rpc(session, ref, "get_block_headers_range",
                                  {"start_height": start, "end_height": end})
            for hdr in res.get("headers", []):
                self._record_header(ref, hdr, now, backfilled=initial)
            start = end + 1
        # Forget hashes above a lower new tip (a reorg to a shorter chain).
        for h in [h for h in ref.hashes if h > top]:
            del ref.hashes[h]
        ref.top_height, ref.top_hash = top, top_hash
        if len(ref.headers) > 2000:
            for h in sorted(ref.headers)[:-1000]:
                del ref.headers[h]

    def _record_header(self, ref: RefState, hdr: dict, now: float, backfilled: bool) -> None:
        h, bh = int(hdr["height"]), hdr["hash"]
        old = ref.hashes.get(h)
        if old == bh:
            return
        if old is not None:
            log.warning("reference %s reorg at height %d: %s -> %s", ref.cfg.id, h, old[:12], bh[:12])
            self.sink.add("hub_event", {"time": now, "source": ref.cfg.id, "event": "ref_reorg",
                                        "detail": f"height={h} old={old} new={bh}"})
        ref.hashes[h] = bh
        ref.headers[h] = hdr
        seen = self.first_seen.get((h, bh))
        if not backfilled and (seen is None or now < seen):
            self.first_seen[(h, bh)] = now
            seen = now
        self.sink.add("ref_block", {
            "ref_id": ref.cfg.id, "first_seen": seen if seen is not None else now,
            "height": h, "hash": bh, "prev_hash": hdr.get("prev_hash"),
            "timestamp": hdr.get("timestamp"), "major_version": hdr.get("major_version"),
            "minor_version": hdr.get("minor_version"), "block_size": hdr.get("block_size"),
            "block_weight": hdr.get("block_weight"), "long_term_weight": hdr.get("long_term_weight"),
            "num_txes": hdr.get("num_txes"), "reward": hdr.get("reward"),
            "difficulty": hdr.get("difficulty"),
            "wide_cumulative_difficulty": hdr.get("wide_cumulative_difficulty"),
            "backfilled": backfilled,
        })

    async def run(self) -> None:
        auth_by_ref = {}
        for r in self.refs.values():
            if r.cfg.rpc_login:
                user, _, pw = r.cfg.rpc_login.partition(":")
                auth_by_ref[r.cfg.id] = aiohttp.DigestAuthMiddleware(user, pw)
        timeout = aiohttp.ClientTimeout(total=20)
        sessions = {rid: aiohttp.ClientSession(timeout=timeout,
                                               middlewares=(auth_by_ref[rid],) if rid in auth_by_ref else ())
                    for rid in self.refs}
        try:
            while True:
                async def one(ref: RefState):
                    try:
                        await self._poll(sessions[ref.cfg.id], ref)
                    except Exception as e:  # noqa: BLE001 - keep polling whatever happens
                        msg = f"{type(e).__name__}: {e}"
                        if ref.last_error != msg:
                            log.warning("reference %s poll failed: %s", ref.cfg.id, msg)
                            self.sink.add("hub_event", {"time": time.time(), "source": ref.cfg.id,
                                                        "event": "ref_poll_error", "detail": msg[:300]})
                        ref.last_error = msg
                await asyncio.gather(*(one(r) for r in self.refs.values()))
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            for s in sessions.values():
                await s.close()
