# Running the hub

The hub ([`hub/msnm`](../hub/msnm)) receives sidecar uploads, polls the reference nodes, subscribes to operator nodes' ZMQ, tracks node states, writes Parquet, and serves Prometheus metrics.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e 'hub[test]'
.venv/bin/python -m pytest hub/tests        # optional
cp deploy/hub.example.yaml hub.yaml          # edit: data_dir, references, zmq_nodes
```

**Reference nodes** should run with unrestricted RPC reachable from the hub (on the LAN), and preferably `--zmq-pub tcp://0.0.0.0:28083` for precise block first-seen times. Pruned references (`--prune-blockchain`) are fine.

## Run

```bash
.venv/bin/msnm -c hub.yaml serve
```

Or install `deploy/msnm-hub.service`. The hub listens on:

| Port (default) | Paths | Expose to |
|---|---|---|
| `127.0.0.1:8790` | `/v1/ping`, `/v1/ingest`, `/v1/bundle` | the internet, **only** via HAProxy + TLS (`deploy/haproxy.cfg.example`) |
| `127.0.0.1:8791` | `/metrics`, `/healthz` | Prometheus (`deploy/prometheus.yml`) |

## Adding nodes

Give each node a **pseudonym**. It appears on public dashboards.

```bash
.venv/bin/msnm -c hub.yaml node add vol-07 --tier volunteer --position remote \
    --note "matrix: @someone, ThinkPad X230"
```

This prints the token (shown once). It also writes a plain-text message for the volunteer, ready to paste into chat, to `data_dir/welcome/vol-07.txt`, readable only by you. The message has the hub URL (`public_url` in the config, or `--hub-url`), the token, the `monerod` flags and the install steps. The `--note` stays private in the registry.

One person running several sidecars needs one token per machine, but only one message. Name them all in one command:

```bash
.venv/bin/msnm -c hub.yaml node add vol-08a vol-08b vol-08c --tier volunteer --position remote
```

That writes `welcome/vol-08a+vol-08b+vol-08c.txt`, with each node's token on its own line. `node rotate-token ID` writes a short new-token message to `welcome/ID.txt`.

- `--tier`: `operator` (your machines), `trusted` (known people), or `volunteer`. Every row of data can be filtered by tier. Base headline results on `operator` and `trusted`.
- `--position`: `lan` (same LAN as the tx generator) or `remote`.

Other commands: `node list`, `node revoke ID` (takes effect within 10 s), `node rotate-token ID`, `node set ID --tier ...`.

## Data on disk

```
data_dir/
  registry.sqlite              node pseudonyms, tiers, token hashes
  raw/ledger.ndjson            hash-chained log of every upload (node, seq, sha256, time, source IP)
  raw/<node>/<date>/<seq>.gz   every batch exactly as received
  bundles/<node>/...           crash and stall bundles
  parquet/<table>/date=<YYYY-MM-DD>/part-*.parquet
```

- `msnm verify-ledger` checks the hash chain and every archived file.
- `msnm reparse --out DIR [--node ID]` rebuilds every sidecar-derived table from the raw archive. Use it after fixing a parser, or to rebuild the dataset without an excluded participant (revoke them, then reparse with their entries filtered out).
- `msnm compact` merges each past day's Parquet part files into one file per table.

Rows are flushed to Parquet every `flush_interval_s` (default 5 min) and on shutdown. Raw batches are written immediately, so after a crash `reparse` can recover sidecar data. Hub-observed tables (`ref_block`, `zmq_*`, `node_state`) can lose at most one flush interval.

**Privacy:** the raw archive, the `connections` table and log text contain peer IP addresses. Publish data only after dropping or hashing those columns.

## Node states

Evaluated every 5 s; transitions are written to `node_state`. Thresholds are set in `hub.yaml`.

| State | Rule |
|---|---|
| `DOWN` | no upload for `silent_after_s`, or `monerod` exited |
| `UNRESPONSIVE` | `get_info` failed `unresponsive_polls` times in a row, or no RPC data for `silent_after_s` |
| `WRONG_CHAIN` | past `fork_height` with top block `major_version < min_major_version` |
| `FORKED` | tip hash differs from the canonical hash at that height for `fork_confirm_s` |
| `SYNCING` | `busy_syncing` and lag > `syncing_lag` |
| `STALLED` | height unchanged for `stall_after_s` while the canonical chain advanced |
| `FALLING_BEHIND` | lag ≥ `behind_blocks` and growing faster than `falling_slope_per_h` |
| `BEHIND` | lag ≥ `behind_blocks` |
| `OK` | otherwise |

When a node enters `STALLED` or `UNRESPONSIVE`, the hub asks its sidecar for a log-tail bundle. The sidecar sends one only if its operator set `ALLOW_REMOTE_CAPTURE=1`.
