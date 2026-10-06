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

### Full blocks sustained for 3+ hours; steady ~20 s per block; the low-end node's only peer link drops a few times an hour (~10:00–13:15 UTC)

- **Load (measured, `block_timing.cumulative_weight`):** from ~10:00 the hourly median block is **10.1–10.2 MB**, so every block is full, compared with 1.5–5.6 MB overnight. The network produced only 20–27 blocks per hour.
- **Block timing (measured, `block_timing`):** per-hour medians stayed flat for over three hours.

  | Hour (UTC) | `ref-a` median / max | `op-g6950-hdd` median / max | `op-g6950-hdd` median `t3` |
  |---|---|---|---|
  | 10 | 0.59 / 0.61 s | **19.4 / 20.2 s** | 17.8 s |
  | 11 | 0.59 / 0.60 s | **19.7 / 20.6 s** | 17.9 s |
  | 12 | 0.59 / 0.60 s | **19.9 / 20.2 s** | 18.2 s |

  So the 15–19 s of the earlier burst is the steady state at this load, not a transient. `t3` (block transactions not already in the pool) is still ~90% of the time.
- **Pool snapshot (measured, `get_info` at 13:16):** `op-g6950-hdd` held 6,349 transactions, against 10,700 on `ref-a`. That is a size snapshot, not an admission rate.
- **RPC (measured, `rpc_error`):** 13–22 timed-out polls per hour on `op-g6950-hdd` between 10:00 and 13:00, against 1–2 per hour overnight.
- **Peer link (measured, `info.outgoing_connections_count`; `bitmonero.log`):**
  - From ~09:00, `op-g6950-hdd`'s single outbound connection to `ref-a` is missing in 4–6 polls per hour. Each gap is one poll (≤ ~30 s). There were none from 09:00 on 10-05 until 09:00 on 10-06, except one at 20:00.
  - `monerod` logged "monerod is now disconnected from the network" 5 times between 10:00 and 13:15.
  - `ref-a` never lost its outbound peers in the same period.
- **The gaps are part of constant connection churn (measured, `connections`, checked at 14:15):** the two nodes hold one connection in each direction.

  | Period | New connections per hour (out / in) | Longest connection |
  |---|---|---|
  | 18:00 on 10-05 to 09:00 on 10-06, lighter load | usually 1 / 1 | hours: ~10 h outbound, ~4.5 h inbound |
  | 10:00–14:00 on 10-06, full blocks | **11–14 / 16–19** | **6–16 min** |

  - Both ends see it: `ref-a`'s rows for this peer show the same connections being replaced.
  - A poll shows `outgoing_connections_count` = 0 only when it falls between a drop and the reconnect.
  - The `connections` rows before a drop look ordinary: state `normal`, receive idle ≤ ~70 s.
  - So which side closes the link, and why, isn't visible in the data collected now.
- **Hypotheses:**
  - **Not supported:** block verification blocking the P2P link. None of the 5 logged disconnects fell inside a block's verification window, which is `log_time − total_ms` to `log_time`, ±2 s. Block verification covered only ~11% of the time since 09:30.
  - **Now explained by the transaction-request tracker:** see the next entry.
- **Load easing (measured):** in 14:00–14:15 the median block was 6.7 MB on `ref-a`, and `op-g6950-hdd` took a median 14.7 s per block. 13:00–14:00 had 30 blocks at 10.2 MB, with a median 19.9 s per block on `op-g6950-hdd`.

### The link drops come from v0.19's transaction-request tracker, which gives up on its only peer (16:17–16:25 UTC)

Network logging was turned on for `op-g6950-hdd` at runtime at 16:17:03, at `net.p2p`/`net.cn` DEBUG plus `net.p2p.msg` and `default` INFO. `ref-a`'s logging stayed at the default.

- **What triggers the drop (measured, `bitmonero.log` on `op-g6950-hdd`):** at 16:22:31.755 the request tracker logged that `ref-a` had missed **709 of 992** transaction requests on that connection (**71%**). The next line is `Missed tx request more than threshold of the time, dropping peer`. `op-g6950-hdd` then dropped its outbound connection to `ref-a` with score 0, so no ban. The miss rate for that connection had climbed steadily before that: 50% → 54% → 60% → 65% → 67% → 71% over the previous ~80 s.
- **The rule (from source, v0.19.0.0-beta.3.0):**
  - A transaction request counts as missed when no reply arrives within 30 s (`P2P_DEFAULT_REQUEST_TIMEOUT`).
  - The peer is dropped when more than 70% of its requests are missed (`P2P_REQUEST_FAILURE_THRESHOLD_PERCENTAGE`), once there are at least 5 (`P2P_MIN_SAMPLE_SIZE_FOR_DROPPING`).
  - The counts are kept per connection and accumulate for its whole life.
  - When the answering node (`handle_request_tx_pool_txs`) doesn't have a requested transaction in its pool as "broadcasted", it leaves it out of the reply without saying so. The requester only finds out by the 30 s timeout.
  - A source comment notes that drops are not turned into bans "since we've observed honest peers get banned due to long response times on stressnet".
