# stressnetmonitor

Telemetry and analysis for the Monero **FCMP++ & Carrot beta stressnet v3** ([`v0.19.0.0-beta.3.0`](https://github.com/seraphis-migration/monero/releases/tag/v0.19.0.0-beta.3.0)). The goals:

1. Find the **minimum hardware** that can run an FCMP++ node under heavy network load.
2. Find and document **bugs and rough behavior** under stress, with enough context to file good bug reports.

Design and discussion: [docs/design-overview.md](docs/design-overview.md) · [seraphis-migration/monero#495](https://github.com/seraphis-migration/monero/issues/495)

## Components

| | |
|---|---|
| [`sidecar/msnm-sidecar.sh`](sidecar/msnm-sidecar.sh) | A bash script that runs next to `monerod`. It queries local RPC, samples `/proc`, filters the `monerod` log, and pushes to the hub. Opens no ports. → [docs/sidecar.md](docs/sidecar.md) |
| [`hub/`](hub/) | Python service: sidecar ingest, reference-chain tracking, ZMQ, node states, Prometheus metrics, Parquet output, raw archive with a hash-chained ledger. → [docs/hub.md](docs/hub.md) |
| [`deploy/`](deploy/) | Example hub config, systemd unit, Prometheus and HAProxy configs |
| [docs/data.md](docs/data.md) | Data dictionary for the Parquet tables (R/arrow and DuckDB friendly; tables match [Rucknium/monerod-monitor](https://github.com/Rucknium/monerod-monitor) where they overlap) |

## Volunteers

To contribute data from your stressnet node, ask for a token in the stressnet Matrix room and follow [docs/sidecar.md](docs/sidecar.md). Run `msnm-sidecar --once` first to see exactly what it would send.

## Status

Early. Tested end to end against the v3.0 `monerod` in regtest, including detection of a hung `monerod` (with log capture) and a killed one (with a crash bundle). Not yet run on the live stressnet. Not built yet: the P2P crawler, Grafana dashboards, R starter scripts, and the tx-generator submission log spec.
