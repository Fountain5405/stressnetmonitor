"""Parse sidecar batches into table rows.

A batch is text (see sidecar/msnm-sidecar.sh):

    #MSNM1<TAB>seq=..<TAB>created=..<TAB>sidecar=..<TAB>boot_id=..<TAB>monerod_pid=..
    P <ts> <key> <value>
    E <ts> <event> <detail>
    S <ts> <k=v ...>
    M <ts> <k=v ...>
    R <ts> <method> <http_code> <seconds> <json with newlines removed>
    L <ts> <raw monerod log line>

Parsing is a pure function of the batch text plus per-node ParserState (which
carries values like clk_tck across batches), so tables can be rebuilt from
the raw archive with `msnm reparse`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field

from .tables import to_ts


class BatchError(ValueError):
    pass


@dataclass
class BatchHeader:
    seq: int
    created: float | None
    sidecar: str
    boot_id: str
    monerod_pid: str


def parse_header(first_line: str) -> BatchHeader:
    parts = first_line.rstrip("\n").split("\t")
    if not parts or parts[0] != "#MSNM1":
        raise BatchError("missing #MSNM1 header")
    kv = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
    try:
        seq = int(kv["seq"])
    except (KeyError, ValueError):
        raise BatchError("header has no valid seq") from None
    created = None
    try:
        created = float(kv.get("created", ""))
    except ValueError:
        pass
    return BatchHeader(seq, created, kv.get("sidecar", ""), kv.get("boot_id", ""),
                       kv.get("monerod_pid", ""))


@dataclass
class ParserState:
    """Per-node state that spans batches."""
    clk_tck: int = 100
    pending_block: dict | None = None     # multi-line "BLOCK SUCCESSFULLY ADDED"
    pending_alt: dict | None = None       # multi-line "BLOCK ADDED AS ALTERNATIVE"
    profile: dict = field(default_factory=dict)


@dataclass
class Parsed:
    rows: dict[str, list[dict]] = field(default_factory=dict)
    records: int = 0
    bad_lines: int = 0

    def add(self, table: str, row: dict) -> None:
        self.rows.setdefault(table, []).append(row)


def _kv(s: str) -> dict[str, str]:
    out = {}
    for tok in s.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# ------------------------------------------------------------------- RPC ---

_INFO_FIELDS = (
    "adjusted_time alt_blocks_count block_size_limit block_size_median block_weight_limit "
    "block_weight_median busy_syncing credits cumulative_difficulty cumulative_difficulty_top64 "
    "database_size difficulty difficulty_top64 free_space grey_peerlist_size height "
    "incoming_connections_count mainnet nettype offline outgoing_connections_count restricted "
    "rpc_connections_count stagenet start_time status synchronized target target_height testnet "
    "top_block_hash top_hash tx_count tx_pool_size update_available version white_peerlist_size "
    "wide_cumulative_difficulty wide_difficulty"
).split()


def _rpc_rows(p: Parsed, node: str, ts, method: str, code: int, secs: float, body: str) -> None:
    base = {"node_id": node, "time": ts}
    rpc = {"rpc_http_code": code, "rpc_response_time": secs}
    doc = None
    err = None
    if code == 200 and body.strip():
        try:
            doc = json.loads(body)
        except json.JSONDecodeError as e:
            err = f"bad json: {e}"
    else:
        err = "no response" if code == 0 else f"http {code}"
    if doc is not None:
        if "error" in doc:
            err = json.dumps(doc["error"])[:500]
            doc = None
        else:
            res = doc.get("result", doc)  # get_transaction_pool_stats is not JSON-RPC wrapped
            status = res.get("status")
            if status not in (None, "OK"):
                err = f"status {status}"
            doc = res
    if doc is None:
        p.add("rpc_error", {**base, "method": method, **rpc, "error": err or "unknown"})
        return

    if method == "get_info":
        p.add("info", {**base, **{k: doc.get(k) for k in _INFO_FIELDS}, **rpc})
    elif method == "get_last_block_header":
        p.add("last_block_header", {**base, **(doc.get("block_header") or {}), **rpc})
    elif method == "get_fee_estimate":
        fees = doc.get("fees") or []
        row = {**base, "fee": doc.get("fee"), "quantization_mask": doc.get("quantization_mask"), **rpc}
        for i, f in enumerate(fees[:5], 1):
            row[f"fee_tier_{i}"] = f
        p.add("fee_estimate", row)
    elif method == "get_connections":
        for c in doc.get("connections") or []:
            p.add("connections", {**base, **c})
    elif method == "get_bans":
        for b in doc.get("bans") or []:
            p.add("bans", {**base, **b})
    elif method == "get_transaction_pool_stats":
        ps = dict(doc.get("pool_stats") or {})
        histo = ps.pop("histo", None) or []
        p.add("pool_stats", {**base, **ps, **rpc})
        for i, h in enumerate(histo, 1):
            p.add("pool_stats_histo", {**base, "histo_num": f"histo_{i:02d}",
                                       "bytes": h.get("bytes"), "txs": h.get("txs")})


# ------------------------------------------------------------- host / proc ---

_CPU = ("cpu_user", "cpu_nice", "cpu_system", "cpu_idle", "cpu_iowait", "cpu_irq",
        "cpu_softirq", "cpu_steal")
_DISK = ("reads", "read_sectors", "read_ms", "writes", "write_sectors", "write_ms",
         "in_flight", "io_ms", "weighted_io_ms")
_HOST_DIRECT = ("ctxt", "procs_running", "procs_blocked", "mem_total_kb", "mem_free_kb",
                "mem_avail_kb", "buffers_kb", "cached_kb", "dirty_kb", "writeback_kb",
                "swap_total_kb", "swap_free_kb", "pgmajfault", "pswpin", "pswpout")


def _host_rows(p: Parsed, node: str, ts, rest: str) -> None:
    kv = _kv(rest)
    row = {"node_id": node, "time": ts}
    cpu = kv.get("cpu", "").split(",")
    for name, v in zip(_CPU, cpu):
        row[name] = _num(v)
    for k in _HOST_DIRECT:
        row[k] = _num(kv.get(k))
    load = kv.get("load", "").split(",")
    for name, v in zip(("load1", "load5", "load15"), load):
        row[name] = _num(v)
    row["net_rx_bytes"] = _num(kv.get("net_rx"))
    row["net_tx_bytes"] = _num(kv.get("net_tx"))
    for res in ("cpu", "io", "memory"):
        for kind in ("some", "full"):
            row[f"psi_{res}_{kind}_us"] = _num(kv.get(f"psi_{res}_{kind}"))
    p.add("host_sample", row)
    for k, v in kv.items():
        if k.startswith("disk."):
            d = {"node_id": node, "time": ts, "device": k[5:]}
            for name, x in zip(_DISK, v.split(",")):
                d[name] = _num(x)
            p.add("host_disk", d)


def _proc_rows(p: Parsed, st: ParserState, node: str, ts, rest: str) -> None:
    kv = _kv(rest)
    tck = st.clk_tck or 100

    def kb(k):
        v = _num(kv.get(k))
        return v * 1024 if v is not None else None

    ut, stt = _num(kv.get("utime")), _num(kv.get("stime"))
    p.add("process_info", {
        "node_id": node, "time": ts,
        "cpu_time_user": ut / tck if ut is not None else None,
        "cpu_time_system": stt / tck if stt is not None else None,
        "num_threads": kv.get("num_threads"),
        "mem_rss": kb("rss_kb"), "mem_vms": kb("vms_kb"), "mem_swap": kb("swap_kb"),
        "mem_hwm": kb("hwm_kb"), "mem_rss_anon": kb("rss_anon_kb"), "mem_rss_file": kb("rss_file_kb"),
        "pid": kv.get("pid"), "state": kv.get("state"),
        "minflt": kv.get("minflt"), "majflt": kv.get("majflt"),
        "io_read_bytes": kv.get("io_read_bytes"), "io_write_bytes": kv.get("io_write_bytes"),
        "voluntary_ctxt_switches": kv.get("vcsw"), "nonvoluntary_ctxt_switches": kv.get("nvcsw"),
        "start_ticks": kv.get("starttime"),
    })


# -------------------------------------------------------------- monerod log ---

_HEIGHT_RE = re.compile(r"^HEIGHT (\d+), difficulty:\t(\d+)")
_REWARD_RE = re.compile(
    r"^block reward: ([\d.]+)\(([\d.]+) \+ ([\d.]+)\), coinbase_weight: (\d+), "
    r"cumulative weight: (\d+), (\d+)\((\d+)/(\d+)\)ms")
_TIMING_RE = re.compile(r"^Height: (\d+) coinbase weight: (\d+) cumm: (\d+) p/t: (\d+) \(([\d/]+)\)ms")
_TIMING_FIELDS = ("target_ms", "longhash_ms", "t1_ms", "t2_ms", "t3_ms", "t_exists_ms",
                  "t_pool_ms", "t_checktx_ms", "t_dblspnd_ms", "tac_ms", "vmt_ms",
                  "addblock_ms", "advance_tree_ms")
_TXPOOL_RE = re.compile(
    r"^Transaction added to pool: txid <([0-9a-f]{64})> weight: (\d+) fee/byte: ([\d.e+-]+), "
    r"count: (\d+), pool total weight: (\d+)")
_HASH_RE = re.compile(r"<([0-9a-f]{64})>")
_REORG_RE = re.compile(r"REORGANIZE on height: (\d+)")
_REORG_OK_RE = re.compile(r"REORGANIZE SUCCESS! on height: (\d+)")
_ALT_RE = re.compile(r"BLOCK ADDED AS ALTERNATIVE ON HEIGHT (\d+)")


def _log_time(s: str):
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _log_rows(p: Parsed, st: ParserState, node: str, ts, raw: str) -> None:
    parts = raw.split("\t", 5)
    if len(parts) < 6:
        p.bad_lines += 1
        return
    when, thread, level, category, location, msg = parts
    lt = _log_time(when)
    base = {"node_id": node, "time": ts, "log_time": lt}

    # Multi-line block messages: every line carries the same prefix, so the
    # continuation lines follow the header line directly.
    if msg == "+++++ BLOCK SUCCESSFULLY ADDED":
        st.pending_block = {**base}
        st.pending_alt = None
        return
    if msg.startswith("----- BLOCK ADDED AS ALTERNATIVE ON HEIGHT"):
        m = _ALT_RE.search(msg)
        st.pending_alt = {**base, "kind": "alt_block", "height": int(m.group(1)) if m else None,
                          "message": msg}
        st.pending_block = None
        return
    if msg.startswith("id:\t"):
        m = _HASH_RE.search(msg)
        h = m.group(1) if m else None
        if st.pending_block is not None:
            st.pending_block["hash"] = h
        elif st.pending_alt is not None:
            st.pending_alt["hash"] = h
            p.add("chain_event_log", st.pending_alt)
            st.pending_alt = None
        return
    if msg.startswith("PoW:\t"):
        if st.pending_block is not None:
            m = _HASH_RE.search(msg)
            st.pending_block["pow_hash"] = m.group(1) if m else None
        return
    m = _HEIGHT_RE.match(msg)
    if m:
        if st.pending_block is not None:
            st.pending_block["height"] = int(m.group(1))
            st.pending_block["difficulty"] = float(m.group(2))
        return
    m = _REWARD_RE.match(msg)
    if m:
        b = st.pending_block
        st.pending_block = None
        if b is not None and "height" in b:
            b.update(reward=float(m.group(1)), base_reward=float(m.group(2)), fee=float(m.group(3)),
                     coinbase_weight=float(m.group(4)), cumulative_weight=float(m.group(5)),
                     block_processing_ms=float(m.group(6)), target_ms=float(m.group(7)),
                     longhash_ms=float(m.group(8)))
            p.add("block_added", b)
        return
    m = _TIMING_RE.match(msg)
    if m:
        vals = [float(x) for x in m.group(5).split("/")]
        row = {**base, "height": int(m.group(1)) - 1, "coinbase_weight": float(m.group(2)),
               "cumulative_weight": float(m.group(3)), "block_processing_ms": float(m.group(4)),
               "breakdown_raw": m.group(5)}
        row.update(zip(_TIMING_FIELDS, vals))
        row["total_ms"] = row["block_processing_ms"] + (row.get("addblock_ms") or 0) + (row.get("advance_tree_ms") or 0)
        p.add("block_timing", row)
        return
    m = _TXPOOL_RE.match(msg)
    if m:
        p.add("tx_pool_log", {**base, "txid": m.group(1), "weight": float(m.group(2)),
                              "fee_per_byte": float(m.group(3)), "pool_count": float(m.group(4)),
                              "pool_total_weight": float(m.group(5))})
        return
    m = _REORG_OK_RE.search(msg)
    if m:
        p.add("chain_event_log", {**base, "kind": "reorganize_success", "height": int(m.group(1)),
                                  "message": msg})
        return
    m = _REORG_RE.search(msg)
    if m:
        p.add("chain_event_log", {**base, "kind": "reorganize", "height": int(m.group(1)),
                                  "message": msg})
        return
    if msg.startswith("BLOCK ADDED AS INVALID") or "orphaned and rejected" in msg:
        m = _HASH_RE.search(msg)
        kind = "invalid_block" if msg.startswith("BLOCK ADDED AS INVALID") else "orphaned"
        p.add("chain_event_log", {**base, "kind": kind, "hash": m.group(1) if m else None,
                                  "message": msg})
        return
    if level in ("WARNING", "ERROR", "FATAL"):
        p.add("log_warning", {**base, "level": level, "category": category, "location": location,
                              "thread": thread, "message": msg})


# ------------------------------------------------------------------ batch ---

def parse_batch(text: str, node: str, st: ParserState) -> tuple[BatchHeader, Parsed]:
    lines = text.split("\n")
    hdr = parse_header(lines[0])
    p = Parsed()
    for line in lines[1:]:
        if not line:
            continue
        typ, _, rest = line.partition("\t")
        ts_s, _, rest = rest.partition("\t")
        try:
            ts = to_ts(ts_s)
        except (ValueError, OverflowError):
            p.bad_lines += 1
            continue
        p.records += 1
        try:
            if typ == "S":
                _host_rows(p, node, ts, rest)
            elif typ == "M":
                _proc_rows(p, st, node, ts, rest)
            elif typ == "R":
                method, code, secs, body = (rest.split("\t", 3) + ["", "", "", ""])[:4]
                _rpc_rows(p, node, ts, method, int(code or 0), _num(secs) or 0.0, body)
            elif typ == "L":
                _log_rows(p, st, node, ts, rest)
            elif typ == "P":
                key, _, value = rest.partition("\t")
                st.profile[key] = value
                if key == "clk_tck" and value.isdigit():
                    st.clk_tck = int(value)
                p.add("node_profile", {"node_id": node, "time": ts, "key": key, "value": value})
            elif typ == "E":
                event, _, detail = rest.partition("\t")
                p.add("sidecar_event", {"node_id": node, "time": ts, "event": event, "detail": detail})
            else:
                p.bad_lines += 1
        except (ValueError, KeyError, TypeError, AttributeError):
            p.bad_lines += 1
    return hdr, p
