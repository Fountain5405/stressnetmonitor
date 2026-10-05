# Data dictionary

All tables are Parquet under `parquet/<table>/date=YYYY-MM-DD/`. Read a whole table with:

```r
library(arrow); library(dplyr)
info <- open_dataset("parquet/info") |> collect()
```

or with DuckDB: `SELECT * FROM read_parquet('parquet/info/*/*.parquet', hive_partitioning = true)`.

**Conventions**

- `time` is when the sidecar took the measurement, on the **node's** clock, in UTC. `batch.clock_offset_s` estimates each node's clock offset (node minus hub, including network latency).
- `log_time` is `monerod`'s own log timestamp (UTC, millisecond resolution, node clock).
- Hub-observed tables (`ref_block`, `zmq_*`, `node_state`, `hub_event`) use the hub's clock.
- Byte counts and counters are doubles (exact to 2^53). Heights are int32. 128-bit difficulties are hex strings (`wide_*`).
- `host_sample`, `host_disk` and `process_info` hold **cumulative counters** exactly as read from `/proc`. Take differences between consecutive rows of the same node, and watch for resets when a machine reboots (`batch.boot_id` changes) or `monerod` restarts (`process_info.pid` changes).

## Tables in common with `monerod-monitor`

Column names follow the `monerod` RPC fields and [Rucknium/monerod-monitor](https://github.com/Rucknium/monerod-monitor), with `node_id` added. Every RPC row has `rpc_http_code` and `rpc_response_time` (seconds).

| Table | Source | Notes |
|---|---|---|
| `info` | `get_info` | `height` is chain length: top block = `height - 1` |
| `last_block_header` | `get_last_block_header` | |
| `pool_stats`, `pool_stats_histo` | `/get_transaction_pool_stats` | histogram rows numbered `histo_01`… |
| `fee_estimate` | `get_fee_estimate` | `fee_tier_1`…`fee_tier_5` |
| `connections` | `get_connections` | one row per peer per poll; **contains peer IPs** |
| `bans` | `get_bans` | |
| `process_info` | `/proc/<monerod pid>` | `cpu_time_*` in seconds; `mem_*` in bytes; `majflt` = major page faults (LMDB reads from disk) |

## Additional tables

| Table | Contents |
|---|---|
| `rpc_error` | RPC calls that failed: `method`, `rpc_http_code` (0 = no response/timeout), `error` |
| `host_sample` | Host counters: `cpu_*` (clock ticks, usually 1/100 s), memory and swap (kB), `load1/5/15`, `pgmajfault`, `pswpin/out`, `net_rx/tx_bytes`, `psi_*_us` (pressure stall totals, µs) |
| `host_disk` | Per device backing the data dir (`/proc/diskstats`): `reads`, `read_sectors` (512 B), `read_ms`, `writes`, `write_sectors`, `write_ms`, `in_flight`, `io_ms` (busy time), `weighted_io_ms` |
| `block_added` | From `monerod`'s "BLOCK SUCCESSFULLY ADDED" log message: `height`, `hash`, `pow_hash`, `difficulty`, reward split, `coinbase_weight`, `cumulative_weight`, `block_processing_ms`, `target_ms`, `longhash_ms` |
| `block_timing` | From `--show-time-stats 1`, per block (ms): `target`, `longhash`, `t1`, `t2`, `t3`, `t_exists`, `t_pool`, `t_checktx`, `t_dblspnd`, `tac`, `vmt`, `addblock`, `advance_tree` (FCMP++ curve tree). `total_ms = block_processing_ms + addblock_ms + advance_tree_ms`. `height` is the block height (`monerod` prints height + 1). |
| `tx_pool_log` | 1% sample (txid starting `00`, `01` or `02`) of txs entering the pool: `txid`, `weight`, `fee_per_byte`, `pool_count`, `pool_total_weight`. The same txids are sampled on every node, so arrival times can be compared across nodes. |
| `chain_event_log` | `kind` = `reorganize`, `reorganize_success`, `alt_block`, `invalid_block`, `orphaned` |
| `log_warning` | `monerod` WARNING/ERROR/FATAL lines (`level`, `category`, `location`, `thread`, `message`) |
| `node_profile` | Hardware/config key–value pairs reported by the sidecar (hourly and on `monerod` restart) |
| `sidecar_event` | `sidecar_start`, `monerod_found`, `monerod_exit`, `spool_dropped`, `log_warn_dropped` |
| `batch` | One row per upload: `seq`, `created`, `sent`, `recv_time`, `bytes`, `records`, `boot_id`, `clock_offset_s`, `sha256` |
| `ref_block` | Canonical chain from each reference: header fields and `first_seen` (hub time). `backfilled = true` rows were loaded at hub start, so their `first_seen` isn't meaningful. |
| `zmq_block`, `zmq_tx` | Block and tx arrivals on operator nodes and references via ZMQ (`recv_time` on the hub clock) |
| `node_state` | State transitions with `reason`, `height`, `lag` (see docs/hub.md) |
| `hub_event` | Hub start/stop, reference errors and reorgs, bundle uploads |
| `node_registry` | Hourly snapshot of `node_id`, `tier`, `position`, `created`, `revoked` |

## Useful joins

- **Block acceptance delay on a node:** `block_added` (node, `height`, `hash`, `log_time`), joined to the earliest `ref_block.first_seen` (or `zmq_block.recv_time` from a reference) for that hash. Correct `log_time` by subtracting the node's `clock_offset_s`.
- **Where block time goes:** `block_timing` joined to `ref_block` on `height`, to compare `t_checktx_ms` (txs that missed the pool cache), `t_pool_ms` and `advance_tree_ms` against `num_txes` and `block_weight`.
- **Tx propagation:** `tx_pool_log` (volunteers, sampled) or `zmq_tx` (operator nodes, all txs), joined on `txid` across nodes and to the tx generator's submission log.
