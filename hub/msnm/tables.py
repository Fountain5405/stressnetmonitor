"""Parquet table schemas and the buffered writer.

Types are chosen to read cleanly in R (arrow, duckdb):
  - times are timestamp[us, UTC]
  - heights and small counts are int32
  - byte counts, counters and anything that may exceed 2^31 are float64
    (exact up to 2^53; avoids R's integer64)
  - 128-bit difficulties are kept as the hex strings monerod returns
    (wide_*), plus a float64 for plotting
Table and column names follow Rucknium/monerod-monitor where they overlap
(info, last_block_header, pool_stats, pool_stats_histo, fee_estimate,
connections, bans, process_info), with node_id added.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from collections import defaultdict

import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

TS = pa.timestamp("us", tz="UTC")
I32 = pa.int32()
F64 = pa.float64()
STR = pa.string()
BOOL = pa.bool_()


def _schema(*cols: tuple[str, pa.DataType]) -> pa.Schema:
    return pa.schema([pa.field(n, t) for n, t in cols])


# Columns every sidecar-derived row carries.
_NODE = (("node_id", STR), ("time", TS))
# Columns every RPC-derived row carries.
_RPC = (("rpc_http_code", I32), ("rpc_response_time", F64))

SCHEMAS: dict[str, pa.Schema] = {
    # ---- RPC (monerod-monitor compatible) ----
    "info": _schema(
        *_NODE,
        ("adjusted_time", F64), ("alt_blocks_count", I32), ("block_size_limit", F64),
        ("block_size_median", F64), ("block_weight_limit", F64), ("block_weight_median", F64),
        ("busy_syncing", BOOL), ("credits", F64), ("cumulative_difficulty", F64),
        ("cumulative_difficulty_top64", F64), ("database_size", F64), ("difficulty", F64),
        ("difficulty_top64", F64), ("free_space", F64), ("grey_peerlist_size", I32),
        ("height", I32), ("incoming_connections_count", I32), ("mainnet", BOOL),
        ("nettype", STR), ("offline", BOOL), ("outgoing_connections_count", I32),
        ("restricted", BOOL), ("rpc_connections_count", I32), ("stagenet", BOOL),
        ("start_time", F64), ("status", STR), ("synchronized", BOOL), ("target", I32),
        ("target_height", I32), ("testnet", BOOL), ("top_block_hash", STR), ("top_hash", STR),
        ("tx_count", F64), ("tx_pool_size", I32), ("update_available", BOOL), ("version", STR),
        ("white_peerlist_size", I32), ("wide_cumulative_difficulty", STR), ("wide_difficulty", STR),
        *_RPC,
    ),
    "last_block_header": _schema(
        *_NODE,
        ("block_size", F64), ("block_weight", F64), ("cumulative_difficulty", F64),
        ("cumulative_difficulty_top64", F64), ("depth", I32), ("difficulty", F64),
        ("difficulty_top64", F64), ("hash", STR), ("height", I32), ("long_term_weight", F64),
        ("major_version", I32), ("miner_tx_hash", STR), ("minor_version", I32), ("nonce", F64),
        ("num_txes", I32), ("orphan_status", BOOL), ("pow_hash", STR), ("prev_hash", STR),
        ("reward", F64), ("timestamp", F64), ("wide_cumulative_difficulty", STR),
        ("wide_difficulty", STR),
        *_RPC,
    ),
    "pool_stats": _schema(
        *_NODE,
        ("bytes_max", F64), ("bytes_med", F64), ("bytes_min", F64), ("bytes_total", F64),
        ("fee_total", F64), ("histo_98pc", F64), ("num_10m", I32), ("num_double_spends", I32),
        ("num_failing", I32), ("num_not_relayed", I32), ("oldest", F64), ("txs_total", I32),
        *_RPC,
    ),
    "pool_stats_histo": _schema(*_NODE, ("histo_num", STR), ("bytes", F64), ("txs", I32)),
    "fee_estimate": _schema(
        *_NODE, ("fee", F64), ("fee_tier_1", F64), ("fee_tier_2", F64), ("fee_tier_3", F64),
        ("fee_tier_4", F64), ("fee_tier_5", F64), ("quantization_mask", F64), *_RPC,
    ),
    "connections": _schema(
        *_NODE,
        ("address", STR), ("address_type", I32), ("avg_download", F64), ("avg_upload", F64),
        ("connection_id", STR), ("current_download", F64), ("current_upload", F64),
        ("height", I32), ("host", STR), ("incoming", BOOL), ("ip", STR), ("live_time", F64),
        ("local_ip", BOOL), ("localhost", BOOL), ("peer_id", STR), ("port", STR),
        ("pruning_seed", F64), ("recv_count", F64), ("recv_idle_time", F64),
        ("rpc_credits_per_hash", F64), ("rpc_port", I32), ("send_count", F64),
        ("send_idle_time", F64), ("state", STR), ("support_flags", I32),
    ),
    "bans": _schema(*_NODE, ("host", STR), ("ip", F64), ("seconds", F64)),
    "process_info": _schema(
        *_NODE,
        ("cpu_time_user", F64), ("cpu_time_system", F64), ("num_threads", I32),
        ("mem_rss", F64), ("mem_vms", F64), ("mem_swap", F64), ("mem_hwm", F64),
        ("mem_rss_anon", F64), ("mem_rss_file", F64),
        ("pid", I32), ("state", STR), ("minflt", F64), ("majflt", F64),
        ("io_read_bytes", F64), ("io_write_bytes", F64),
        ("voluntary_ctxt_switches", F64), ("nonvoluntary_ctxt_switches", F64),
        ("start_ticks", F64),
    ),
    "rpc_error": _schema(
        *_NODE, ("method", STR), ("rpc_http_code", I32), ("rpc_response_time", F64),
        ("error", STR),
    ),
    # ---- host (cumulative counters as read from /proc; take differences) ----
    "host_sample": _schema(
        *_NODE,
        ("cpu_user", F64), ("cpu_nice", F64), ("cpu_system", F64), ("cpu_idle", F64),
        ("cpu_iowait", F64), ("cpu_irq", F64), ("cpu_softirq", F64), ("cpu_steal", F64),
        ("ctxt", F64), ("procs_running", I32), ("procs_blocked", I32),
        ("mem_total_kb", F64), ("mem_free_kb", F64), ("mem_avail_kb", F64),
        ("buffers_kb", F64), ("cached_kb", F64), ("dirty_kb", F64), ("writeback_kb", F64),
        ("swap_total_kb", F64), ("swap_free_kb", F64),
        ("load1", F64), ("load5", F64), ("load15", F64),
        ("pgmajfault", F64), ("pswpin", F64), ("pswpout", F64),
        ("net_rx_bytes", F64), ("net_tx_bytes", F64),
        ("psi_cpu_some_us", F64), ("psi_cpu_full_us", F64),
        ("psi_io_some_us", F64), ("psi_io_full_us", F64),
        ("psi_memory_some_us", F64), ("psi_memory_full_us", F64),
    ),
    "host_disk": _schema(
        *_NODE, ("device", STR), ("reads", F64), ("read_sectors", F64), ("read_ms", F64),
        ("writes", F64), ("write_sectors", F64), ("write_ms", F64), ("in_flight", F64),
        ("io_ms", F64), ("weighted_io_ms", F64),
    ),
    # ---- monerod log ----
    "block_added": _schema(
        *_NODE, ("log_time", TS), ("height", I32), ("hash", STR), ("pow_hash", STR),
        ("difficulty", F64), ("reward", F64), ("base_reward", F64), ("fee", F64),
        ("coinbase_weight", F64), ("cumulative_weight", F64),
        ("block_processing_ms", F64), ("target_ms", F64), ("longhash_ms", F64),
    ),
    # From --show-time-stats 1. `height` is the block's height (monerod prints
    # height + 1). All times in ms; total_ms = block_processing_ms + addblock_ms
    # + advance_tree_ms (block_processing_ms stops before those two steps).
    "block_timing": _schema(
        *_NODE, ("log_time", TS), ("height", I32), ("coinbase_weight", F64),
        ("cumulative_weight", F64), ("block_processing_ms", F64),
        ("target_ms", F64), ("longhash_ms", F64), ("t1_ms", F64), ("t2_ms", F64),
        ("t3_ms", F64), ("t_exists_ms", F64), ("t_pool_ms", F64), ("t_checktx_ms", F64),
        ("t_dblspnd_ms", F64), ("tac_ms", F64), ("vmt_ms", F64), ("addblock_ms", F64),
        ("advance_tree_ms", F64), ("total_ms", F64), ("breakdown_raw", STR),
    ),
    "tx_pool_log": _schema(
        *_NODE, ("log_time", TS), ("txid", STR), ("weight", F64), ("fee_per_byte", F64),
        ("pool_count", F64), ("pool_total_weight", F64),
    ),
    "chain_event_log": _schema(
        *_NODE, ("log_time", TS), ("kind", STR), ("height", I32), ("hash", STR), ("message", STR),
    ),
    "log_warning": _schema(
        *_NODE, ("log_time", TS), ("level", STR), ("category", STR), ("location", STR),
        ("thread", STR), ("message", STR),
    ),
    # ---- sidecar meta ----
    "node_profile": _schema(*_NODE, ("key", STR), ("value", STR)),
    "sidecar_event": _schema(*_NODE, ("event", STR), ("detail", STR)),
    # One row per accepted push. clock_offset_s = sidecar send time - hub
    # receive time (positive: node clock ahead; includes network latency).
    "batch": _schema(
        *_NODE, ("seq", F64), ("created", TS), ("sent", TS), ("recv_time", TS),
        ("bytes", F64), ("records", I32), ("sidecar", STR), ("boot_id", STR),
        ("clock_offset_s", F64), ("sha256", STR),
    ),
    # ---- hub-observed ----
    "ref_block": _schema(
        ("ref_id", STR), ("first_seen", TS), ("height", I32), ("hash", STR), ("prev_hash", STR),
        ("timestamp", F64), ("major_version", I32), ("minor_version", I32),
        ("block_size", F64), ("block_weight", F64), ("long_term_weight", F64),
        ("num_txes", I32), ("reward", F64), ("difficulty", F64),
        ("wide_cumulative_difficulty", STR),
        # True for blocks loaded when the hub started; their first_seen is
        # the hub's start time, not when the block appeared.
        ("backfilled", BOOL),
    ),
    "zmq_block": _schema(("node_id", STR), ("recv_time", TS), ("height", I32), ("hash", STR),
                         ("prev_hash", STR)),
    "zmq_tx": _schema(("node_id", STR), ("recv_time", TS), ("txid", STR), ("blob_size", F64),
                      ("weight", F64), ("fee", F64)),
    "node_state": _schema(
        ("node_id", STR), ("time", TS), ("state", STR), ("prev_state", STR), ("reason", STR),
        ("height", I32), ("lag", I32),
    ),
    "hub_event": _schema(("time", TS), ("source", STR), ("event", STR), ("detail", STR)),
    "node_registry": _schema(
        ("node_id", STR), ("tier", STR), ("position", STR), ("note", STR), ("created", TS),
        ("revoked", TS), ("snapshot_time", TS),
    ),
}

# Which column decides a row's date partition.
_TIME_COL = {"ref_block": "first_seen", "zmq_block": "recv_time", "zmq_tx": "recv_time",
             "node_registry": "snapshot_time"}


def to_ts(v) -> dt.datetime | None:
    """Unix seconds (float/str) or datetime -> aware UTC datetime."""
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromtimestamp(float(v), tz=dt.timezone.utc)


def _coerce(value, typ: pa.DataType):
    if value is None:
        return None
    try:
        if typ == STR:
            return value if isinstance(value, str) else str(value)
        if typ == I32:
            if isinstance(value, bool):
                return int(value)
            v = int(value)
            return v if -2**31 <= v < 2**31 else None
        if typ == F64:
            return float(value)
        if typ == BOOL:
            if isinstance(value, str):
                return value.lower() in ("1", "true", "yes")
            return bool(value)
        if pa.types.is_timestamp(typ):
            return to_ts(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value


def coerce_row(row: dict, schema: pa.Schema) -> dict:
    return {f.name: _coerce(row.get(f.name), f.type) for f in schema}


class ParquetSink:
    """Buffers rows per table and writes them as Parquet parts:

        <root>/<table>/date=YYYY-MM-DD/part-<unix_ms>-<n>.parquet

    Each flush writes new part files (written to a temp name, then renamed),
    so readers never see partial files. `msnm compact` merges a day's parts.
    """

    def __init__(self, root: str):
        self.root = root
        self._rows: dict[str, list[dict]] = defaultdict(list)
        self._lock = threading.Lock()
        self._n = 0

    def add(self, table: str, row: dict) -> None:
        if table not in SCHEMAS:
            raise KeyError(table)
        with self._lock:
            self._rows[table].append(row)

    def extend(self, rows: dict[str, list[dict]]) -> None:
        with self._lock:
            for table, lst in rows.items():
                if table not in SCHEMAS:
                    raise KeyError(table)
                self._rows[table].extend(lst)

    def pending(self) -> int:
        with self._lock:
            return sum(len(v) for v in self._rows.values())

    def flush(self) -> int:
        with self._lock:
            rows, self._rows = self._rows, defaultdict(list)
        written = 0
        stamp = int(time.time() * 1000)
        for table, lst in rows.items():
            if not lst:
                continue
            schema = SCHEMAS[table]
            tcol = _TIME_COL.get(table, "time")
            by_date: dict[str, list[dict]] = defaultdict(list)
            for r in lst:
                cr = coerce_row(r, schema)
                t = cr.get(tcol)
                by_date[t.strftime("%Y-%m-%d") if t else "unknown"].append(cr)
            for date, drows in by_date.items():
                d = os.path.join(self.root, table, f"date={date}")
                os.makedirs(d, exist_ok=True)
                self._n += 1
                path = os.path.join(d, f"part-{stamp}-{self._n:06d}.parquet")
                tbl = pa.Table.from_pylist(drows, schema=schema)
                pq.write_table(tbl, path + ".tmp", compression="zstd")
                os.replace(path + ".tmp", path)
                written += len(drows)
        if written:
            log.info("flushed %d rows", written)
        return written


def compact_day(root: str, table: str, date: str) -> int:
    """Merge one day's part files of one table into a single file."""
    d = os.path.join(root, table, f"date={date}")
    parts = sorted(p for p in os.listdir(d) if p.startswith("part-") and p.endswith(".parquet"))
    if len(parts) <= 1:
        return len(parts)
    tbl = pa.concat_tables([pq.read_table(os.path.join(d, p), schema=SCHEMAS[table]) for p in parts])
    out = os.path.join(d, f"compact-{int(time.time() * 1000)}.parquet")
    pq.write_table(tbl, out + ".tmp", compression="zstd")
    os.replace(out + ".tmp", out)
    for p in parts:
        os.remove(os.path.join(d, p))
    return len(parts)
