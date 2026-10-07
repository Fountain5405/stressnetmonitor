# stressnetmonitor

Telemetry and analysis for the Monero **FCMP++ & Carrot beta stressnet v3** ([`v0.19.0.0-beta.3.0`](https://github.com/seraphis-migration/monero/releases/tag/v0.19.0.0-beta.3.0)). The goals:

1. Find the **minimum hardware** that can run an FCMP++ node under heavy network load.
2. Find and document **bugs and rough behavior** under stress, with enough context to file good bug reports.

What we've seen so far: **[docs/findings.md](docs/findings.md)**. Design and discussion: [docs/design-overview.md](docs/design-overview.md) · [seraphis-migration/monero#495](https://github.com/seraphis-migration/monero/issues/495).

## Run a sidecar on your stressnet node

Running a stressnet node, especially on modest hardware? A sidecar next to it sends the measurements this project needs. It's a small bash script that opens no ports and runs at the lowest priority.

1. **Ask for a token and the hub URL** in the stressnet Matrix room.
2. **Run `monerod` with these extra flags** (plus `--seed-node`s on v0.19.0.0-beta.3.0; [see here](docs/sidecar.md#seed-nodes-v01900-beta30)):

   ```
   --show-time-stats 1 --log-level 0,blockchain:INFO,txpool:INFO
   ```

3. **Install the sidecar** on the same machine:

   ```bash
   git clone https://github.com/Fountain5405/stressnetmonitor.git
   cd stressnetmonitor
   sudo ./sidecar/install.sh --hub <hub URL>
   ```

   It asks for your token, checks everything, starts the sidecar as a systemd service, and confirms once the hub has received its first batch. Later, `sudo msnm-sidecar --status` tells you whether data is still reaching the hub. Without systemd or sudo, run it as your `monerod` user with `--no-systemd` instead, which uses `screen` plus an `@reboot` crontab line.

Details:
- what it sends;
- log size;
- running without systemd;
- upgrading and removing.

All of these are covered in **[docs/sidecar.md](docs/sidecar.md)**.

## Components

| | |
|---|---|
| [`sidecar/`](sidecar/) | `msnm-sidecar.sh` runs next to `monerod`: it queries local RPC, samples `/proc`, filters the `monerod` log, and pushes to the hub. `install.sh` sets it up. → [docs/sidecar.md](docs/sidecar.md) |
| [`hub/`](hub/) | Python service with these parts: sidecar ingest, reference-chain tracking, ZMQ, node states, Prometheus metrics, Parquet output, and a raw archive with a hash-chained ledger. → [docs/hub.md](docs/hub.md) |
| [`deploy/`](deploy/) | Example configs for running a hub: hub config, systemd units, HAProxy with TLS for public ingest, Let's Encrypt renewal, an SSH reverse tunnel, and Prometheus |
| [docs/data.md](docs/data.md) | Data dictionary for the Parquet tables. They work with R/arrow and DuckDB, and use the same table names as [Rucknium/monerod-monitor](https://github.com/Rucknium/monerod-monitor) where they overlap. |
| [docs/findings.md](docs/findings.md) | Dated log of observations: measured vs hypothesis, with pointers to the data |

## Status

- **Live since 2026-10-05:**
  - the hub;
  - two operator nodes: a Ryzen 3900X reference and a Pentium G6950 on a hard disk;
  - a public HTTPS ingest endpoint for volunteer sidecars.
- **Not built yet:**
  - the P2P crawler;
  - Grafana dashboards;
  - R starter scripts;
  - the spec for the transaction generator's submission log.
