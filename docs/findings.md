# Stressnet findings log

A dated log of what the monitor has observed on the FCMP++ & Carrot beta stressnet. Newest entries are at the bottom. Each entry says what was **measured** and what is only a **hypothesis**, and points to the raw data so that anyone can check it or redo the analysis.

Nodes are named by pseudonym. IP addresses are never recorded here.

## Setup

- **Network:** FCMP++ & Carrot beta stressnet v3 (`monerod` v0.19.0.0-beta.3.0). It uses testnet's network ID and ports, and forked from testnet at height 3,102,800 (v17). The fork had already happened when monitoring started.
- **Monitored nodes:** both are pruned, and both run `--show-time-stats 1 --log-level 0,blockchain:INFO,txpool:INFO`.

  | Node | Role | CPU | RAM | Disk | Peers |
  |---|---|---|---|---|---|
  | `ref-a` | reference (defines the canonical chain) | Ryzen 9 3900X (12c/24t) | 31 GB | NVMe (LVM) | public network |
  | `op-g6950-hdd` | low-end test node | Pentium G6950 (2c/2t, no AES-NI/AVX) | 3.6 GB | 320 GB 7200 rpm HDD | `ref-a` only (`--add-exclusive-node`) |

- **Load:** a transaction generator on a separate machine (2× EPYC 7H12) submits transactions to the network. Its submission log isn't yet joined with this data.

### Reading `--show-time-stats` lines

`monerod` logs one line per block added:

```
Height: N coinbase weight: W cumm: C p/t: P (target/longhash/t1/t2/t3/t_exists/t_pool/t_checktx/t_dblspnd/tac/vmt/addblock/advance_tree)ms
```

- `Height: N` is printed for **block N−1**. Heights in this log are true block heights.
- `cumm` is the block's cumulative weight in bytes, roughly its size.
- `p/t` is the processing time in ms. It excludes `addblock` and `advance_tree`.
- In v0.19.0.0-beta.3.0, `t3` (`src/cryptonote_core/blockchain.cpp`, lines 4613–4728) times the non-input consensus checks and the **FCMP++ batch verification of block transactions that were not already in the node's pool**. Transactions that are already in the pool were verified when they arrived, so `t3` is mostly the cost of transactions the node hadn't seen yet.

### Where the data is

The hub's Parquet tables (see [data.md](data.md)):
- `block_timing`: per-block timing lines.
- `chain_event_log`: reorgs and alternative blocks.
- `info`: `get_info` responses, including `rpc_response_time`.
- `rpc_error`: failed RPC polls.
- `pool_stats`: transaction-pool statistics.
- `node_state`: the hub's state transitions.
- `ref_block`: canonical blocks with the time the hub first saw each.

Raw sidecar batches are kept as well, and the tables can be rebuilt from them with `msnm reparse`.

---

## 2026-10-05

### Initial sync: a pruned node can't sync only from another pruned node (operational)

- **Measured:** `op-g6950-hdd` (pruning seed 391) stopped at height 9,071 while syncing only from `ref-a` (pruning seed 389). It connected and saw the higher chain, but never received more blocks. `ref-a` repeatedly blocked and unblocked it.
- **Mechanism (inferred):** a pruned node keeps full data for only 1/8 of the chain (its stripe), plus the most recent ~5,500 blocks. Seeds 389 and 391 are different stripes, so `ref-a` doesn't have data that `op-g6950-hdd` needs for its initial sync.
- **Consequence:** a pruned node behind `--add-exclusive-node` needs a peer with full data, or a copy of the database. We copied `ref-a`'s pruned LMDB (6.6 GB, 84 s over gigabit LAN). At the tip this stops mattering, because new blocks are always complete.

### Full pruned sync on the reference node

- **Measured:** `ref-a` synced from genesis to the stressnet tip (~3,103,034) in **~5.5 h**. The pruned database at the tip is **6.6 GB**.
- **Rate:** usually 250–300 blocks/s, at ~40% CPU with no iowait. One exception: near height **1.917M**, the rate fell to **~2 blocks/s**, with these timings:
  - `p/t` 60–530 ms per block, about 75% of it `t_checktx`;
  - **one** `monerod` thread at ~93% CPU while the machine was 95% idle;
  - no iowait, ~1 MB/s network.
