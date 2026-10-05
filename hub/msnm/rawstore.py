"""Append-only archive of everything sidecars push, with a hash-chained ledger.

    <data>/raw/<node_id>/<YYYY-MM-DD>/<seq>.gz|.txt   batch bodies, as received
    <data>/bundles/<node_id>/<name>                   crash/stall bundles
    <data>/raw/ledger.ndjson                          one line per accepted upload

Each ledger line includes `prev`, the SHA-256 of the previous line, so edits
or deletions in the ledger are detectable (`msnm verify-ledger`). Because
the ledger records node, token tier and source IP for every upload, all data
from a participant can be found and excluded later.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
from collections.abc import Iterator

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class RawStore:
    def __init__(self, data_dir: str):
        self.raw = os.path.join(data_dir, "raw")
        self.bundles = os.path.join(data_dir, "bundles")
        os.makedirs(self.raw, exist_ok=True)
        os.makedirs(self.bundles, exist_ok=True)
        self.ledger_path = os.path.join(self.raw, "ledger.ndjson")
        self._lock = threading.Lock()
        self._prev = self._last_hash()

    def _last_hash(self) -> str:
        if not os.path.exists(self.ledger_path):
            return "0" * 64
        last = b""
        with open(self.ledger_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            for line in f.read().splitlines():
                if line.strip():
                    last = line
        return hashlib.sha256(last).hexdigest() if last else "0" * 64

    def _append_ledger(self, entry: dict) -> None:
        entry["prev"] = self._prev
        line = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        with open(self.ledger_path, "ab") as f:
            f.write(line + b"\n")
            f.flush()
            os.fsync(f.fileno())
        self._prev = hashlib.sha256(line).hexdigest()

    def save_batch(self, node_id: str, seq: int, body: bytes, gzipped: bool, meta: dict) -> tuple[bool, str]:
        """Store a batch. Returns (is_new, sha256). A repeated seq with the same
        content is a duplicate (the sidecar retried after a lost response)."""
        sha = hashlib.sha256(body).hexdigest()
        day = dt.datetime.fromtimestamp(meta["recv_time"], dt.timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            idx = os.path.join(self.raw, node_id, "seq-index")
            os.makedirs(idx, exist_ok=True)
            marker = os.path.join(idx, f"{seq:012d}")
            if os.path.exists(marker):
                with open(marker) as f:
                    old = f.read().split()
                if old and old[0] == sha:
                    return False, sha
                # Same seq, different content (e.g. sidecar spool was reset):
                # keep both, under a distinct name.
                seq_name = f"{seq:012d}-{sha[:12]}"
            else:
                seq_name = f"{seq:012d}"
            d = os.path.join(self.raw, node_id, day)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, seq_name + (".gz" if gzipped else ".txt"))
            with open(path + ".tmp", "wb") as f:
                f.write(body)
            os.replace(path + ".tmp", path)
            with open(marker, "w") as f:
                f.write(f"{sha} {os.path.relpath(path, self.raw)}\n")
            self._append_ledger({"type": "batch", "node_id": node_id, "seq": seq, "sha256": sha,
                                 "bytes": len(body), "path": os.path.relpath(path, self.raw), **meta})
        return True, sha

    def save_bundle(self, node_id: str, name: str, body: bytes, meta: dict) -> str:
        if not _SAFE_NAME.match(name):
            raise ValueError("bad bundle name")
        sha = hashlib.sha256(body).hexdigest()
        with self._lock:
            d = os.path.join(self.bundles, node_id)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, name)
            if os.path.exists(path):
                path = os.path.join(d, f"{sha[:12]}-{name}")
            with open(path + ".tmp", "wb") as f:
                f.write(body)
            os.replace(path + ".tmp", path)
            self._append_ledger({"type": "bundle", "node_id": node_id, "name": name, "sha256": sha,
                                 "bytes": len(body), "path": os.path.relpath(path, self.raw), **meta})
        return path

    def iter_ledger(self) -> Iterator[dict]:
        if not os.path.exists(self.ledger_path):
            return
        with open(self.ledger_path, "rb") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def verify_ledger(data_dir: str) -> list[str]:
    """Check the hash chain and every archived file. Returns a list of problems."""
    raw = os.path.join(data_dir, "raw")
    problems = []
    prev = "0" * 64
    path = os.path.join(raw, "ledger.ndjson")
    if not os.path.exists(path):
        return ["no ledger"]
    with open(path, "rb") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip(b"\n")
            if not line:
                continue
            e = json.loads(line)
            if e.get("prev") != prev:
                problems.append(f"line {n}: hash chain broken")
            prev = hashlib.sha256(line).hexdigest()
            fp = os.path.normpath(os.path.join(raw, e["path"]))
            try:
                with open(fp, "rb") as g:
                    if hashlib.sha256(g.read()).hexdigest() != e["sha256"]:
                        problems.append(f"line {n}: {e['path']} content changed")
            except FileNotFoundError:
                problems.append(f"line {n}: {e['path']} missing")
    return problems
