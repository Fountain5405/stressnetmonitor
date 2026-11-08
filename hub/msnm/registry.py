"""Node registry: node pseudonyms, trust tiers and push tokens (SQLite).

Tokens are shown once when issued; only their SHA-256 is stored.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass

TIERS = ("operator", "trusted", "volunteer")
POSITIONS = ("lan", "remote")
NODE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
MOD_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
MAX_MODS = 8


@dataclass(frozen=True)
class Node:
    node_id: str
    tier: str
    position: str
    note: str
    created: float
    revoked: float | None


@dataclass(frozen=True)
class BuildChange:
    node_id: str
    since: float
    mods: tuple[str, ...]


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Registry:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY, tier TEXT NOT NULL, position TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '', token_sha256 TEXT UNIQUE NOT NULL,
            created REAL NOT NULL, revoked REAL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS builds (
            node_id TEXT NOT NULL, since REAL NOT NULL, mods TEXT NOT NULL,
            PRIMARY KEY (node_id, since))""")
        self.db.commit()
        self._cache: dict[str, Node] = {}
        self._cache_time = 0.0

    def add(self, node_id: str, tier: str, position: str, note: str = "") -> str:
        if not NODE_ID_RE.match(node_id):
            raise ValueError("node id must be 2-41 chars of a-z 0-9 _ - (use a pseudonym)")
        if tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}")
        if position not in POSITIONS:
            raise ValueError(f"position must be one of {POSITIONS}")
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,NULL)",
                            (node_id, tier, position, note, _hash(token), time.time()))
        self._cache_time = 0
        return token

    def rotate(self, node_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.db:
            cur = self.db.execute("UPDATE nodes SET token_sha256=?, revoked=NULL WHERE node_id=?",
                                  (_hash(token), node_id))
        if cur.rowcount != 1:
            raise KeyError(node_id)
        self._cache_time = 0
        return token

    def revoke(self, node_id: str) -> None:
        with self.db:
            cur = self.db.execute("UPDATE nodes SET revoked=? WHERE node_id=?", (time.time(), node_id))
        if cur.rowcount != 1:
            raise KeyError(node_id)
        self._cache_time = 0

    def set(self, node_id: str, *, tier: str | None = None, position: str | None = None,
            note: str | None = None) -> None:
        if tier is not None and tier not in TIERS:
            raise ValueError(f"tier must be one of {TIERS}")
        if position is not None and position not in POSITIONS:
            raise ValueError(f"position must be one of {POSITIONS}")
        with self.db:
            for col, val in (("tier", tier), ("position", position), ("note", note)):
                if val is not None:
                    self.db.execute(f"UPDATE nodes SET {col}=? WHERE node_id=?", (val, node_id))
        self._cache_time = 0

    def set_build(self, node_id: str, mods: list[str], since: float | None = None) -> None:
        """Record that the node runs these mods (none = stock) from `since` on."""
        labels = sorted(set(mods))
        bad = [m for m in labels if not MOD_RE.match(m)]
        if bad:
            raise ValueError(f"bad mod label {bad[0]!r}: 1-24 chars of a-z 0-9 -, not starting with -")
        if len(labels) > MAX_MODS:
            raise ValueError(f"at most {MAX_MODS} mods per entry")
        now = time.time()
        if since is None:
            since = now
        if since > now + 60:
            raise ValueError("since is in the future")
        if self.get(node_id) is None:
            raise KeyError(node_id)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO builds VALUES (?,?,?)", (node_id, since, ",".join(labels)))
        self._cache_time = 0

    def remove_build(self, node_id: str, since: float) -> None:
        with self.db:
            cur = self.db.execute("DELETE FROM builds WHERE node_id=? AND since=?", (node_id, since))
        if cur.rowcount != 1:
            raise KeyError(f"{node_id} since {since}")
        self._cache_time = 0

    def builds(self, node_id: str | None = None) -> list[BuildChange]:
        q = "SELECT node_id, since, mods FROM builds"
        rows = self.db.execute(q + (" WHERE node_id=?" if node_id else "") + " ORDER BY node_id, since",
                               (node_id,) if node_id else ())
        return [BuildChange(n, s, tuple(m.split(",")) if m else ()) for n, s, m in rows]

    def build_at(self, node_id: str, t: float) -> tuple[str, ...]:
        """The mods in effect at time t; () for stock or no record."""
        r = self.db.execute("SELECT mods FROM builds WHERE node_id=? AND since<=? ORDER BY since DESC LIMIT 1",
                            (node_id, t)).fetchone()
        return tuple(r[0].split(",")) if r and r[0] else ()

    def all(self) -> list[Node]:
        rows = self.db.execute(
            "SELECT node_id, tier, position, note, created, revoked FROM nodes ORDER BY node_id")
        return [Node(*r) for r in rows]

    def get(self, node_id: str) -> Node | None:
        r = self.db.execute("SELECT node_id, tier, position, note, created, revoked FROM nodes "
                            "WHERE node_id=?", (node_id,)).fetchone()
        return Node(*r) if r else None

    def by_token(self, token: str) -> Node | None:
        """Active node for a token. Cached briefly so revocations apply within 10 s."""
        now = time.time()
        if now - self._cache_time > 10:
            rows = self.db.execute("SELECT token_sha256, node_id, tier, position, note, created, revoked "
                                   "FROM nodes WHERE revoked IS NULL")
            self._cache = {r[0]: Node(*r[1:]) for r in rows}
            self._cache_time = now
        return self._cache.get(_hash(token))