- **Interpretation:** in transaction-heavy stretches of history, sync speed is set by single-thread CPU speed, not core count, disk or network.
- **Side effect:** during that stretch, `get_info` took **9–15 s** to answer. RPC waits behind the blockchain lock. The sidecar's 10 s RPC timeout therefore failed, and the hub reported the node `UNRESPONSIVE` although it was working.

### `prune_blockchain` RPC can stall a node on slow storage (operational hazard)

- **Measured:** one RPC call, `prune_blockchain {"check": true}`, made right after `op-g6950-hdd` started on the copied 6.6 GB database, blocked its RPC and incoming-transaction handling for **more than 10 minutes**:
  - the box read ~500 KB/s at ~47% iowait;
  - gdb showed `check_blockchain_pruning` → `prune_worker` → `is_v1_tx` → `mdb_page_search`, with `tx_memory_pool::add_tx` and the RPC threads waiting on the lock;
  - `systemctl stop` didn't finish within 5 min, and systemd killed the process. LMDB recovered cleanly.
- **Interpretation:** the check walks the whole database while holding the blockchain lock. That's harmless on NVMe, but on a hard disk with less RAM than the database it takes very long.
- **Advice for low-end operators:** don't run `prune_blockchain` with `check: true` on a running node on spinning disks.

### First block timings on the low-end node

- **Measured** on `op-g6950-hdd`, at 08:33 UTC, just after it started following the tip:
  - block 3,103,036 (4.09 MB): `p/t` **11.7 s**, of which `t3` 7.3 s, `t_exists` 2.3 s, `t_checktx` 2.0 s;
  - 0.82 MB and 1.70 MB blocks: 2.4 s and 4.4 s.

### Stress load begins; the low-end node falls behind (~10:50–11:07 UTC)

- **Load (measured):**
  - "Transaction added to pool" lines from 10:07 to 11:07: 8,528 on `ref-a` and 6,619 on `op-g6950-hdd`;
  - blocks 3,103,106–3,103,110 weighed 7.6–9.0 MB.
- **`op-g6950-hdd`:**
  - `p/t` was **81–89 s** per ~8 MB block, of which **`t3` was 75–84 s (~93%)**;
  - CPU was ~54% user and ~46% iowait;
  - the hub reported `FALLING_BEHIND`.
- **`ref-a`, same blocks:** `p/t` 0.2–9.7 s, with `t3` up to 9.6 s, so even the reference sometimes received blocks containing transactions it hadn't seen.
- **Lag behind `ref-a`:**
  - block 3,103,106 was added on `ref-a` at 10:57:28 and on `op-g6950-hdd` at 11:00:40, **+3 min 12 s**;
  - block 3,103,112 lagged by +71 s.
- **Time budget:** with ~2 min blocks, `op-g6950-hdd` spent ~70% of wall time verifying blocks.
- **Hypothesis:** `op-g6950-hdd` can't verify incoming transactions as fast as they arrive. Blocks therefore show up with most of their transactions unseen, which moves FCMP++ verification onto the block-processing path, where it delays the block. Two further measurements would test it:
  - pool size over time on both nodes (`pool_stats`);
  - `t_pool` against `t3` per block (`block_timing`).

### Heavier load; the low-end node stops answering RPC; a 1-block reorg (~22:55–23:08 UTC)

- **Load (measured):** ~**13.5k** pool additions per hour on `op-g6950-hdd`, about twice the morning rate. Blocks reached **10.0 MB**.
- **`op-g6950-hdd` block timings:**

  | Block | Weight | `p/t` | `t3` |
  |---|---|---|---|
  | 3,103,404 | 10.0 MB | 19.1 s | 18.2 s |
  | 3,103,405 | 3.0 MB | 12.3 s | 12.1 s |
  | 3,103,406 (later orphaned) | 10.0 MB | **93.1 s** | 91.5 s |
  | 3,103,408 | 10.0 MB | **53.1 s** | 51.6 s |

- **RPC:** `get_info` took **37 s**, so the hub reported `UNRESPONSIVE`.
- **Pools at 23:07:** `op-g6950-hdd` held **136** transactions and `ref-a` **824**. So the low-end node was missing most of the network's pool, consistent with the hypothesis above.
- **Reorg (measured):** a 1-block reorg at height **3,103,406**.
  - `ref-a` added the block that lost at 23:00:56 and switched chains at 23:03:36–37.
  - `op-g6950-hdd` added the losing block at 23:02:52. It spent 93 s verifying it, and switched chains at 23:06:37–42, **3 min after `ref-a`**.
  - Earlier events of the same kind: a reorg at 3,103,208 (~14:32 UTC) and an alternative block at 3,103,341 seen by `ref-a` (20:26 UTC).

