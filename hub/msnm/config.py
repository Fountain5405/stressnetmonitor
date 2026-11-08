"""Hub configuration (YAML). See deploy/hub.example.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field, fields

import yaml

from .chain import RefConfig
from .dashboard import DashboardConfig
from .live import Thresholds
from .registry import MOD_RE


@dataclass
class ZmqNode:
    id: str
    zmq: str


@dataclass
class Config:
    data_dir: str = "/var/lib/msnm"
    public_url: str = ""          # what sidecars push to; used in `msnm node add` messages
    ingest_host: str = "127.0.0.1"
    ingest_port: int = 8790
    metrics_host: str = "127.0.0.1"
    metrics_port: int = 8791
    trusted_proxies: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])
    fork_height: int = 0
    min_major_version: int = 0
    flush_interval_s: float = 300
    max_batch_bytes: int = 8 * 1024 * 1024          # compressed body
    max_batch_text_bytes: int = 64 * 1024 * 1024    # after decompression
    max_bundle_bytes: int = 64 * 1024 * 1024
    max_batches_per_min: int = 20                   # per node
    ref_poll_interval_s: float = 2.0
    ref_backfill_blocks: int = 720
    references: list[RefConfig] = field(default_factory=list)
    zmq_nodes: list[ZmqNode] = field(default_factory=list)
    thresholds: Thresholds = field(default_factory=Thresholds)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    mods: dict[str, str] = field(default_factory=dict)    # mod label -> public description


def _check_mods(mods) -> dict[str, str]:
    if not isinstance(mods, dict):
        raise ValueError("mods: must map label -> description")
    for label, desc in mods.items():
        if not MOD_RE.match(str(label)):
            raise ValueError(f"mods: bad label {label!r}")
        if (not isinstance(desc, str) or not 1 <= len(desc) <= 80 or not desc.isprintable()
                or "<" in desc or ">" in desc or "://" in desc):
            raise ValueError(f"mods[{label!r}]: 1-80 printable characters, no '<', '>' or '://'")
    return {str(k): v for k, v in mods.items()}


def load(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    cfg = Config(**{k: v for k, v in raw.items()
                    if k not in ("references", "zmq_nodes", "thresholds", "dashboard", "mods")})
    cfg.references = [RefConfig(**r) for r in raw.get("references") or []]
    cfg.zmq_nodes = [ZmqNode(**z) for z in raw.get("zmq_nodes") or []]
    cfg.thresholds = Thresholds(**(raw.get("thresholds") or {}))
    cfg.dashboard = DashboardConfig(**(raw.get("dashboard") or {}))
    cfg.mods = _check_mods(raw.get("mods") or {})
    if not cfg.references:
        # Data collection works without references; node states stay UNKNOWN
        # until one is configured.
        import logging
        logging.getLogger(__name__).warning("no reference nodes configured: node states will be UNKNOWN")
    return cfg
