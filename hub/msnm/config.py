"""Hub configuration (YAML). See deploy/hub.example.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field, fields

import yaml

from .chain import RefConfig
from .live import Thresholds


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


def load(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    known = {f.name for f in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    cfg = Config(**{k: v for k, v in raw.items()
                    if k not in ("references", "zmq_nodes", "thresholds")})
    cfg.references = [RefConfig(**r) for r in raw.get("references") or []]
    cfg.zmq_nodes = [ZmqNode(**z) for z in raw.get("zmq_nodes") or []]
    cfg.thresholds = Thresholds(**(raw.get("thresholds") or {}))
    if not cfg.references:
        # Data collection works without references; node states stay UNKNOWN
        # until one is configured.
        import logging
        logging.getLogger(__name__).warning("no reference nodes configured: node states will be UNKNOWN")
    return cfg
