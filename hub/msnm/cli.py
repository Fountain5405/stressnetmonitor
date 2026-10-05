"""msnm command line.

  msnm serve -c hub.yaml
  msnm node add NODE_ID --tier operator|trusted|volunteer --position lan|remote [--note ...]
  msnm node list | revoke NODE_ID | rotate-token NODE_ID | set NODE_ID [--tier ..] [--position ..]
  msnm reparse --out DIR [--node NODE_ID]   rebuild sidecar tables from the raw archive
  msnm verify-ledger                        check the raw archive's hash chain
  msnm compact [--before YYYY-MM-DD]        merge each day's Parquet part files
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import gzip
import logging
import os
import sys
from collections import defaultdict

from . import config as config_mod
from .rawstore import RawStore, verify_ledger
from .records import ParserState, parse_batch
from .registry import POSITIONS, TIERS, Registry
from .tables import ParquetSink, compact_day


def _registry(cfg) -> Registry:
    os.makedirs(cfg.data_dir, exist_ok=True)
    return Registry(os.path.join(cfg.data_dir, "registry.sqlite"))


def cmd_serve(cfg, args) -> int:
    from .server import Hub
    asyncio.run(Hub(cfg).run())
    return 0


def cmd_node(cfg, args) -> int:
    reg = _registry(cfg)
    if args.node_cmd == "add":
        token = reg.add(args.node_id, args.tier, args.position, args.note or "")
        print(f"node {args.node_id} added ({args.tier}, {args.position}).")
        print("Token (shown once; give it only to this node's operator):\n")
        print(f"  {token}\n")
        print("Sidecar config (/etc/msnm-sidecar.conf):\n")
        print(f'  HUB_URL="{args.hub_url or "https://<your-hub>"}"')
        print(f'  TOKEN="{token}"')
    elif args.node_cmd == "rotate-token":
        token = reg.rotate(args.node_id)
        print(f"new token for {args.node_id} (the old one no longer works):\n\n  {token}")
    elif args.node_cmd == "revoke":
        reg.revoke(args.node_id)
        print(f"revoked {args.node_id}; its pushes are refused within 10 s")
    elif args.node_cmd == "set":
        reg.set(args.node_id, tier=args.tier, position=args.position, note=args.note)
    elif args.node_cmd == "list":
        for n in reg.all():
            created = dt.datetime.fromtimestamp(n.created, dt.timezone.utc).strftime("%Y-%m-%d")
            status = "REVOKED" if n.revoked else "active"
            print(f"{n.node_id:24s} {n.tier:10s} {n.position:7s} {created} {status:8s} {n.note}")
    return 0


def cmd_reparse(cfg, args) -> int:
    store = RawStore(cfg.data_dir)
    sink = ParquetSink(args.out)
    states: dict[str, ParserState] = defaultdict(ParserState)
    entries = [e for e in store.iter_ledger() if e.get("type") == "batch"
               and (not args.node or e["node_id"] == args.node)]
    entries.sort(key=lambda e: (e["node_id"], e["seq"], e["recv_time"]))
    n = 0
    for e in entries:
        path = os.path.join(store.raw, e["path"])
        with open(path, "rb") as f:
            body = f.read()
        text = (gzip.decompress(body) if path.endswith(".gz") else body).decode("utf-8", errors="replace")
        hdr, parsed = parse_batch(text, e["node_id"], states[e["node_id"]])
        sink.extend(parsed.rows)
        sent = e.get("sent")
        sink.add("batch", {"node_id": e["node_id"], "time": e["recv_time"], "seq": hdr.seq,
                           "created": hdr.created, "sent": sent, "recv_time": e["recv_time"],
                           "bytes": e["bytes"], "records": parsed.records, "sidecar": hdr.sidecar,
                           "boot_id": hdr.boot_id,
                           "clock_offset_s": (sent - e["recv_time"]) if sent else None,
                           "sha256": e["sha256"]})
        n += 1
        if sink.pending() > 500_000:
            sink.flush()
    sink.flush()
    print(f"reparsed {n} batches into {args.out}")
    return 0


def cmd_verify(cfg, args) -> int:
    problems = verify_ledger(cfg.data_dir)
    for p in problems:
        print(p)
    print("ledger OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_compact(cfg, args) -> int:
    root = os.path.join(cfg.data_dir, "parquet")
    before = args.before or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    for table in sorted(os.listdir(root)):
        tdir = os.path.join(root, table)
        for part in sorted(os.listdir(tdir)):
            if part.startswith("date=") and part[5:] < before:
                merged = compact_day(root, table, part[5:])
                if merged > 1:
                    print(f"{table} {part[5:]}: merged {merged} files")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="msnm", description="StressNet monitor hub")
    ap.add_argument("-c", "--config", default=os.environ.get("MSNM_HUB_CONFIG", "/etc/msnm/hub.yaml"))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    pn = sub.add_parser("node")
    nsub = pn.add_subparsers(dest="node_cmd", required=True)
    a = nsub.add_parser("add")
    a.add_argument("node_id")
    a.add_argument("--tier", required=True, choices=TIERS)
    a.add_argument("--position", required=True, choices=POSITIONS)
    a.add_argument("--note")
    a.add_argument("--hub-url")
    nsub.add_parser("list")
    for name in ("revoke", "rotate-token"):
        nsub.add_parser(name).add_argument("node_id")
    s = nsub.add_parser("set")
    s.add_argument("node_id")
    s.add_argument("--tier", choices=TIERS)
    s.add_argument("--position", choices=POSITIONS)
    s.add_argument("--note")
    rp = sub.add_parser("reparse")
    rp.add_argument("--out", required=True)
    rp.add_argument("--node")
    sub.add_parser("verify-ledger")
    cp = sub.add_parser("compact")
    cp.add_argument("--before", help="compact days before this date (default: today)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config_mod.load(args.config)
    handler = {"serve": cmd_serve, "node": cmd_node, "reparse": cmd_reparse,
               "verify-ledger": cmd_verify, "compact": cmd_compact}[args.cmd]
    try:
        return handler(cfg, args)
    except (KeyError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
