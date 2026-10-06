"""msnm command line.

  msnm serve -c hub.yaml
  msnm node add NODE_ID [NODE_ID ...] --tier operator|trusted|volunteer --position lan|remote [--note ...]
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


REPO = "https://github.com/Fountain5405/stressnetmonitor"

# Plain text, no markdown: it gets pasted into chat clients as is.
WELCOME_ONE = """\
Thanks for running a stressnet sidecar!

Node name: {node_id}
Hub URL: {hub_url}
Token: {token}

Please keep the token private. It identifies your node to the hub.
"""

WELCOME_MANY = """\
Thanks for running stressnet sidecars!

Hub URL: {hub_url}

You have {n} nodes, each with its own token. Use a different one on each
machine, and keep the list so you know which machine is which:

{tokens}

Please keep the tokens private. Each one identifies one node to the hub.
"""

WELCOME_STEPS = """
Step 1. {step1} these options to monerod and restart it:

--show-time-stats 1 --log-level 0,blockchain:INFO,txpool:INFO

On v0.19.0.0-beta.3.0, also add these seed nodes (its built-in ones are dead):

--seed-node 185.141.216.177:28180 --seed-node 208.123.187.228:28080 --seed-node 185.141.216.147:28080 --seed-node 209.141.41.69:28080

Step 2. On {step2}, run:

git clone {repo}.git
cd stressnetmonitor
sudo ./sidecar/install.sh --hub {hub_url}

The installer asks for the token{which}, checks everything,
and starts the sidecar as a systemd service. Without sudo or systemd,
run this instead, as the user that runs monerod:

./sidecar/install.sh --hub {hub_url} --no-systemd

What it sends and how to remove it: {repo}/blob/main/docs/sidecar.md

The sidecar reports the hardware itself (CPU, RAM, disk type). Do tell me
about anything it can't see, such as other heavy work on the same machine.
"""


def welcome_text(hub_url: str, nodes: list[tuple[str, str]]) -> str:
    """The operator's copy-paste message for one or more (node_id, token) pairs."""
    if len(nodes) == 1:
        head = WELCOME_ONE.format(node_id=nodes[0][0], hub_url=hub_url, token=nodes[0][1])
        which = ""
        step1, step2 = "Add", "the same machine"
    else:
        head = WELCOME_MANY.format(hub_url=hub_url, n=len(nodes),
                                   tokens="\n".join(f"{i}: {t}" for i, t in nodes))
        which = " (that machine's own)"
        step1, step2 = "On each machine, add", "each machine"
    return head + WELCOME_STEPS.format(hub_url=hub_url, repo=REPO, which=which,
                                       step1=step1, step2=step2)

NEW_TOKEN = """\
Here is a new token for your stressnet sidecar ({node_id}). The old one no longer works.

{token}

To switch, run the installer again from your stressnetmonitor checkout:

git pull
sudo ./sidecar/install.sh --hub {hub_url}

Or put TOKEN="{token}" in /etc/msnm-sidecar.conf
(~/.config/msnm-sidecar.conf with --no-systemd) and restart the sidecar.
"""


def _write_message(cfg, node_id: str, text: str) -> str:
    """Write a copy-paste message for the node's operator. It holds the token,
    so the file is readable by the hub's user only."""
    d = os.path.join(cfg.data_dir, "welcome")
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, f"{node_id}.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def cmd_serve(cfg, args) -> int:
    from .server import Hub
    asyncio.run(Hub(cfg).run())
    return 0


def cmd_node(cfg, args) -> int:
    reg = _registry(cfg)
    hub_url = getattr(args, "hub_url", None) or cfg.public_url or "https://<your-hub>"
    if args.node_cmd == "add":
        # Several ids = one operator running several sidecars: one token each,
        # one message. Check them all first so a bad id doesn't leave a half-done batch.
        ids = args.node_id
        taken = {n.node_id for n in reg.all()}
        bad = sorted({i for i in ids if i in taken} | {i for i in ids if ids.count(i) > 1})
        if bad:
            raise ValueError(f"node id already exists or repeated: {', '.join(bad)}")
        nodes = [(i, reg.add(i, args.tier, args.position, args.note or "")) for i in ids]
        path = _write_message(cfg, "+".join(ids), welcome_text(hub_url, nodes))
        for i, token in nodes:
            print(f"node {i} added ({args.tier}, {args.position}). Token (shown once): {token}")
        print(f"Message for the operator, ready to paste: {path}")
        print("The running hub accepts new tokens within 10 s.")
    elif args.node_cmd == "rotate-token":
        token = reg.rotate(args.node_id)
        path = _write_message(cfg, args.node_id, NEW_TOKEN.format(
            node_id=args.node_id, hub_url=hub_url, token=token))
        print(f"new token for {args.node_id} (the old one no longer works): {token}")
        print(f"Message for the operator, ready to paste: {path}")
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
    a.add_argument("node_id", nargs="+", help="several ids: one operator, one token each, one message")
    a.add_argument("--tier", required=True, choices=TIERS)
    a.add_argument("--position", required=True, choices=POSITIONS)
    a.add_argument("--note")
    a.add_argument("--hub-url", help="default: public_url from the config")
    nsub.add_parser("list")
    nsub.add_parser("revoke").add_argument("node_id")
    r = nsub.add_parser("rotate-token")
    r.add_argument("node_id")
    r.add_argument("--hub-url", help="default: public_url from the config")
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