## 2026-10-06

### Contrast: a large block is fast when its transactions are already in the pool

- **Measured** at 02:06 UTC: `op-g6950-hdd` added block 3,103,479 (**9.65 MB**) in **0.9 s**, with `t3` = 0 and `t_pool` 832 ms. Every transaction was already in its pool.
- **Interpretation:** this supports the hypothesis above. Block size alone doesn't make a block slow. What does is transactions arriving faster than the node can verify them for its pool, so blocks bring transactions it hasn't seen.

### Sustained full blocks; the low-end node keeps only ~38% of the transaction flow (~09:30–10:16 UTC)

- **Load (measured):**
  - Transaction arrivals rose from ~190/min (09:15–09:32) to **~620/min, about 10 tx/s** (09:32–10:15).
  - Pool additions from 09:15 to 10:15: **29,965 on `ref-a`** and **11,454 on `op-g6950-hdd`** (38%).
  - `ref-a`'s pool grew to 3,918 transactions at 10:16 and 4,094 a few minutes later.
- **Blocks are full (measured):**
  - Blocks 3,103,672–3,103,679 are almost all **10,098,237 bytes with 1,320 transactions each** (~7.65 KB per transaction).
  - The block weight median is 10,000,000 and the hard limit is 20,000,000. Blocks fill to just above the median, which is where the reward penalty starts.
  - So the network is at its penalty-free capacity, roughly 1,320 transactions per block, and the pool backlog grows.
- **`ref-a`:** `p/t` **0.41–0.49 s** per full block, with `t3` = 0 (every transaction already in its pool), `t_pool` ~0.47 s and `advance_tree` ~60 ms.
- **`op-g6950-hdd`:**
  - `p/t` **14.8–19.0 s** per full block, of which `t3` was 14.2–18.3 s. Most block transactions were unseen. `advance_tree` was ~0.68 s, 11× `ref-a`'s.
  - `get_info` took **28.9 s**, so the hub reported `UNRESPONSIVE`.
  - CPU was 50% user, 17% system and 33% iowait. Its pool held 609 transactions.
  - Lag behind `ref-a` per block:

    | Block | Lag |
    |---|---|
    | 3,103,674 | +19 s |
    | 3,103,675 | +48 s |
    | 3,103,676 | **+2 min 40 s** |
    | 3,103,677 | +1 min 46 s |
    | 3,103,678 | +59 s |
    | 3,103,679 | +26 s |

    It never fell more than ~3 blocks behind.
- **Interpretation:** this is the clearest evidence yet for the hypothesis.
  - At ~10 tx/s, a 2-core Pentium on a hard disk admits only ~38% of the network's transactions to its pool. The rest arrive inside blocks and are verified on the block path, at ~15–19 s per full block, compared with <0.5 s on the 3900X.
  - It keeps up with the chain, but with delays of up to minutes, and its RPC is unusable while it verifies.
  - What isn't yet separated is the split of the time between CPU (FCMP++ verification) and disk. A rough split could come from `host_sample` iowait and the `block_timing` fields.

---

## Monitor notes

These are about the monitor's behaviour, not the network's.

- **False `BEHIND` (fixed 2026-10-06):** node heights come from 30 s RPC polls pushed every 30 s, while the canonical height is polled every 2 s. Right after a pair of quick blocks, healthy nodes looked 2 behind. The hub now compares each node with the canonical height at the moment the node took its sample.
- **`UNRESPONSIVE` while busy:** when `get_info` takes more than 10 s because the node is verifying a block, the hub reports `UNRESPONSIVE`. That's a real symptom, slow RPC under load, but not a crash. The `info.rpc_response_time` and `rpc_error` tables show how slow it was.
- **Expected warnings that aren't faults:**
  - "Unable to send transaction(s), no available connections" on a node whose only peer is `ref-a`, because `monerod` doesn't relay a transaction back to its source;
  - "There were N blocks in the last 90 minutes…" when testnet's hashrate drops.
- **Fixed in sidecar 0.1.1:** a "Grace blocks not equal to expected 1000" warning on every RPC poll.
