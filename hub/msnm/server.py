"""The hub service: sidecar ingest API, reference polling, ZMQ, states, metrics.

Two listeners:
  ingest  (cfg.ingest_host:ingest_port)  /v1/ping /v1/ingest /v1/bundle
          Put this behind HAProxy/TLS; it is the only thing exposed publicly.
  metrics (cfg.metrics_host:metrics_port) /metrics /healthz, for Prometheus.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import zlib
from collections import defaultdict, deque

from aiohttp import web
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from . import zmqsub
from .chain import Chain
from .config import Config
from .live import Monitor
from .rawstore import RawStore
from .records import BatchError, ParserState, parse_batch, parse_header
from .registry import Registry
from .tables import ParquetSink

log = logging.getLogger(__name__)


def ping_text(node_id: str, nl, now: float) -> str:
    """/v1/ping body. The first line stays "pong NODE_ID" for older sidecars;
    the key<TAB>value lines after it tell the operator that data is arriving."""
    lines = [f"pong {node_id}"]
    if nl is not None and nl.last_batch:
        lines += [f"last_batch_age_s\t{max(0, int(now - nl.last_batch))}", f"state\t{nl.state}"]
    return "".join(line + "\n" for line in lines)


def gunzip_limited(data: bytes, limit: int) -> bytes:
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, limit + 1)
    if len(out) > limit or d.unconsumed_tail:
        raise ValueError("decompressed body too large")
    return out


class Hub:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        self.registry = Registry(os.path.join(cfg.data_dir, "registry.sqlite"))
        self.raw = RawStore(cfg.data_dir)
        self.sink = ParquetSink(os.path.join(cfg.data_dir, "parquet"))
        self.chain = Chain(cfg.references, self.sink, cfg.ref_poll_interval_s, cfg.ref_backfill_blocks)
        self.monitor = Monitor(self.chain, self.sink, cfg.thresholds, cfg.fork_height,
                               cfg.min_major_version)
        self.parser_states: dict[str, ParserState] = defaultdict(ParserState)
        self.rate: dict[str, deque] = defaultdict(lambda: deque(maxlen=cfg.max_batches_per_min))
        self.metrics_registry = CollectorRegistry()
        self.metrics_registry.register(self.monitor)
        self._tasks: list[asyncio.Task] = []

    # --------------------------------------------------------------- auth ---

    def _remote(self, request: web.Request) -> str:
        peer = request.remote or ""
        if peer in self.cfg.trusted_proxies:
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[-1].strip()
        return peer

    def _auth(self, request: web.Request):
        h = request.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            raise web.HTTPUnauthorized(text="missing token\n")
        node = self.registry.by_token(h[7:].strip())
        if node is None:
            raise web.HTTPUnauthorized(text="unknown or revoked token\n")
        return node

    def _rate_limit(self, node_id: str) -> None:
        q = self.rate[node_id]
        now = time.time()
        if len(q) == q.maxlen and now - q[0] < 60:
            raise web.HTTPTooManyRequests(text="slow down\n")
        q.append(now)

    # ------------------------------------------------------------- routes ---

    async def ping(self, request: web.Request) -> web.Response:
        node = self._auth(request)
        return web.Response(text=ping_text(node.node_id, self.monitor.nodes.get(node.node_id), time.time()))

    async def ingest(self, request: web.Request) -> web.Response:
        node = self._auth(request)
        self._rate_limit(node.node_id)
        if request.content_length and request.content_length > self.cfg.max_batch_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=self.cfg.max_batch_bytes,
                                                actual_size=request.content_length)
        body = await request.read()
        if len(body) > self.cfg.max_batch_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=self.cfg.max_batch_bytes, actual_size=len(body))
        gz = request.headers.get("Content-Encoding", "").lower() == "gzip"
        try:
            raw_text = gunzip_limited(body, self.cfg.max_batch_text_bytes) if gz else body
        except (zlib.error, ValueError) as e:
            raise web.HTTPBadRequest(text=f"bad body: {e}\n") from None
        if len(raw_text) > self.cfg.max_batch_text_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=self.cfg.max_batch_text_bytes,
                                                actual_size=len(raw_text))
        text = raw_text.decode("utf-8", errors="replace")
        try:
            hdr = parse_header(text.split("\n", 1)[0])
        except BatchError as e:
            raise web.HTTPBadRequest(text=f"{e}\n") from None
        recv = time.time()
        try:
            sent = float(request.headers.get("X-MSNM-Sent", ""))
        except ValueError:
            sent = None
        meta = {"recv_time": recv, "sent": sent, "tier": node.tier,
                "sidecar": request.headers.get("X-MSNM-Sidecar", ""), "remote": self._remote(request)}
        is_new, sha = await asyncio.to_thread(self.raw.save_batch, node.node_id, hdr.seq, body, gz, meta)
        if not is_new:
            return web.Response(text="")
        st = self.parser_states[node.node_id]
        hdr, parsed = parse_batch(text, node.node_id, st)
        self.sink.extend(parsed.rows)
        self.sink.add("batch", {
            "node_id": node.node_id, "time": recv, "seq": hdr.seq, "created": hdr.created,
            "sent": sent, "recv_time": recv, "bytes": len(body), "records": parsed.records,
            "sidecar": hdr.sidecar, "boot_id": hdr.boot_id,
            "clock_offset_s": (sent - recv) if sent else None, "sha256": sha})
        if parsed.bad_lines:
            log.warning("node %s batch %d: %d unparseable lines", node.node_id, hdr.seq, parsed.bad_lines)
        self.monitor.ingest(node, hdr, parsed, recv, sent, len(body))
        cmds = self.monitor.take_commands(node.node_id)
        return web.Response(text="".join(c + "\n" for c in cmds))

    async def bundle(self, request: web.Request) -> web.Response:
        node = self._auth(request)
        self._rate_limit(node.node_id)
        name = request.query.get("name", "")
        body = await request.read()
        if len(body) > self.cfg.max_bundle_bytes:
            raise web.HTTPRequestEntityTooLarge(max_size=self.cfg.max_bundle_bytes, actual_size=len(body))
        meta = {"recv_time": time.time(), "tier": node.tier, "remote": self._remote(request)}
        try:
            path = await asyncio.to_thread(self.raw.save_bundle, node.node_id, name, body, meta)
        except ValueError as e:
            raise web.HTTPBadRequest(text=f"{e}\n") from None
        log.warning("node %s uploaded bundle %s", node.node_id, os.path.basename(path))
        self.sink.add("hub_event", {"time": time.time(), "source": node.node_id, "event": "bundle",
                                    "detail": os.path.relpath(path, self.cfg.data_dir)})
        return web.Response(text="")

    async def metrics(self, request: web.Request) -> web.Response:
        return web.Response(body=generate_latest(self.metrics_registry),
                            headers={"Content-Type": CONTENT_TYPE_LATEST})

    async def healthz(self, request: web.Request) -> web.Response:
        h, _ = self.chain.tip()
        return web.json_response({"ok": True, "canonical_height": h, "nodes": len(self.monitor.nodes),
                                  "pending_rows": self.sink.pending()})

    # -------------------------------------------------------------- tasks ---

    async def _evaluate_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            try:
                self.monitor.evaluate()
            except Exception:  # noqa: BLE001
                log.exception("state evaluation failed")

    def _snapshot_registry(self) -> None:
        now = time.time()
        for n in self.registry.all():
            self.sink.add("node_registry", {"node_id": n.node_id, "tier": n.tier, "position": n.position,
                                            "note": n.note, "created": n.created, "revoked": n.revoked,
                                            "snapshot_time": now})

    async def _flush_loop(self) -> None:
        last_snapshot = 0.0
        while True:
            await asyncio.sleep(self.cfg.flush_interval_s)
            if time.time() - last_snapshot > 3600:
                self._snapshot_registry()
                last_snapshot = time.time()
            try:
                await asyncio.to_thread(self.sink.flush)
            except Exception:  # noqa: BLE001
                log.exception("parquet flush failed")

    async def run(self) -> None:
        ingest = web.Application(client_max_size=max(self.cfg.max_batch_bytes, self.cfg.max_bundle_bytes))
        ingest.add_routes([web.get("/v1/ping", self.ping), web.post("/v1/ingest", self.ingest),
                           web.post("/v1/bundle", self.bundle)])
        metrics = web.Application()
        metrics.add_routes([web.get("/metrics", self.metrics), web.get("/healthz", self.healthz)])
        # auto_decompress=False: keep gzip bodies as received for the raw archive.
        r1 = web.AppRunner(ingest, auto_decompress=False, access_log=None)
        r2 = web.AppRunner(metrics, access_log=None)
        await r1.setup()
        await r2.setup()
        await web.TCPSite(r1, self.cfg.ingest_host, self.cfg.ingest_port).start()
        await web.TCPSite(r2, self.cfg.metrics_host, self.cfg.metrics_port).start()
        log.info("ingest on %s:%d, metrics on %s:%d", self.cfg.ingest_host, self.cfg.ingest_port,
                 self.cfg.metrics_host, self.cfg.metrics_port)

        self._tasks.append(asyncio.create_task(self.chain.run(), name="chain"))
        self._tasks.append(asyncio.create_task(self._evaluate_loop(), name="evaluate"))
        self._tasks.append(asyncio.create_task(self._flush_loop(), name="flush"))
        for ref in self.cfg.references:
            if ref.zmq:
                def on_ref_block(node_id, h, bh, t, ref_id=ref.id):
                    self.chain.note_block(ref_id, h, bh, t)
                    self.monitor.on_zmq_block(node_id, h, bh, t)
                self._tasks.append(asyncio.create_task(
                    zmqsub.subscribe(ref.id, ref.zmq, self.sink, on_ref_block, self.monitor.on_zmq_tx)))
        for zn in self.cfg.zmq_nodes:
            self._tasks.append(asyncio.create_task(
                zmqsub.subscribe(zn.id, zn.zmq, self.sink, self.monitor.on_zmq_block, self.monitor.on_zmq_tx)))
        self.sink.add("hub_event", {"time": time.time(), "source": "hub", "event": "start", "detail": ""})
        self._snapshot_registry()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log.info("shutting down")
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.sink.add("hub_event", {"time": time.time(), "source": "hub", "event": "stop", "detail": ""})
        await asyncio.to_thread(self.sink.flush)
        await r1.cleanup()
        await r2.cleanup()
