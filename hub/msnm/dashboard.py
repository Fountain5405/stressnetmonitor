"""Public dashboard: a read-only live view of the network and the nodes.

Its own listener (dashboard.host:port, default 127.0.0.1:8792), published
through HAProxy like the ingest API (see deploy/haproxy.cfg.example):

  /                    the page (msnm/web/index.html)
  /static/<file>       its script, styles and the vendored uPlot
  /api/live.json       network, recent blocks, one entry per node
  /api/history.json    24 h of per-block and per-minute series for the charts

Everything published comes from an allow-list: an arbitrary public ID per
node (a keyed hash of the node id, see public_id), whether it is a reference,
a hardware summary (with what msnm/cpudb.py looks up from the CPU model),
chain, performance and RPC response-time numbers. Node ids, tier, position,
registry notes, monerod arguments, data paths, peer addresses and log text
never leave the hub here. Strings that come from nodes (CPU and disk models,
monerod version) are cleaned and shortened, and the page renders all data as
text.

Chart history lives in memory (History). At startup it is filled from the
Parquet tables in a worker thread, then merged on the event loop, so only the
loop thread ever touches it.
"""

from __future__ import annotations

import bisect
import datetime as dt
import gzip
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import statistics
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
from aiohttp import web

from . import cpudb
from .chain import Chain
from .live import Monitor, NodeLive, _ts
from .registry import Registry

log = logging.getLogger(__name__)

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
WINDOW_S = 24 * 3600     # chart history
BLOCK_KEEP = 1000        # per-block series: blocks within this many of the tip
TARGET_S = 120           # block target; hashrate = difficulty / 120 s
RPC_TIMEOUT_S = 10.0     # the sidecar's RPC_TIMEOUT: a call with no answer by then is a timeout
RPC_SLOW_S = 1.0         # 95th percentile get_info time above this (or any timeout): "RPC slow"
RPC_SPAN_S = 900         # window of the live RPC numbers
RPC_KEEP = 240           # get_info samples kept per node (2 h at one poll per 30 s)
ID_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"   # no 0/O, 1/I/L, U
_ID_RE = re.compile(r"[A-Za-z0-9-]{2,16}")

STATIC = {  # published name -> (file under web/, content type)
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "app.css": ("app.css", "text/css; charset=utf-8"),
    "favicon.svg": ("favicon.svg", "image/svg+xml"),
    "uPlot.iife.min.js": ("vendor/uPlot.iife.min.js", "text/javascript; charset=utf-8"),
    "uPlot.min.css": ("vendor/uPlot.min.css", "text/css; charset=utf-8"),
}

SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                                "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                                "frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


@dataclass
class DashboardConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8792
    title: str = "Monero stressnet monitor"
    subtitle: str = ""
    description: str = ("Live view of a Monero stressnet: the chain, the transaction load, and how "
                        "each monitored node's hardware keeps up.")
    hide: list[str] = field(default_factory=list)        # node ids never published
    id_secret: str = ""                                  # key of the public IDs; keep it stable and private
    aliases: dict[str, str] = field(default_factory=dict)  # node id -> public ID, overriding the hash
    links: list[dict] = field(default_factory=list)      # footer links: [{label, url}]

    def __post_init__(self):
        for link in self.links:
            if not isinstance(link, dict) or not str(link.get("url", "")).startswith(("https://", "http://")):
                raise ValueError(f"dashboard.links entries need an http(s) url: {link!r}")
        if self.enabled and len(self.id_secret) < 16:
            raise ValueError("dashboard.id_secret: set a random string of 16+ characters. It keys the public "
                             "node IDs: changing it changes every ID, and anyone who has it can map IDs to nodes.")
        for nid, pid in self.aliases.items():
            if not _ID_RE.fullmatch(str(pid)):
                raise ValueError(f"dashboard.aliases[{nid!r}]: 2-16 letters, digits or '-'")


def public_id(secret: str, node_id: str, n: int = 4) -> str:
    """A node's public ID: n characters of an HMAC of its node id. Stable for
    a given secret, and without the secret not traceable to the node id."""
    v = int.from_bytes(hmac.new(secret.encode(), node_id.encode(), hashlib.sha256).digest()[:8], "big")
    out = []
    for _ in range(n):
        v, r = divmod(v, len(ID_ALPHABET))
        out.append(ID_ALPHABET[r])
    return "".join(out)


# ------------------------------------------------------------------ helpers ---

_UNSAFE = re.compile(r"[^A-Za-z0-9 .,:;@()+_/#'-]")


def clean(s, n: int = 64) -> str:
    """Node-supplied text for the public page: a safe character set, short."""
    return re.sub(r"\s+", " ", _UNSAFE.sub("", str(s or ""))).strip()[:n]