- **The reconnect is blocked for 15–30 s (measured):**
  - The old socket closes only 16–29 s after the drop (`closed in state normal`).
  - Meanwhile every reconnect attempt, about one per second, is closed before the handshake: 27 attempts at 16:18 and 14 at 16:22. The likely cause is the one-inbound-connection-per-address limit (`has_too_many_connections`), which still counts the old connection on `ref-a`.
  - `op-g6950-hdd` itself logged `CONNECTION FROM <ref-a> REFUSED, too many connections from the same address` for `ref-a`'s reconnects in the other direction.
  - With a single peer, the node is cut off for that time, and its own transactions can't be relayed (`Unable to send transaction(s), no available connections`).
- **Replies are either fast or absent (measured, requests made 16:21:15–16:23:50):**
  - 1,476 requests; 420 (28%) went stale. The stale ages were 30.3–33.1 s, so they were removed right at the timeout.
  - Requests that were answered got their reply in **0.1–0.2 s**.
  - Of the stale ones, 133 arrived later (35–90 s after the request) and 287 weren't seen in the window at all.
  - The stale share varied a lot between 30 s periods: 81%, 13%, 29%, 0% and 24%.
- **Not block verification (measured):** `op-g6950-hdd` added no block between 16:15:22 and 16:24:51, so the 16:22 drop happened while it wasn't verifying a block at all.
- **The first hypothesis, that `ref-a` silently omits transactions, is not supported.** `net.p2p.msg` INFO was enabled on `ref-a` at 16:50:37. See the follow-up below.

#### Follow-up: the replies arrive, but the low-end node doesn't read them in time (16:50–16:56 UTC)

- **`ref-a` answers everything (measured, `ref-a`'s `net.p2p.msg` log, 16:50:37–~16:52:30):**
  - It received 43 transaction-request messages from all its peers, covering 665 transactions, and replied to every one with the same count.
  - It logged **no** `Requested tx … not found in pool`.
  - The two request batches `op-g6950-hdd` sent on its own outbound link (57 and 77 transactions) were logged as received on `ref-a` within 3 ms and answered in full.
- **The replies wait unread on the low-end node (measured, `get_connections` on both nodes plus `ss` on `op-g6950-hdd`, four samples 16:55:03–16:55:29):**
  - On `op-g6950-hdd`'s link to `ref-a`, `ref-a`'s `send_count` minus `op-g6950-hdd`'s `recv_count` was **0.58–1.16 MB**.
  - That equals, to the byte, the kernel receive queue (`Recv-Q`) on `op-g6950-hdd`'s socket. So the data had arrived at the machine, but `monerod` hadn't read it.
  - In the other direction the counters matched exactly, so nothing was outstanding.
- **Per-transaction verification (measured, `HASH … ms:` lines, 15 min to ~16:56):** `op-g6950-hdd` p90 **103 ms**, max 137 ms; `ref-a` p90 30 ms, max 36 ms.
- **Drop reasons over the logged hour (measured, `op-g6950-hdd` `bitmonero.log`, 16:20:37–17:16):**
  - 18 connection drops in all.
  - **17** came from the transaction-request tracker (`Missed tx request more than threshold of the time`).
  - **1**, at 16:50:54, came from the block-download path (`Failed to request missing objects, dropping connection`, logged as ERROR).
  - Extra logging was set back to the defaults on both nodes at 17:16. With it on, `op-g6950-hdd`'s log grew ~35 MB in that hour.
- **Unexplained (measured):**
  - Three request batches (100 transactions) that `op-g6950-hdd` sent on the link `ref-a` had opened never appear in `ref-a`'s log.
  - `op-g6950-hdd` dropped that link for missed requests at 16:51:33.
  - A possible explanation is that `ref-a` had already closed that connection on its side, but that isn't checked.
- **Hypothesis (leading; consistent with the measurements but the mechanism isn't confirmed in code):**
  - `monerod` handles one connection's messages one at a time.
  - On the low-end CPU, a reply carrying N transactions takes about N × 0.1 s to verify. So a reply queued behind a few large batches on the same connection isn't read until more than 30 s after the request was sent.
  - The tracker counts from when the request was *sent*, so it blames the peer.
  - That would explain three things: replies are fast when the queue is empty; some arrive 35–90 s late; and the 30 s timeouts cluster in some periods but not others.
- **Consequence to report upstream:**
  - A node that is slow to verify transactions marks its honest peers as failing and drops them.
  - Because the 70% rule is cumulative over the connection's life, and the reconnect is then blocked for 15–30 s, a node with one or a few peers is cut off repeatedly under load: 11–19 times an hour between 10:00 and 14:00.

---

## Monitor notes

These are about the monitor's behaviour, not the network's.

- **False `BEHIND` (fixed 2026-10-06):** node heights come from 30 s RPC polls pushed every 30 s, while the canonical height is polled every 2 s. Right after a pair of quick blocks, healthy nodes looked 2 behind. The hub now compares each node with the canonical height at the moment the node took its sample.
- **`UNRESPONSIVE` while busy:** when `get_info` takes more than 10 s because the node is verifying a block, the hub reports `UNRESPONSIVE`. That's a real symptom, slow RPC under load, but not a crash. The `info.rpc_response_time` and `rpc_error` tables show how slow it was.
- **Expected warnings that aren't faults:**
  - "Unable to send transaction(s), no available connections" on a node whose only peer is `ref-a`, because `monerod` doesn't relay a transaction back to its source;
  - "There were N blocks in the last 90 minutes…" when testnet's hashrate drops.
- **Fixed in sidecar 0.1.1:** a "Grace blocks not equal to expected 1000" warning on every RPC poll.
