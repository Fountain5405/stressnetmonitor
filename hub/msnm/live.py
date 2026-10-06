"""Live per-node view: derived rates, node states, and Prometheus metrics.

This is a convenience layer for the dashboard and for flagging incidents.
Analysis should use the Parquet tables, which hold the raw counters.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .chain import Chain
from .records import BatchHeader, Parsed
from .registry import Node
from .tables import ParquetSink

log = logging.getLogger(__name__)

STATES = ("UNKNOWN", "OK", "BEHIND", "FALLING_BEHIND", "STALLED", "UNRESPONSIVE", "DOWN",
          "FORKED", "WRONG_CHAIN", "SYNCING")


@dataclass
class Thresholds:
    silent_after_s: float = 120       # no pushes for this long: DOWN
    unresponsive_polls: int = 2       # consecutive failed get_info: UNRESPONSIVE
    behind_blocks: int = 2            # lag at or above this: BEHIND
    falling_slope_per_h: float = 2.0  # lag growth (blocks/hour) for FALLING_BEHIND
    slope_window_s: float = 1800
    stall_after_s: float = 600        # height unchanged while the network moved
    syncing_lag: int = 30             # lag above this while busy_syncing: SYNCING
    fork_confirm_s: float = 90        # tip hash must disagree this long: FORKED


def _ts(t) -> float:
    return t.timestamp() if t is not None else 0.0


@dataclass
class NodeLive:
    node: Node
    last_batch: float = 0.0
    batches: int = 0
    bytes: int = 0
    offsets: deque = field(default_factory=lambda: deque(maxlen=20))
    profile: dict = field(default_factory=dict)
    monerod_alive_at: float = 0.0     # latest sign of life (process sample / found)
    monerod_exit_at: float = 0.0      # latest exit event
    info: dict | None = None
    info_time: float = 0.0
    info_fail_streak: int = 0
    header: dict | None = None
    pool: dict | None = None
    fee: dict | None = None
    connections: int | None = None
    bans: int | None = None
    rpc_seconds: dict = field(default_factory=dict)
    host_prev: dict | None = None
    host: dict = field(default_factory=dict)
    disk_prev: dict = field(default_factory=dict)
    disk: dict = field(default_factory=dict)
    proc_prev: dict | None = None
    proc: dict = field(default_factory=dict)
    block_timing: dict | None = None
    block_delay: float | None = None
    warnings: int = 0
    chain_events: int = 0
    lag_hist: deque = field(default_factory=lambda: deque(maxlen=720))
    height: int | None = None
    height_changed_at: float = 0.0
    canon_height_at_change: int | None = None
    fork_since: float | None = None
    state: str = "UNKNOWN"
    reason: str = ""
    state_since: float = field(default_factory=time.time)
    commands: list = field(default_factory=list)
    capture_sent_for: float | None = None

    @property
    def clock_offset(self) -> float | None:
        # sent - recv = node clock offset - network latency; the max over
        # recent pushes is the least latency-affected estimate.
        return max(self.offsets) if self.offsets else None


class Monitor:
    def __init__(self, chain: Chain, sink: ParquetSink, thresholds: Thresholds,
                 fork_height: int = 0, min_major_version: int = 0):
        self.chain = chain
        self.sink = sink
        self.th = thresholds
        self.fork_height = fork_height
        self.min_major_version = min_major_version
        self.nodes: dict[str, NodeLive] = {}
        self.zmq_tx: dict[str, int] = {}
        self.zmq_last_block: dict[str, float] = {}

    def live(self, node: Node) -> NodeLive:
        nl = self.nodes.get(node.node_id)
        if nl is None:
            nl = self.nodes[node.node_id] = NodeLive(node)
        else:
            nl.node = node
        return nl

    # ------------------------------------------------------------- ingest ---

    def ingest(self, node: Node, hdr: BatchHeader, parsed: Parsed, recv_time: float,
               sent: float | None, nbytes: int) -> None:
        nl = self.live(node)
        nl.last_batch = recv_time
        nl.batches += 1
        nl.bytes += nbytes
        if sent:
            nl.offsets.append(sent - recv_time)
        rows = parsed.rows
        for r in rows.get("node_profile", []):
            nl.profile[r["key"]] = r["value"]
        for r in rows.get("sidecar_event", []):
            if r["event"] == "monerod_exit":
                nl.monerod_exit_at = max(nl.monerod_exit_at, _ts(r["time"]))
            elif r["event"] == "monerod_found":
                nl.monerod_alive_at = max(nl.monerod_alive_at, _ts(r["time"]))
        for r in rows.get("process_info", []):
            nl.monerod_alive_at = max(nl.monerod_alive_at, _ts(r["time"]))
        for r in rows.get("info", []):
            nl.info, nl.info_time = r, _ts(r["time"])
            nl.info_fail_streak = 0
        for r in rows.get("rpc_error", []):
            nl.rpc_seconds[r["method"]] = r.get("rpc_response_time")
            if r["method"] == "get_info":
                nl.info_fail_streak += 1
        for r in rows.get("last_block_header", []):
            nl.header = r
        for r in rows.get("pool_stats", []):
            nl.pool = r
        for r in rows.get("fee_estimate", []):
            nl.fee = r
        for table in ("info", "last_block_header", "pool_stats", "fee_estimate"):
            for r in rows.get(table, []):
                nl.rpc_seconds[{"info": "get_info", "last_block_header": "get_last_block_header",
                                "pool_stats": "get_transaction_pool_stats",
                                "fee_estimate": "get_fee_estimate"}[table]] = r.get("rpc_response_time")
        if rows.get("info"):
            # connections/bans arrive in the same poll as get_info; none means zero
            t = rows["info"][-1]["time"]
            nl.connections = sum(1 for r in rows.get("connections", []) if r["time"] == t)
            nl.bans = sum(1 for r in rows.get("bans", []) if r["time"] == t)
        for r in rows.get("host_sample", []):
            self._host(nl, r)
        for r in rows.get("host_disk", []):
            self._disk(nl, r)
        for r in rows.get("process_info", []):
            self._proc(nl, r)
        for r in rows.get("block_timing", []):
            nl.block_timing = r
        for r in rows.get("block_added", []):
            self._block_delay(nl, r)
        nl.warnings += len(rows.get("log_warning", []))
        nl.chain_events += len(rows.get("chain_event_log", []))

    def _host(self, nl: NodeLive, r: dict) -> None:
        prev, nl.host_prev = nl.host_prev, r
        h = nl.host
        h["mem_avail_bytes"] = (r.get("mem_avail_kb") or 0) * 1024
        h["mem_total_bytes"] = (r.get("mem_total_kb") or 0) * 1024
        if r.get("swap_total_kb") is not None and r.get("swap_free_kb") is not None:
            h["swap_used_bytes"] = (r["swap_total_kb"] - r["swap_free_kb"]) * 1024
        h["load1"] = r.get("load1")
        h["procs_blocked"] = r.get("procs_blocked")
        if not prev:
            return
        dt_s = _ts(r["time"]) - _ts(prev["time"])
        if dt_s <= 0:
            return
        cpu = ("cpu_user", "cpu_nice", "cpu_system", "cpu_idle", "cpu_iowait", "cpu_irq",
               "cpu_softirq", "cpu_steal")
        d = {k: (r.get(k) or 0) - (prev.get(k) or 0) for k in cpu}
        total = sum(d.values())
        if total > 0:
            h["cpu_busy_ratio"] = 1 - (d["cpu_idle"] + d["cpu_iowait"]) / total
            h["cpu_iowait_ratio"] = d["cpu_iowait"] / total
            h["cpu_steal_ratio"] = d["cpu_steal"] / total
        for k in ("pgmajfault", "pswpin", "pswpout", "net_rx_bytes", "net_tx_bytes"):
            if r.get(k) is not None and prev.get(k) is not None:
                h[f"{k}_rate"] = (r[k] - prev[k]) / dt_s
        for k in ("psi_cpu_some_us", "psi_io_some_us", "psi_io_full_us", "psi_memory_some_us",
                  "psi_memory_full_us"):
            if r.get(k) is not None and prev.get(k) is not None:
                h[k.replace("_us", "_ratio")] = (r[k] - prev[k]) / (dt_s * 1e6)

    def _disk(self, nl: NodeLive, r: dict) -> None:
        dev = r["device"]
        prev = nl.disk_prev.get(dev)
        nl.disk_prev[dev] = r
        if not prev:
            return
        dt_s = _ts(r["time"]) - _ts(prev["time"])
        if dt_s <= 0:
            return
        g = lambda k: (r.get(k) or 0) - (prev.get(k) or 0)  # noqa: E731
        nl.disk[dev] = {
            "util_ratio": min(1.0, g("io_ms") / (dt_s * 1000)),
            "read_bytes_rate": g("read_sectors") * 512 / dt_s,
            "write_bytes_rate": g("write_sectors") * 512 / dt_s,
            "await_ms": (g("read_ms") + g("write_ms")) / max(1, g("reads") + g("writes")),
        }

    def _proc(self, nl: NodeLive, r: dict) -> None:
        prev, nl.proc_prev = nl.proc_prev, r
        p = nl.proc
        p["rss_bytes"] = r.get("mem_rss")
        p["swap_bytes"] = r.get("mem_swap")
        p["threads"] = r.get("num_threads")
        p["state_d"] = 1 if r.get("state") == "D" else 0
        if not prev or prev.get("pid") != r.get("pid"):
            return
        dt_s = _ts(r["time"]) - _ts(prev["time"])
        if dt_s <= 0:
            return
        cpu = lambda x: (x.get("cpu_time_user") or 0) + (x.get("cpu_time_system") or 0)  # noqa: E731
        p["cpu_cores"] = (cpu(r) - cpu(prev)) / dt_s
        for k in ("majflt", "io_read_bytes", "io_write_bytes"):
            try:
                p[f"{k}_rate"] = (float(r[k]) - float(prev[k])) / dt_s
            except (KeyError, TypeError, ValueError):
                pass

    def _block_delay(self, nl: NodeLive, r: dict) -> None:
        if r.get("log_time") is None or r.get("height") is None:
            return
        seen = self.chain.first_seen_at(r["height"], r.get("hash"))
        if seen is None:
            return
        off = nl.clock_offset or 0.0
        nl.block_delay = (_ts(r["log_time"]) - off) - seen

    def on_zmq_block(self, node_id: str, height: int, block_hash: str, t: float) -> None:
        self.zmq_last_block[node_id] = t

    def on_zmq_tx(self, node_id: str, n: int) -> None:
        self.zmq_tx[node_id] = self.zmq_tx.get(node_id, 0) + n

    # -------------------------------------------------------------- states ---

    def evaluate(self, now: float | None = None) -> None:
        now = now or time.time()
        canon_h, _ = self.chain.tip()
        for nl in self.nodes.values():
            state, reason, lag = self._classify(nl, now, canon_h)
            if state != nl.state:
                log.info("node %s: %s -> %s (%s)", nl.node.node_id, nl.state, state, reason)
                self.sink.add("node_state", {
                    "node_id": nl.node.node_id, "time": now, "state": state, "prev_state": nl.state,
                    "reason": reason, "height": nl.height, "lag": lag})
                nl.state, nl.reason, nl.state_since = state, reason, now
                if state in ("STALLED", "UNRESPONSIVE") and nl.capture_sent_for != nl.state_since:
                    nl.commands.append("capture_logs")
                    nl.capture_sent_for = nl.state_since

    def _classify(self, nl: NodeLive, now: float, canon_h: int | None) -> tuple[str, str, int | None]:
        th = self.th
        if now - nl.last_batch > th.silent_after_s:
            return "DOWN", f"no data for {int(now - nl.last_batch)}s", None
        if nl.monerod_exit_at > nl.monerod_alive_at:
            return "DOWN", "monerod not running", None
        if nl.info_fail_streak >= th.unresponsive_polls:
            return "UNRESPONSIVE", f"get_info failed {nl.info_fail_streak}x", None
        # info_time is on the node's clock; last_batch on the hub's.
        rpc_age = nl.last_batch + (nl.clock_offset or 0.0) - nl.info_time
        if nl.info is not None and rpc_age > th.silent_after_s:
            return "UNRESPONSIVE", f"no RPC data for {int(rpc_age)}s", None
        if nl.info is None or nl.info.get("height") is None:
            return "UNKNOWN", "no get_info yet", None
        h = int(nl.info["height"]) - 1
        if h != nl.height:
            nl.height, nl.height_changed_at, nl.canon_height_at_change = h, nl.info_time, canon_h
        if canon_h is None:
            return "UNKNOWN", "reference chain unavailable", None
        # Lag against the canonical height when the node took this sample, not
        # now: its get_info can be a minute old (RPC poll + push interval), and
        # blocks sometimes arrive seconds apart, so comparing with the current
        # tip flags healthy nodes as BEHIND.
        canon_then = self.chain.height_at(nl.info_time - (nl.clock_offset or 0.0))
        lag = (canon_h if canon_then is None else min(canon_then, canon_h)) - h
        if not nl.lag_hist or nl.lag_hist[-1][0] != nl.info_time:
            nl.lag_hist.append((nl.info_time, lag))
        mv = (nl.header or {}).get("major_version")
        if h >= self.fork_height and mv is not None and mv < self.min_major_version:
            return "WRONG_CHAIN", f"major_version {mv} after fork height", lag
        canon_hash = self.chain.hash_at(h)
        top_hash = nl.info.get("top_block_hash")
        if canon_hash and top_hash and canon_hash != top_hash:
            nl.fork_since = nl.fork_since or now
            if now - nl.fork_since >= th.fork_confirm_s:
                return "FORKED", f"tip {top_hash[:12]} != canonical {canon_hash[:12]} at {h}", lag
        else:
            nl.fork_since = None
        if (nl.info.get("busy_syncing") or nl.info.get("synchronized") is False) and lag > th.syncing_lag:
            return "SYNCING", f"lag {lag}, busy syncing", lag
        if (nl.canon_height_at_change is not None and canon_h > nl.canon_height_at_change
                and nl.info_time - nl.height_changed_at > th.stall_after_s):
            return "STALLED", f"height {h} unchanged for {int(nl.info_time - nl.height_changed_at)}s", lag
        if lag >= th.behind_blocks:
            slope = self._slope(nl, now)
            if slope is not None and slope > th.falling_slope_per_h:
                return "FALLING_BEHIND", f"lag {lag}, growing {slope:.1f}/h", lag
            return "BEHIND", f"lag {lag}", lag
        return "OK", "", lag

    def _slope(self, nl: NodeLive, now: float) -> float | None:
        pts = [(t, l) for t, l in nl.lag_hist if now - t <= self.th.slope_window_s]
        if len(pts) < 5:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        ml = sum(l for _, l in pts) / n
        var = sum((t - mt) ** 2 for t, _ in pts)
        if var == 0:
            return None
        return sum((t - mt) * (l - ml) for t, l in pts) / var * 3600

    def take_commands(self, node_id: str) -> list[str]:
        nl = self.nodes.get(node_id)
        if not nl or not nl.commands:
            return []
        cmds, nl.commands = nl.commands, []
        return cmds

    # ---------------------------------------------------------- prometheus ---

    def collect(self):
        """prometheus_client custom collector."""
        L = ["node", "tier", "position"]

        def g(name, doc, extra=()):
            return GaugeMetricFamily(f"msnm_{name}", doc, labels=L + list(extra))

        now = time.time()
        m = {k: g(k, d) for k, d in (
            ("node_up", "1 if the node's sidecar reported recently"),
            ("node_last_report_age_seconds", "Seconds since the last push"),
            ("node_height", "Top block height"),
            ("node_lag_blocks", "Canonical height minus node height"),
            ("node_tx_pool_size", "Transactions in the pool (get_info)"),
            ("node_pool_bytes", "Pool size in bytes"),
            ("node_pool_failing", "Pool txs failing verification"),
            ("node_connections", "P2P connections"),
            ("node_connections_out", "Outgoing P2P connections"),
            ("node_connections_in", "Incoming P2P connections"),
            ("node_bans", "Banned hosts"),
            ("node_database_bytes", "LMDB database size"),
            ("node_free_space_bytes", "Free space on the data volume"),
            ("node_clock_offset_seconds", "Estimated node clock offset (positive: ahead)"),
            ("node_block_delay_seconds", "Last block: node acceptance minus first seen by a reference"),
            ("node_block_processing_ms", "Last block: p/t from --show-time-stats"),
            ("node_block_total_ms", "Last block: p/t + addblock + advance_tree"),
            ("node_block_advance_tree_ms", "Last block: FCMP++ curve tree growth"),
            ("node_block_checktx_ms", "Last block: tx checks not covered by the pool cache"),
            ("node_cpu_busy_ratio", "Host CPU busy fraction"),
            ("node_cpu_iowait_ratio", "Host CPU iowait fraction"),
            ("node_cpu_steal_ratio", "Host CPU steal fraction (VMs)"),
            ("node_mem_available_bytes", "Host MemAvailable"),
            ("node_mem_total_bytes", "Host MemTotal"),
            ("node_swap_used_bytes", "Host swap in use"),
            ("node_load1", "Host 1-minute load average"),
            ("node_majfault_rate", "Host major page faults per second"),
            ("node_net_rx_bytes_rate", "Host network receive bytes/s"),
            ("node_net_tx_bytes_rate", "Host network transmit bytes/s"),
            ("node_psi_cpu_some_ratio", "Pressure: share of time some task waited for CPU"),
            ("node_psi_io_some_ratio", "Pressure: share of time some task waited for IO"),
            ("node_psi_io_full_ratio", "Pressure: share of time all tasks waited for IO"),
            ("node_psi_memory_some_ratio", "Pressure: share of time some task waited for memory"),
            ("node_monerod_cpu_cores", "monerod CPU use, in cores"),
            ("node_monerod_rss_bytes", "monerod resident memory"),
            ("node_monerod_swap_bytes", "monerod swapped-out memory"),
            ("node_monerod_majfault_rate", "monerod major page faults per second (LMDB misses)"),
            ("node_monerod_disk_wait", "1 if monerod was in uninterruptible IO wait at the last sample"),
        )}
        rpc_t = g("node_rpc_seconds", "Last RPC response time", ["method"])
        state = g("node_state", "1 for the node's current state", ["state"])
        disk_util = g("node_disk_util_ratio", "Data disk busy fraction", ["device"])
        disk_await = g("node_disk_await_ms", "Data disk average IO latency", ["device"])
        disk_rd = g("node_disk_read_bytes_rate", "Data disk read bytes/s", ["device"])
        disk_wr = g("node_disk_write_bytes_rate", "Data disk write bytes/s", ["device"])
        warn = CounterMetricFamily("msnm_node_log_warnings", "WARNING/ERROR log lines", labels=L)
        batches = CounterMetricFamily("msnm_node_batches", "Batches received", labels=L)
        zmq_tx = CounterMetricFamily("msnm_node_zmq_txpool_adds", "Txs added to pool (ZMQ)", labels=["node"])

        def put(name, labels, v):
            if v is not None:
                m[name].add_metric(labels, float(v))

        for nl in self.nodes.values():
            lb = [nl.node.node_id, nl.node.tier, nl.node.position]
            age = now - nl.last_batch
            put("node_up", lb, 1 if age < self.th.silent_after_s else 0)
            put("node_last_report_age_seconds", lb, age)
            for s in STATES:
                state.add_metric(lb + [s], 1.0 if nl.state == s else 0.0)
            info = nl.info or {}
            if info.get("height") is not None:
                put("node_height", lb, int(info["height"]) - 1)
                canon_h, _ = self.chain.tip()
                if canon_h is not None:
                    put("node_lag_blocks", lb, canon_h - (int(info["height"]) - 1))
            put("node_tx_pool_size", lb, info.get("tx_pool_size"))
            put("node_connections_out", lb, info.get("outgoing_connections_count"))
            put("node_connections_in", lb, info.get("incoming_connections_count"))
            put("node_database_bytes", lb, info.get("database_size"))
            fs = info.get("free_space")
            put("node_free_space_bytes", lb, fs if fs is not None and fs < 2**63 else None)
            put("node_connections", lb, nl.connections)
            put("node_bans", lb, nl.bans)
            pool = nl.pool or {}
            put("node_pool_bytes", lb, pool.get("bytes_total"))
            put("node_pool_failing", lb, pool.get("num_failing"))
            put("node_clock_offset_seconds", lb, nl.clock_offset)
            put("node_block_delay_seconds", lb, nl.block_delay)
            bt = nl.block_timing or {}
            put("node_block_processing_ms", lb, bt.get("block_processing_ms"))
            put("node_block_total_ms", lb, bt.get("total_ms"))
            put("node_block_advance_tree_ms", lb, bt.get("advance_tree_ms"))
            put("node_block_checktx_ms", lb, bt.get("t_checktx_ms"))
            h = nl.host
            put("node_cpu_busy_ratio", lb, h.get("cpu_busy_ratio"))
            put("node_cpu_iowait_ratio", lb, h.get("cpu_iowait_ratio"))
            put("node_cpu_steal_ratio", lb, h.get("cpu_steal_ratio"))
            put("node_mem_available_bytes", lb, h.get("mem_avail_bytes"))
            put("node_mem_total_bytes", lb, h.get("mem_total_bytes"))
            put("node_swap_used_bytes", lb, h.get("swap_used_bytes"))
            put("node_load1", lb, h.get("load1"))
            put("node_majfault_rate", lb, h.get("pgmajfault_rate"))
            put("node_net_rx_bytes_rate", lb, h.get("net_rx_bytes_rate"))
            put("node_net_tx_bytes_rate", lb, h.get("net_tx_bytes_rate"))
            put("node_psi_cpu_some_ratio", lb, h.get("psi_cpu_some_ratio"))
            put("node_psi_io_some_ratio", lb, h.get("psi_io_some_ratio"))
            put("node_psi_io_full_ratio", lb, h.get("psi_io_full_ratio"))
            put("node_psi_memory_some_ratio", lb, h.get("psi_memory_some_ratio"))
            p = nl.proc
            put("node_monerod_cpu_cores", lb, p.get("cpu_cores"))
            put("node_monerod_rss_bytes", lb, p.get("rss_bytes"))
            put("node_monerod_swap_bytes", lb, p.get("swap_bytes"))
            put("node_monerod_majfault_rate", lb, p.get("majflt_rate"))
            put("node_monerod_disk_wait", lb, p.get("state_d"))
            for meth, secs in nl.rpc_seconds.items():
                if secs is not None:
                    rpc_t.add_metric(lb + [meth], float(secs))
            for dev, d in nl.disk.items():
                disk_util.add_metric(lb + [dev], d["util_ratio"])
                disk_await.add_metric(lb + [dev], d["await_ms"])
                disk_rd.add_metric(lb + [dev], d["read_bytes_rate"])
                disk_wr.add_metric(lb + [dev], d["write_bytes_rate"])
            warn.add_metric(lb, nl.warnings)
            batches.add_metric(lb, nl.batches)
        for node_id, n in self.zmq_tx.items():
            zmq_tx.add_metric([node_id], n)

        yield from m.values()
        yield from (rpc_t, state, disk_util, disk_await, disk_rd, disk_wr, warn, batches, zmq_tx)
        yield from self._chain_metrics()

    def _chain_metrics(self):
        ref_h = GaugeMetricFamily("msnm_ref_height", "Reference node top height", labels=["ref"])
        ref_age = GaugeMetricFamily("msnm_ref_last_ok_age_seconds", "Seconds since the reference answered",
                                    labels=["ref"])
        now = time.time()
        for rid, r in self.chain.refs.items():
            if r.top_height is not None:
                ref_h.add_metric([rid], r.top_height)
            ref_age.add_metric([rid], now - r.last_ok if r.last_ok else float("inf"))
        yield ref_h
        yield ref_age
        h, _ = self.chain.tip()
        if h is None:
            return
        yield GaugeMetricFamily("msnm_canonical_height", "Canonical chain height", value=h)
        hdr = self.chain.header(h)
        if hdr:
            yield GaugeMetricFamily("msnm_canonical_block_weight", "Weight of the canonical tip block",
                                    value=hdr.get("block_weight", 0))
            yield GaugeMetricFamily("msnm_canonical_block_txes", "Transactions in the canonical tip block",
                                    value=hdr.get("num_txes", 0))
            yield GaugeMetricFamily("msnm_canonical_block_timestamp", "Timestamp of the canonical tip block",
                                    value=hdr.get("timestamp", 0))
        recent = [self.chain.header(x) for x in range(h - 29, h + 1)]
        recent = [x for x in recent if x]
        if recent:
            yield GaugeMetricFamily("msnm_canonical_txes_last30", "Transactions in the last 30 blocks",
                                    value=sum(x.get("num_txes", 0) for x in recent))
            yield GaugeMetricFamily("msnm_canonical_weight_last30", "Total weight of the last 30 blocks",
                                    value=sum(x.get("block_weight", 0) for x in recent))