def _num(v, nd: int | None = None):
    """A JSON-safe number: None for missing or non-finite, optionally rounded."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    if nd is None:
        return int(f) if f.is_integer() and abs(f) < 2**53 else f
    return round(f, nd)


def _int(v) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _median(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


_SIDECAR_RE = re.compile(r"\d{1,4}(?:\.\d{1,4}){1,3}")


def sidecar_version(v) -> str | None:
    """A sidecar version as a node reported it: plain dotted numbers, else None."""
    v = str(v or "").strip()
    return v if _SIDECAR_RE.fullmatch(v) else None


def version_newer(a: str | None, b: str | None) -> bool:
    """Is dotted version a newer than b? False when either is unknown."""
    if not (a and b):
        return False
    return tuple(map(int, a.split("."))) > tuple(map(int, b.split(".")))


# memory sizes machines are sold or rented with, in GiB
_RAM_GIB = (0.5, 1, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024)
# disk size bands, upper bounds in GB; a drive sold as "256 GB" is a little over 256e9 bytes
_DISK_BANDS = ((128, "up to 128 GB"), (256, "128–256 GB"), (512, "256–512 GB"), (1000, "512 GB–1 TB"),
               (2000, "1–2 TB"), (4000, "2–4 TB"), (8000, "4–8 TB"), (16000, "8–16 TB"))


def ram_class(kb: int | None) -> float | None:
    """MemTotal as the size the machine was sold with: the kernel keeps some
    back, so it's the smallest common size at or above it."""
    if not kb:
        return None
    gib = kb / 2**20
    size = next((s for s in _RAM_GIB if s >= gib), None)
    return size if size is not None else math.ceil(gib / 256) * 256


def disk_band(n: int | None) -> str | None:
    if not n:
        return None
    gb = n / 1e9
    return next((label for top, label in _DISK_BANDS if gb <= top * 1.05), "over 16 TB")


def _mem_used(host: dict) -> float | None:
    total, avail = _int(host.get("mem_total_bytes")), _int(host.get("mem_avail_bytes"))
    return round(1 - avail / total, 3) if total and avail is not None else None


def monerod_private(anon, swap) -> float | None:
    """monerod's own memory: anonymous pages plus what is in swap. Resident
    memory also counts LMDB's file-backed pages, which are page cache: large
    on a big box, squeezed out on a small one, while swapped-out pages vanish
    from it though the box still has to hold them."""
    return None if anon is None else float(anon) + float(swap or 0)


def hardware(profile: dict) -> dict:
    """Public hardware summary from a node's profile, by class only. Allow-list:
    the profile also holds monerod arguments, paths and unit files. And no exact
    CPU or disk model, clock, cache or byte count: together those can pick out
    a machine or a hosting plan. node_profile keeps them all for analysis."""
    vm = profile.get("virtualized") == "yes"
    devs = str(profile.get("data_devices") or "").split()
    physical = [d for d in devs if not d.startswith("dm-")] or devs
    disks = []
    for dev in physical[:4]:
        kv = dict(p.split("=", 1) for p in str(profile.get(f"disk.{dev}") or "").split() if "=" in p)
        if vm or kv.get("model", "").startswith("QEMU") or dev.startswith(("vd", "xvd")):
            kind = "virtual disk"
        elif dev.startswith("nvme"):
            kind = "NVMe"
        else:
            kind = "HDD" if kv.get("rotational") == "1" else "SSD"
        sectors = _int(kv.get("sectors"))
        disks.append({"kind": kind, "size": disk_band(sectors * 512 if sectors else None)})
    model, arch = str(profile.get("cpu_model") or ""), profile.get("arch")
    spec = cpudb.lookup(model)
    virtual = bool(spec and spec.virtual)
    maker = cpudb.vendor(model, arch)
    if virtual:
        cpu = spec.gen
    elif spec:
        # the "Core" microarchitecture is the Core 2 line; "Intel Core" reads as today's brand
        cpu = f"{maker} {'Core 2' if spec.gen == 'Core' else spec.gen}".strip()
    else:
        cpu = f"{maker} CPU" if maker else ""
    swap = _int(profile.get("swap_total_kb"))
    swap_gib = swap / 2**20 if swap is not None else None
    return {
        "cpu": clean(cpu, 60),
        "segment": None if virtual else cpudb.segment(model),
        "era": None if virtual or not spec else cpudb.era(spec.year),
        "cores": _int(profile.get("cpu_physical_cores")),
        "threads": _int(profile.get("cpu_logical")),
        "ram_gb": ram_class(_int(profile.get("mem_total_kb"))),
        "swap_gb": None if swap_gib is None else float(round(swap_gib) if swap_gib >= 4 else round(swap_gib * 2) / 2),
        "vm": vm,
        "virtual_cpu": virtual,
        "disks": disks,
        "arch": clean(arch, 16),
        "isa": (cpudb.isa(arch, profile["cpu_flags"], profile.get("cpu_flags_checked"))
                if "cpu_flags" in profile else []),
    }


# ------------------------------------------------------------------ history ---

_CPU = ("cpu_user", "cpu_nice", "cpu_system", "cpu_idle", "cpu_iowait", "cpu_irq", "cpu_softirq", "cpu_steal")
# fields of a per-minute bucket; HT: when H was sampled; RPC: slowest get_info
# (seconds, a timeout counts as RPC_TIMEOUT_S); RPC_TO: get_info timeouts;
# MON: monerod's private memory in bytes (monerod_private)
H, POOL, CPU_SUM, CPU_N, MEM, HT, RPC, RPC_TO, MON = range(9)


def _new_bucket() -> list:
    return [None, None, 0.0, 0, None, None, None, 0, None]


_EMPTY = tuple(_new_bucket())


