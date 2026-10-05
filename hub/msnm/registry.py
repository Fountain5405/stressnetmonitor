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


@dataclass(frozen=True)
class Node:
    node_id: str
    tier: str
    position: str
    note: str
    created: float
    revoked: float | None


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
