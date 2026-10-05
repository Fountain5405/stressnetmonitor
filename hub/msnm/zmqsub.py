"""Subscribe to monerod's ZMQ publisher (operator nodes and references).

Topics: json-minimal-chain_main {"first_height", "first_prev_id", "ids"} and
json-minimal-txpool_add [{"id", "blob_size", "weight", "fee"}, ...].
Messages are one frame: b"<topic>:<json>". Times are the hub's receive time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable

import zmq
import zmq.asyncio

from .tables import ParquetSink

log = logging.getLogger(__name__)

TOPICS = (b"json-minimal-chain_main", b"json-minimal-txpool_add")


async def subscribe(node_id: str, endpoint: str, sink: ParquetSink,
                    on_block: Callable[[str, int, str, float], None] | None = None,
                    on_tx_count: Callable[[str, int], None] | None = None) -> None:
    ctx = zmq.asyncio.Context.instance()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.RCVHWM, 200_000)
    sock.setsockopt(zmq.RECONNECT_IVL, 1000)
    sock.setsockopt(zmq.RECONNECT_IVL_MAX, 30_000)
    for t in TOPICS:
        sock.setsockopt(zmq.SUBSCRIBE, t)
    sock.connect(endpoint)
    log.info("zmq: subscribed to %s (%s)", node_id, endpoint)
    try:
        while True:
            msg = await sock.recv()
            now = time.time()
            topic, _, payload = msg.partition(b":")
            try:
                doc = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if topic == b"json-minimal-chain_main":
                h0 = int(doc["first_height"])
                prev = doc.get("first_prev_id")
                for i, bh in enumerate(doc.get("ids", [])):
                    sink.add("zmq_block", {"node_id": node_id, "recv_time": now, "height": h0 + i,
                                           "hash": bh, "prev_hash": prev})
                    if on_block:
                        on_block(node_id, h0 + i, bh, now)
                    prev = bh
            elif topic == b"json-minimal-txpool_add":
                for tx in doc:
                    sink.add("zmq_tx", {"node_id": node_id, "recv_time": now, "txid": tx.get("id"),
                                        "blob_size": tx.get("blob_size"), "weight": tx.get("weight"),
                                        "fee": tx.get("fee")})
                if on_tx_count:
                    on_tx_count(node_id, len(doc))
    except asyncio.CancelledError:
        raise
    finally:
        sock.close(linger=0)