class History:
    """Chart data for the last 24 h.

    Per node: per-minute buckets (latest top height and when it was sampled,
    pool size, CPU busy, memory in use, monerod's own memory, slowest
    get_info), the recent get_info
    response times, and for blocks near the tip each block's processing time
    and when the node accepted it. Node times are moved onto the hub's clock
    with the node's clock offset. Per ZMQ source: transactions entering the
    pool per minute. `seen` holds block first-seen times from before this hub
    run, and `profiles` each node's last profile (sidecars send it only now
    and then).
    """

    def __init__(self, window_s: float = WINDOW_S):
        self.window_s = window_s
        self.minutes: dict[str, dict[int, list]] = defaultdict(dict)
        self.block_ms: dict[str, dict[int, float]] = defaultdict(dict)
        self.accepted: dict[str, dict[int, tuple[str | None, float]]] = defaultdict(dict)
        self.rpc: dict[str, deque] = defaultdict(lambda: deque(maxlen=RPC_KEEP))  # (t, seconds, timed out)
        self.tx_in: dict[str, dict[int, int]] = defaultdict(dict)
        self.seen: dict[tuple[int, str], float] = {}
        self.profiles: dict[str, dict] = {}
        self._host_prev: dict[str, dict] = {}
        self._pruned = 0.0

    # live input: the rows of one parsed batch (as the Parquet tables get them)
    def record(self, node_id: str, rows: dict, offset: float | None, tip: int | None) -> None:
        for r in rows.get("info", []):
            self._info(node_id, r, offset or 0.0)
            self._rpc(node_id, r, offset or 0.0, answered=True)
        for r in rows.get("rpc_error", []):
            if r.get("method") == "get_info":
                self._rpc(node_id, r, offset or 0.0, answered=False)
        for r in rows.get("host_sample", []):
            self._host(node_id, r)
        for r in rows.get("process_info", []):
            self._proc(node_id, r)
        if tip is None:
            return
        for r in rows.get("block_timing", []):
            self._timing(node_id, r, tip)
        for r in rows.get("block_added", []):
            self._added(node_id, r, offset or 0.0, tip)

    def on_zmq_tx(self, source: str, n: int, t: float) -> None:
        d = self.tx_in[source]
        m = int(t // 60)
        d[m] = d.get(m, 0) + n

    def _bucket(self, node_id: str, t: float) -> list:
        m = int(t // 60)
        b = self.minutes[node_id].get(m)
        if b is None:
            b = self.minutes[node_id][m] = _new_bucket()
        return b

    def _rpc(self, node_id: str, r: dict, offset: float, answered: bool) -> None:
        """One get_info call: an `info` row (answered) or an `rpc_error` row.
        An error with an HTTP status still answered; one with none either
        timed out (it took the sidecar's whole limit) or was refused, which
        says the node is down, not slow, and is left out."""
        secs, t = r.get("rpc_response_time"), r.get("time")
        if secs is None or t is None:
            return
        timed_out = False
        if not answered and not r.get("rpc_http_code"):
            if secs < RPC_TIMEOUT_S * 0.95:
                return
            timed_out, secs = True, RPC_TIMEOUT_S
        t = _ts(t) - offset
        b = self._bucket(node_id, t)
        b[RPC] = secs if b[RPC] is None else max(b[RPC], secs)
        b[RPC_TO] += timed_out
        self.rpc[node_id].append((t, float(secs), timed_out))

    def rpc_stats(self, node_id: str, now: float, span_s: float = RPC_SPAN_S) -> dict | None:
        """get_info response time over the last span_s: median and 95th
        percentile with timeouts counted as slower than everything (so a node
        that often doesn't answer can't look fast), and the timeout count."""
        xs = [(s, to) for t, s, to in self.rpc.get(node_id, ()) if t >= now - span_s]
        if len(xs) < 4:
            return None
        vals = sorted(math.inf if to else s for s, to in xs)

        def q(p: float) -> float:
            v = vals[max(0, math.ceil(p * len(vals)) - 1)]
            return RPC_TIMEOUT_S if v == math.inf else v

        timeouts = sum(1 for _, to in xs if to)
        p95 = q(0.95)
        return {"n": len(xs), "p50_ms": _num(q(0.5) * 1000, 2), "p95_ms": _num(p95 * 1000, 2),
                "timeouts": timeouts, "limit_ms": _num(RPC_TIMEOUT_S * 1000),
                "slow": p95 > RPC_SLOW_S or timeouts > 0}

    def _info(self, node_id: str, r: dict, offset: float) -> None:
        if r.get("height") is None or r.get("time") is None:
            return
        t = _ts(r["time"]) - offset
        b = self._bucket(node_id, t)
        if b[HT] is None or t >= b[HT]:
            b[H], b[HT] = int(r["height"]) - 1, t
        p = r.get("tx_pool_size")
        if p is not None:
            b[POOL] = p if b[POOL] is None else max(b[POOL], p)

    def _host(self, node_id: str, r: dict) -> None:
        if r.get("time") is None:
            return
        prev, self._host_prev[node_id] = self._host_prev.get(node_id), r
        t = _ts(r["time"])
        b = self._bucket(node_id, t)
        total, avail = r.get("mem_total_kb"), r.get("mem_avail_kb")
        if total and avail is not None:
            used = 1 - avail / total
            b[MEM] = used if b[MEM] is None else max(b[MEM], used)
        if not prev or not 0 < t - _ts(prev["time"]) <= 120:
            return
        d = {k: (r.get(k) or 0) - (prev.get(k) or 0) for k in _CPU}
        total_d = sum(d.values())
        if total_d > 0 and min(d.values()) >= 0:   # counters reset at reboot
            b[CPU_SUM] += 1 - (d["cpu_idle"] + d["cpu_iowait"]) / total_d
            b[CPU_N] += 1

    def _proc(self, node_id: str, r: dict) -> None:
        if r.get("time") is None:
            return
        v = monerod_private(r.get("mem_rss_anon"), r.get("mem_swap"))
        if v is not None:
            b = self._bucket(node_id, _ts(r["time"]))
            b[MON] = v if b[MON] is None else max(b[MON], v)

    def _timing(self, node_id: str, r: dict, tip: int) -> None:
        h = r.get("height")
        if h is not None and h >= tip - BLOCK_KEEP and r.get("total_ms") is not None:
            self.block_ms[node_id][h] = float(r["total_ms"])

    def _added(self, node_id: str, r: dict, offset: float, tip: int) -> None:
        h, lt = r.get("height"), r.get("log_time")
        if h is not None and lt is not None and h >= tip - BLOCK_KEEP:
            self.accepted[node_id][h] = (r.get("hash"), _ts(lt) - offset)

    def merge(self, old: History) -> None:
        """Add history loaded from Parquet (rows from before this run)."""
        for node, mins in old.minutes.items():
            mine = self.minutes[node]
            for m, ob in mins.items():
                b = mine.get(m)
                if b is None:
                    mine[m] = ob
                    continue
                if ob[HT] is not None and (b[HT] is None or ob[HT] > b[HT]):
                    b[H], b[HT] = ob[H], ob[HT]
                for i in (POOL, MEM, RPC, MON):
                    if ob[i] is not None:
                        b[i] = ob[i] if b[i] is None else max(b[i], ob[i])
                b[CPU_SUM] += ob[CPU_SUM]
                b[CPU_N] += ob[CPU_N]
                b[RPC_TO] += ob[RPC_TO]
        for node, q in old.rpc.items():
            merged = sorted([*q, *self.rpc.get(node, ())])
            self.rpc[node] = deque(merged[-RPC_KEEP:], maxlen=RPC_KEEP)
        for mine_all, old_all in ((self.block_ms, old.block_ms), (self.accepted, old.accepted)):
            for node, d in old_all.items():
                mine = mine_all[node]
                for h, v in d.items():
                    mine.setdefault(h, v)
        for src, d in old.tx_in.items():
            mine = self.tx_in[src]
            for m, n in d.items():
                mine[m] = mine.get(m, 0) + n
        for k, v in old.seen.items():
            self.seen.setdefault(k, v)
        for node, p in old.profiles.items():
            self.profiles[node] = {**p, **self.profiles.get(node, {})}

    def prune(self, now: float, tip: int | None) -> None:
        if now - self._pruned < 300:
            return
        self._pruned = now
        m0 = int((now - self.window_s) // 60) - 1
        for d in list(self.minutes.values()) + list(self.tx_in.values()):
            for m in [m for m in d if m < m0]:
                del d[m]
        if tip is not None:
            for d in list(self.block_ms.values()) + list(self.accepted.values()):
                for h in [h for h in d if h < tip - BLOCK_KEEP]:
                    del d[h]
        for k in [k for k, t in self.seen.items() if t < now - self.window_s - 3600]:
            del self.seen[k]

    def sync_rate(self, node_id: str, now: float, span_s: float = 3600) -> float | None:
        """Blocks per hour over the last `span_s`, from the per-minute heights."""
        m_now = int(now // 60)
        pts = [(m, b[H]) for m, b in self.minutes.get(node_id, {}).items()
               if b[H] is not None and m >= m_now - span_s // 60]
        if len(pts) < 2:
            return None
        (m0, h0), (m1, h1) = min(pts), max(pts)
        if m1 - m0 < 10:
            return None
        return (h1 - h0) / ((m1 - m0) * 60) * 3600


def _dataset(root: str, table: str) -> ds.Dataset | None:
    path = os.path.join(root, table)
    if not os.path.isdir(path):
        return None
    return ds.dataset(path, format="parquet",
                      partitioning=ds.partitioning(pa.schema([("date", pa.string())]), flavor="hive"))


def _parquet_rows(root: str, table: str, cols: list[str], lo: dt.datetime, hi: dt.datetime,
                  tcol: str = "time", extra=None) -> pa.Table | None:
    d = _dataset(root, table)
    if d is None:
        return None
    days = []
    day = lo.date()
    while day <= hi.date():
        days.append(day.isoformat())
        day += dt.timedelta(days=1)
    f = pc.field("date").isin(days) & (pc.field(tcol) >= lo) & (pc.field(tcol) < hi)
    if extra is not None:
        f = f & extra
    # Little read-ahead: block rows from a syncing node can run to millions a day,
    # and only those near the tip are kept.
    return d.scanner(columns=cols, filter=f, batch_readahead=4, fragment_readahead=2).to_table()


def _rows(t: pa.Table | None):
    """A table's rows as dicts, a batch at a time (a day of host samples is
    ~160k rows)."""
    if t is not None:
        for batch in t.to_batches(max_chunksize=20_000):
            yield from batch.to_pylist()


def load_history(parquet_root: str, until: float, window_s: float = WINDOW_S) -> History:
    """History from the Parquet tables for the `window_s` before `until` (the
    hub's start time; later rows reach History live). Runs in a worker thread."""
    hist = History(window_s)
    lo = dt.datetime.fromtimestamp(until - window_s, dt.timezone.utc)
    hi = dt.datetime.fromtimestamp(until, dt.timezone.utc)

    d = _dataset(parquet_root, "node_profile")    # all of it: a profile can be days old
    if d is not None:
        t = d.to_table(columns=["node_id", "time", "key", "value"])
        for r in _rows(t.sort_by("time")):
            hist.profiles.setdefault(r["node_id"], {})[r["key"]] = r["value"]

    t = _parquet_rows(parquet_root, "ref_block", ["first_seen", "height", "hash", "backfilled"], lo, hi,
                      tcol="first_seen")
    tip = None
    for r in _rows(t):
        tip = r["height"] if tip is None else max(tip, r["height"])
        if not r["backfilled"] and r["first_seen"] is not None:
            k = (r["height"], r["hash"])
            s = _ts(r["first_seen"])
            hist.seen[k] = min(s, hist.seen.get(k, s))

    offsets: dict[str, float] = {}
    t = _parquet_rows(parquet_root, "batch", ["node_id", "time", "clock_offset_s"], lo, hi)
    if t is not None and t.num_rows:
        by_node: dict[str, list] = defaultdict(list)
        for r in _rows(t.sort_by("time")):
            if r["clock_offset_s"] is not None:
                by_node[r["node_id"]].append(r["clock_offset_s"])
        offsets = {n: max(v[-20:]) for n, v in by_node.items()}   # as NodeLive.clock_offset

    hist.rpc = defaultdict(deque)   # unbounded while answers and errors are added one table after the other
    t = _parquet_rows(parquet_root, "info", ["node_id", "time", "height", "tx_pool_size", "rpc_response_time"],
                      lo, hi)
    if t is not None and t.num_rows:
        for r in _rows(t.sort_by([("node_id", "ascending"), ("time", "ascending")])):
            hist._info(r["node_id"], r, offsets.get(r["node_id"], 0.0))
            hist._rpc(r["node_id"], r, offsets.get(r["node_id"], 0.0), answered=True)
    t = _parquet_rows(parquet_root, "rpc_error", ["node_id", "time", "rpc_http_code", "rpc_response_time"], lo, hi,
                      extra=pc.field("method") == "get_info")
    if t is not None and t.num_rows:
        for r in _rows(t.sort_by([("node_id", "ascending"), ("time", "ascending")])):
            hist._rpc(r["node_id"], r, offsets.get(r["node_id"], 0.0), answered=False)
    hist.rpc = defaultdict(lambda: deque(maxlen=RPC_KEEP),
                           {n: deque(sorted(q)[-RPC_KEEP:], maxlen=RPC_KEEP) for n, q in hist.rpc.items()})

    t = _parquet_rows(parquet_root, "host_sample", ["node_id", "time", "mem_total_kb", "mem_avail_kb", *_CPU],
                      lo, hi)
    if t is not None and t.num_rows:
        for r in _rows(t.sort_by([("node_id", "ascending"), ("time", "ascending")])):
            hist._host(r["node_id"], r)

    t = _parquet_rows(parquet_root, "process_info", ["node_id", "time", "mem_rss_anon", "mem_swap"], lo, hi)
    for r in _rows(t):
        hist._proc(r["node_id"], r)

    if tip is not None:
        near_tip = pc.field("height") >= tip - BLOCK_KEEP
        t = _parquet_rows(parquet_root, "block_timing", ["node_id", "height", "total_ms"], lo, hi, extra=near_tip)
        for r in _rows(t):
            hist._timing(r["node_id"], r, tip)
        t = _parquet_rows(parquet_root, "block_added", ["node_id", "log_time", "height", "hash"], lo, hi,
                          extra=near_tip)
        for r in _rows(t):
            hist._added(r["node_id"], r, offsets.get(r["node_id"], 0.0), tip)

    t = _parquet_rows(parquet_root, "zmq_tx", ["node_id", "recv_time"], lo, hi, tcol="recv_time")
    if t is not None and t.num_rows:
        minute = pc.divide(pc.cast(t["recv_time"], pa.int64()), 60_000_000)
        counts = pa.table({"node_id": t["node_id"], "m": minute}).group_by(["node_id", "m"]).aggregate([("m", "count")])
        for r in counts.to_pylist():
            hist.tx_in[r["node_id"]][r["m"]] = r["m_count"]
    pool = pa.default_memory_pool()
    if hasattr(pool, "release_unused"):   # hand the scan buffers back to the OS
        pool.release_unused()
    return hist


# ---------------------------------------------------------------- dashboard ---

class Dashboard:
    def __init__(self, cfg: DashboardConfig, monitor: Monitor, chain: Chain, history: History,
                 registry: Registry, references: list[str], sidecar_latest: str | None = None):
        self.cfg = cfg
        self.monitor = monitor
        self.chain = chain
        self.history = history
        self.registry = registry
        self.references = list(references)
        self.sidecar_latest = sidecar_version(sidecar_latest)
        self._cache: dict[str, tuple[float, bytes, bytes]] = {}
        self._id_cache: tuple[tuple, dict[str, str]] = ((), {})
        self._static = self._load_static()

    # ------------------------------------------------------------- static ---

    def _load_static(self) -> dict[str, tuple[bytes, bytes, str]]:
        files = {}
        for name, (rel, ctype) in STATIC.items():
            with open(os.path.join(WEB_DIR, rel), "rb") as f:
                files[name] = (f.read(), ctype)
        version = hashlib.sha256(b"".join(files[n][0] for n in sorted(files))).hexdigest()[:10]
        with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
            page = f.read()
        for key, value in (("title", self.cfg.title), ("subtitle", self.cfg.subtitle),
                           ("description", self.cfg.description), ("v", version)):
            page = page.replace("{{" + key + "}}", html.escape(value))
        files["index.html"] = (page.encode(), "text/html; charset=utf-8")
        return {n: (body, gzip.compress(body, 9), ctype) for n, (body, ctype) in files.items()}

    # --------------------------------------------------------------- http ---

    def app(self) -> web.Application:
        app = web.Application(middlewares=[_security_headers])
        app.add_routes([
            web.get("/", self.index),
            web.get("/static/{name}", self.static_file),
            web.get("/api/live.json", self.live),
            web.get("/api/history.json", self.history_json),
        ])
        return app

    @staticmethod
    def _send(request: web.Request, body: bytes, gz: bytes, ctype: str, max_age: int) -> web.Response:
        headers = {"Content-Type": ctype, "Cache-Control": f"public, max-age={max_age}",
                   "Vary": "Accept-Encoding"}
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "gzip"
            body = gz
        return web.Response(body=body, headers=headers)

    async def index(self, request: web.Request) -> web.Response:
        return self._send(request, *self._static["index.html"], max_age=60)

    async def static_file(self, request: web.Request) -> web.Response:
        f = self._static.get(request.match_info["name"])
        if f is None or request.match_info["name"] == "index.html":
            raise web.HTTPNotFound()
        return self._send(request, *f, max_age=86400)

    async def live(self, request: web.Request) -> web.Response:
        _, body, gz = self._cached("live", 2, self.build_live)
        return self._send(request, body, gz, "application/json", max_age=2)

    async def history_json(self, request: web.Request) -> web.Response:
        _, body, gz = self._cached("history", 30, self.build_history)
        return self._send(request, body, gz, "application/json", max_age=30)

    def _cached(self, key: str, ttl: float, build) -> tuple[float, bytes, bytes]:
        now = time.time()
        c = self._cache.get(key)
        if c is None or now - c[0] >= ttl:
            body = json.dumps(build(now), separators=(",", ":"), allow_nan=False).encode()
            c = self._cache[key] = (now, body, gzip.compress(body, 6))
        return c

    # ------------------------------------------------------------- shared ---

    def _ids(self) -> dict[str, str]:
        """node id -> public ID: the configured alias, else public_id(); where
        two hashes collide, both get a longer one."""
        nids = tuple(sorted({n.node_id for n in self.registry.all()} | set(self.monitor.nodes)))
        if self._id_cache[0] != nids:
            ids = {nid: self.cfg.aliases.get(nid) or public_id(self.cfg.id_secret, nid) for nid in nids}
            dup = {pid for pid, k in Counter(ids.values()).items() if k > 1}
            for nid in nids:
                if ids[nid] in dup and nid not in self.cfg.aliases:
                    ids[nid] = public_id(self.cfg.id_secret, nid, 8)
            self._id_cache = (nids, ids)
        return self._id_cache[1]

    def public(self, node_id: str) -> str:
        return self._ids()[node_id]

    def _visible(self) -> list[NodeLive]:
        hide = set(self.cfg.hide)
        revoked = {n.node_id for n in self.registry.all() if n.revoked is not None}
        nodes = [nl for nl in self.monitor.nodes.values()
                 if nl.node.node_id not in hide and nl.node.node_id not in revoked]
        ids = self._ids()
        return sorted(nodes, key=lambda nl: (nl.node.node_id not in self.references, ids[nl.node.node_id]))

    def _blocks(self) -> list[dict]:
        """The canonical reference's recent headers, oldest first. `t` is when a
        reference first saw the block (the miner timestamp if unknown), made
        strictly increasing for the charts."""
        c = self.chain.canonical()
        if c is None or c.top_height is None:
            return []
        out, last_t = [], 0.0
        for h in sorted(x for x in c.headers if x <= c.top_height):
            hdr = c.headers[h]
            bh = hdr.get("hash")
            seen = self.chain.first_seen.get((h, bh)) or self.history.seen.get((h, bh))
            ts = hdr.get("timestamp")
            t = seen if seen is not None else ts
            if t is None:
                continue
            last_t = max(float(t), last_t + 0.001)
            out.append({"h": h, "hash": bh, "ts": ts, "seen": seen, "t": last_t,
                        "size": hdr.get("block_size"), "weight": hdr.get("block_weight"),
                        "txs": hdr.get("num_txes"), "difficulty": hdr.get("difficulty"),
                        "mv": hdr.get("major_version")})
        return out

    def _delay(self, node_id: str, b: dict) -> float | None:
        acc = self.history.accepted.get(node_id, {}).get(b["h"])
        if acc is None or b["seen"] is None or (acc[0] is not None and acc[0] != b["hash"]):
            return None
        return acc[1] - b["seen"]

    def _ref_node(self, ref_id: str | None) -> NodeLive | None:
        """The canonical reference's own sidecar data (pool, weight median)."""
        for rid in [ref_id, *self.references]:
            nl = self.monitor.nodes.get(rid) if rid else None
            if nl is not None and nl.info:
                return nl
        return None

    # --------------------------------------------------------------- live ---

    def build_live(self, now: float) -> dict:
        blocks = self._blocks()
        canon = self.chain.canonical()
        ref_id = canon.cfg.id if canon else None
        self.history.prune(now, canon.top_height if canon else None)
        tip = blocks[-1] if blocks else {}
        ref = self._ref_node(ref_id)
        info = (ref.info if ref else None) or {}
        pool = (ref.pool if ref else None) or {}
        last30 = blocks[-31:]
        hour = [b for b in blocks if b["t"] >= now - 3600]
        interval = None
        if len(last30) > 1 and last30[0]["ts"] and last30[-1]["ts"]:
            interval = (last30[-1]["ts"] - last30[0]["ts"]) / (len(last30) - 1)
        tx_in = None
        src = self.history.tx_in.get(ref_id or "", {})
        m_now = int(now // 60)
        if any(m >= m_now - 15 for m in src):
            tx_in = sum(src.get(m, 0) for m in range(m_now - 10, m_now)) / 10
        difficulty = tip.get("difficulty")
        network = {
            "height": tip.get("h"),
            "hash": tip.get("hash"),
            "major_version": tip.get("mv"),
            "tip_timestamp": _num(tip.get("ts")),
            "tip_seen": _num(tip.get("seen"), 3),
            "weight": _num(tip.get("weight")),
            "size": _num(tip.get("size")),
            "txs": _num(tip.get("txs")),
            "weight_median": _num(info.get("block_weight_median")),
            "weight_limit": _num(info.get("block_weight_limit")),
            "pool_txs": _num(info.get("tx_pool_size")),
            "pool_bytes": _num(pool.get("bytes_total")),
            "txs_1h": sum(b["txs"] or 0 for b in hour),
            "blocks_1h": len(hour),
            "tps_1h": _num(sum(b["txs"] or 0 for b in hour) / 3600, 4) if hour else None,
            "interval_30": _num(interval, 1),
            "weight_30": _num(statistics.fmean(b["weight"] or 0 for b in blocks[-30:]), 0) if blocks else None,
            "difficulty": _num(difficulty),
            "hashrate": _num(difficulty / TARGET_S, 1) if difficulty else None,
            "tx_in_per_min": _num(tx_in, 1),
        }
        recent = [{"h": b["h"], "hash": b["hash"], "ts": _num(b["ts"]), "seen": _num(b["seen"], 3),
                   "size": _num(b["size"]), "weight": _num(b["weight"]), "txs": _num(b["txs"])}
                  for b in reversed(blocks[-24:])]
        last30_blocks = [b for b in blocks if tip and b["h"] > tip["h"] - 30]
        return {
            "v": 1,
            "now": round(now, 3),
            "links": [{"label": str(x.get("label") or x["url"]), "url": str(x["url"])} for x in self.cfg.links],
            "sidecar_latest": self.sidecar_latest,
            "network": network,
            "blocks": recent,
            "nodes": [self._node(nl, now, last30_blocks) for nl in self._visible()],
        }

    def _node(self, nl: NodeLive, now: float, last30: list[dict]) -> dict:
        nid = nl.node.node_id
        info = nl.info or {}
        bt = nl.block_timing or {}
        host, proc, pool = nl.host, nl.proc, nl.pool or {}
        busiest = max(nl.disk.values(), key=lambda d: d["util_ratio"], default=None)
        start = info.get("start_time")
        lag = nl.lag_hist[-1][1] if nl.lag_hist else None
        ms_hist = self.history.block_ms.get(nid, {})
        prof = {**self.history.profiles.get(nid, {}), **nl.profile}
        sidecar = sidecar_version(prof.get("sidecar_version"))
        return {
            "id": self.public(nid),
            "ref": nid in self.references,
            "hw": hardware(prof),
            "sidecar": sidecar,
            "sidecar_update": version_newer(self.sidecar_latest, sidecar),
            "rpc": self.history.rpc_stats(nid, now),
            "version": clean(info.get("version"), 40),
            "state": nl.state,
            "reason": clean(nl.reason, 120),
            "since": round(nl.state_since, 1),
            "height": nl.height,
            "lag": lag,
            "synced": info.get("synchronized"),
            "sync": self._sync(nl, now),
            "peers_out": _num(info.get("outgoing_connections_count")),
            "peers_in": _num(info.get("incoming_connections_count")),
            "pool_txs": _num(info.get("tx_pool_size")),
            "pool_bytes": _num(pool.get("bytes_total")),
            "block_h": bt.get("height"),
            "block_ms": _num(bt.get("total_ms"), 1),
            "delay_s": _num(nl.block_delay, 3),
            "ms_med": _num(_median([ms_hist.get(b["h"]) for b in last30]), 1),
            "delay_med": _num(_median([self._delay(nid, b) for b in last30]), 3),
            "cpu": _num(host.get("cpu_busy_ratio"), 3),
            "iowait": _num(host.get("cpu_iowait_ratio"), 3),
            "steal": _num(host.get("cpu_steal_ratio"), 3),
            "mcpu": _num(proc.get("cpu_cores"), 2),
            "rss": _num(proc.get("rss_bytes")),
            "mon_mem": _num(monerod_private(proc.get("anon_bytes"), proc.get("swap_bytes"))),
            # a share, not bytes: the exact total identifies a machine (hw.ram_gb is the rounded size)
            "mem_used": _mem_used(host),
            "swap_used": _num(host.get("swap_used_bytes")),
            "disk_util": _num(busiest["util_ratio"], 3) if busiest else None,
            "disk_await_ms": _num(busiest["await_ms"], 2) if busiest else None,
            "db_bytes": _num(info.get("database_size")),
            "uptime_s": _num(now - start, 0) if start else None,
            "age_s": _num(now - nl.last_batch, 1) if nl.last_batch else None,
        }

    def _sync(self, nl: NodeLive, now: float) -> dict | None:
        if nl.state != "SYNCING" or nl.height is None:
            return None
        tip, _ = self.chain.tip()
        target = int((nl.info or {}).get("target_height") or 0) - 1   # chain length -> top block
        target = max(target, tip or 0, nl.height)
        rate = self.history.sync_rate(nl.node.node_id, now)
        eta = (target - nl.height) / rate * 3600 if rate and rate > 0 else None
        return {"target": target, "pct": _num(nl.height / target, 5) if target else None,
                "rate_h": _num(rate, 0), "eta_s": _num(eta, 0)}

    # ------------------------------------------------------------ history ---

    def build_history(self, now: float) -> dict:
        hist = self.history
        canon = self.chain.canonical()
        ref_id = canon.cfg.id if canon else None
        hist.prune(now, canon.top_height if canon else None)
        all_blocks = self._blocks()
        blocks = [b for b in all_blocks if b["t"] >= now - hist.window_s]
        order = [nl.node.node_id for nl in self._visible()]
        ids = self._ids()

        per_block = {}
        for nid in order:
            ms = hist.block_ms.get(nid, {})
            per_block[ids[nid]] = {"ms": [_num(ms.get(b["h"]), 1) for b in blocks],
                                   "delay": [_num(self._delay(nid, b), 3) for b in blocks]}

        # per minute; lag against the canonical height when the node sampled
        # its height (as the node states do), so a block arriving later in the
        # minute doesn't count against it
        m_end = int(now // 60)
        mins = range(m_end - int(hist.window_s // 60) + 1, m_end + 1)
        bt = [b["t"] for b in all_blocks]

        def canon_at(t: float) -> int | None:
            i = bisect.bisect_right(bt, t) - 1
            return all_blocks[i]["h"] if i >= 0 else None

        series = {}
        for nid in order:
            d = hist.minutes.get(nid, {})
            s = {"lag": [], "pool": [], "cpu": [], "mem": [], "mon": [], "rpc": []}
            for m in mins:
                b = d.get(m) or _EMPTY
                ch = canon_at(b[HT]) if b[HT] is not None else None
                s["lag"].append(max(0, ch - b[H]) if ch is not None else None)
                s["pool"].append(b[POOL])
                s["cpu"].append(_num(b[CPU_SUM] / b[CPU_N], 3) if b[CPU_N] else None)
                s["mem"].append(_num(b[MEM], 3))
                s["mon"].append(_num(b[MON] / 1e9, 3) if b[MON] is not None else None)   # GB
                s["rpc"].append(_num(b[RPC] * 1000, 2) if b[RPC] is not None else None)   # ms
            series[ids[nid]] = s

        src = hist.tx_in.get(ref_id or "") or max(hist.tx_in.values(), key=len, default={})
        first = min(src) if src else None
        tx_in = [None if first is None or m < first or m == m_end else src.get(m, 0) for m in mins]

        return {
            "v": 1,
            "now": round(now, 3),
            "window_s": hist.window_s,
            "rpc_limit_ms": _num(RPC_TIMEOUT_S * 1000),
            "order": [ids[nid] for nid in order],
            "blocks": {
                "t": [round(b["t"], 3) for b in blocks],
                "h": [b["h"] for b in blocks],
                "ts": [_num(b["ts"]) for b in blocks],
                "weight": [_num(b["weight"]) for b in blocks],
                "size": [_num(b["size"]) for b in blocks],
                "txs": [_num(b["txs"]) for b in blocks],
            },
            "per_block": per_block,
            "minutes": {"t": [m * 60 for m in mins], "tx_in": tx_in, "nodes": series},
        }


@web.middleware
async def _security_headers(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        e.headers.update(SECURITY_HEADERS)
        raise
    resp.headers.update(SECURITY_HEADERS)
    return resp
