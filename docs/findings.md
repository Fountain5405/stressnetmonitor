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

### Setup change: the low-end node gets outbound internet peers (17:56 UTC)

The aim is to test whether the drops above also happen with a normal peer set, rather than a single peer.

- **Configuration change:**
  - Before: `op-g6950-hdd` peered only with `ref-a` (`--add-exclusive-node`).
  - Since 17:56:24 UTC: `ref-a` is a priority node, plus four `--seed-node`s and the default 12 outbound connections.
  - It still accepts no inbound connections from the internet: the router forwards no ports.
  - The restart took 16 s, and the height was unchanged at 3,103,861.
- **Finding peers is slow (measured, `get_connections`; `net.p2p` INFO for 60 s at 18:01:31):**
  - In that minute it made 17 outbound attempts. 14 failed to connect (dead peer-list entries), and 12 handshakes were closed by the remote end (`LEVIN_ERROR_CONNECTION_DESTROYED`).
  - 2 of the 7 hosts that refused were peers `ref-a` already had. Both nodes share one public IP, and `monerod` accepts one inbound connection per address, so a second node behind the same NAT can only use peers the first isn't connected to.
  - The other 5 refusals are unexplained. Full or overloaded nodes are possible.
  - Its first internet peer arrived at 18:05:41, about 9 minutes after the restart.
- **Capped at 4 outbound peers from 18:10:49 UTC** (`--out-peers 4`, `ref-a` included):
  - Why: a remote node that collects more than 10 fail points against an IP blocks it for 2 minutes (`P2P_IP_FAILS_BEFORE_BLOCK`, `P2P_IP_BLOCKTIME`). The block closes every connection from that address, so a block caused by the slow node would also cut `ref-a`'s links. The cap limits that exposure and the competition for peers.
  - The restart took 9 s; the height was unchanged.
- **Guarding the reference node (measured baseline, `info` and `connections`, 00:00–18:00):**
  - `ref-a` held 10–12 outbound peers in every 10-minute window from 15:00 to 18:00, both before and after the switch.
  - It had 34–97 distinct outbound internet connections per hour, typically 46–56.
  - `op-g6950-hdd` goes back to a single peer if `ref-a` falls below 8 outbound peers for a sustained period, or goes above 100 distinct outbound connections in an hour.
- **Data from before 17:56 on 2026-10-06 is from the single-peer setup.** 17:56–18:10 had up to 12 peers; from 18:10, up to 4. Compare churn and drops across these boundaries.
- **The load stopped at the same moment (measured, `block_timing`):**
  - Blocks were full (10.41 MB) up to 3,103,860 (17:51:11). Block 3,103,861 (17:54:47) was partial at 2.53 MB, as `ref-a`'s pool emptied.
  - Every block from 3,103,862 (17:57:30) on is coinbase-only, and `ref-a`'s pool held 0 transactions at 19:26.
  - **So the multi-peer setup has not yet run under load.** Its churn and drop numbers can't be compared with the single-peer period until the transaction flow resumes.
- **Idle-network baseline with up to 4 peers (measured, `info`/`connections`, 18:10–19:25):**
  - `op-g6950-hdd` held 4 outbound peers throughout 19:00–19:25.
  - It saw 14 distinct outbound internet connections in those 25 min, using 3 internet slots. In the same window `ref-a` saw 31 across 12 slots.
  - `ref-a` stayed at 11–12 outbound peers, with 41 distinct outbound connections in 18:00–19:00, within its baseline.
  - **19:00–20:00, network still idle:**
    - `op-g6950-hdd` saw **34** distinct outbound internet connections on 3 slots, about 11 per slot.
    - `ref-a` saw 55 on 12 slots, about 4.6 per slot.
    - So the low-end node replaces its internet peers more than twice as often, even with no transaction load. Which side closes those connections isn't logged at the default level.
- **First light load under the 4-peer setup (measured, `block_timing`, 2026-10-07 00:07–00:20 UTC):**
  - Blocks 3,104,042–3,104,049 carried 0.18–0.85 MB of transactions. Before and after were empty blocks, and both pools were at 0 by 00:26.
  - `op-g6950-hdd` had every block's transactions in its pool already (`t3` = 0). It took 0.25–0.72 s per block, against 0.06–0.20 s on `ref-a`.
  - That is far below the ~10 tx/s flow that caused the drops, so it says nothing yet about them.

## 2026-10-07

### New controlled node: `exp-2c2g`, a "cheap VPS" virtual machine (from 10:46 UTC)

- **Purpose:** measure one fixed small-hardware profile under the same load as the other nodes, and later vary it (CPU features, disk speed, RAM, vCPUs) one thing at a time.
- **Shape (setup):**
  - A KVM virtual machine with **2 vCPU pinned to the two hyperthreads of one physical core** of a Xeon E3-1240 v3 (Haswell, 3.4 GHz, AES-NI/AVX2 exposed). That's the shape of a low-end VPS.
  - **2 GB RAM**, no swap.
  - A virtio disk on a SATA SSD (thin qcow2).
  - Debian 12 minimal.
  - The same `monerod` binary as the other operator nodes (v0.19.0.0-beta.3.0, sha256 `03df3f8a…`), pruned, with the same log flags.
  - Unlike the LAN nodes, it has its **own public address on the stressnet**, with inbound P2P open. That makes it the first operator node with a normal inbound+outbound peer set.
  - The host's other workloads (non-stressnet daemons) are kept off its core.
- **Initial sync from the network started at 10:46 UTC (measured, in progress):** block 17,500 of 3,104,428 after ~4 min, both vCPUs busy, 299 MB of RAM used, 2 inbound and 5 outbound peers. How long a 2 vCPU / 2 GB machine takes to join is itself a result; it goes here when the sync finishes.
- **The sync slowed about 20× at ~18:36 UTC, around block 1,912,800 (measured):**
  - **Before:** it averaged ~250,000 blocks/h up to 18:29 (block 1,907,514; chain length from `get_info`). Most of those blocks were small, ~10 kB each.
  - **After:** blocks average 100–200 kB each (the `cumm` weight in the log's `p/t` lines). The node now syncs 70–160 blocks/min, about 5,000/h. It took 780–1,100 blocks per 10 min from 18:40 to 19:30.
  - **What a block costs:**
    - Heavy blocks take ~0.3–1.1 s each to process.
    - Most of that time is one stage of the `p/t` breakdown (the 8th field, e.g. 832 of 1,036 ms).
    - Both vCPUs run at ~100% user time and the disk sits nearly idle, so the node is CPU-bound, not I/O-bound.
  - **RPC under that load:**
    - `get_info` took 10–21 s (5 samples at 19:30).
    - That's past the sidecar's 10 s RPC timeout, so the hub shows the node as `UNRESPONSIVE` even though it is alive and syncing.
    - Data: the `info.rpc_response_time` and `rpc_error` tables.
  - **Memory:** monerod's resident memory is 1.6 GB. 857 MB is still available (most of it page cache); there's no swap and no OOM.
  - **The heavy stretch ended at ~20:10 UTC, around block 1,923,000 (measured):**
    - It lasted ~10,000 blocks over ~1.5 h.
    - Blocks went back to 3–7 kB on average, and the node did 13,000–20,000 blocks per 10 min.
    - `get_info` answered in 3.4 s at 20:30, and the hub shows `SYNCING` again.
  - **Hypothesis:** the heavy chain near the tip, where the stressnet load ran, will slow it the same way. The finishing time will tell.
- **It lost all its peers at once at 2026-10-08 01:27:28 UTC, around block 2,807,600, and then began blocking hosts (measured):**
  - Within 0.03 s, the log printed "monerod is now disconnected from the network" four times.
  - From 01:28:48 the node blocked 9 remote hosts in 3 minutes. Each block lasts 2 minutes.
  - Peer count went from 38 in / 12 out (00:29) down to 3–9 in / 3–4 out.
  - The sync kept going at ~4,500 empty blocks/min, so it isn't stuck.
  - There were no warnings or errors before the drop.
  - The log level (`0`) doesn't record why a host is penalised, so the cause is unknown.
  - Data: peer counts are in the `info` table; the drop and blocks are in the node's own log.
  - **An hour later (02:29):**
    - It kept blocking hosts at ~115/h: 49 in 01:28–01:55 and 65 in 01:55–02:28.
    - There was no second total disconnect.
    - Peers recovered to 16 in / 8 out.
    - The node reached block 3,038,215 (synced to ~67,000 below the tip).
- **It reached the stressnet's heavy chain at ~02:45 UTC on 2026-10-08, around block 3,100,661 (measured):**
  - **Speed:** past the FCMP++ fork height (3,102,800) blocks are 0.8–3.6 MB. Each takes 1.3–5.5 s to process, and the node manages ~10 blocks/min.
  - **Where the time goes:** the dominant `p/t` stage is now the 5th field (e.g. 5,287 of 5,544 ms). On the pre-fork heavy patches it was the 8th field.
  - **Memory:** at 03:30, monerod's heap (`AnonPages`) was 1.54 GB of 1.98 GB. Page cache was squeezed to ~290 MB, leaving 262 MB available, with no swap. There's no OOM kill yet.
  - **RPC:** `get_info` took 6.8 s.
  - **Position at 03:29:** block 3,103,362, 1,660 blocks behind the tip.
  - **Hypothesis:** if the rest of the chain looks like this, it needs another ~3 h to reach the tip, and memory is the likely limit for a 2 GB machine.
  - **At 04:29 (measured):**
    - It's on the full ~10 MB blocks of the stressnet's peak load (block weight 10,197,687).
    - Each takes 15–16 s, almost all in the 5th `p/t` stage (e.g. 15,924 of 16,402 ms), so ~4 blocks/min.
    - It gained only 392 blocks in the hour and is at block 3,103,754.
    - Memory has eased to 334 MB available (heap 1.47 GB). There's no OOM and no restart.
    - Reading: 15–16 s per full block is ~13% of the 120 s block time. Keeping up at the tip should be feasible on this shape, but catching up is slow: ~4 full blocks/min.
  - **Hypothesis:** load stopped at block 3,103,861, so ~105 full blocks remain (about 30 min), followed by empty blocks to the tip.
- **Result: the initial sync from the network took 18 h 15 min (measured).**
  - It started 2026-10-07 10:46:19 UTC.
  - At 2026-10-08 05:00:53 UTC monerod logged "You are now synchronized", with the tip near block 3,105,060. That's on a 2 vCPU (one core), 2 GB RAM, SATA SSD VM with no restarts and no OOM.
  - **Rough split of the time:**
    - ~14 h for blocks 0–3,100,600, mostly empty or light, at 170–270k blocks/h;
    - ~1.5 h for the pre-fork heavy patch near block 1.91M;
    - ~2.3 h for the ~2,400 stressnet blocks before the tip. Of those, the full ~10 MB blocks took 15–16 s each.
  - **Peak memory:** ~262 MB available out of 1.98 GB (03:30). The 2 GB of RAM was the tightest resource; CPU was the slowest one.
  - **Once synced:** a 1.5 MB block at the tip took 348 ms (its transactions were already checked in the pool). `get_info` answers in 4 ms. Memory stays at ~330 MB available.
  - Data: the `info` table (height, `synchronized`) and the node's log (`p/t` lines).
- **Hypothesis:** while far behind, it scores peers' replies as failures and blocks them, possibly because the reported chain tops differ (peers reported tops of 3,104,435 and 3,104,450 against a real tip near 3,104,950). Unconfirmed; it needs `net.p2p`/`net.cn` debug logging.
- **Known confounder:** a non-stressnet testnet daemon (`v0.18`, same network ID) shares this node's public IP. Remote nodes allow one inbound connection per IP, and an IP block from fail points applies to both, so some of this node's connection churn may come from its neighbour.

## 2026-10-08

- **Full blocks came back at ~20:13 UTC (first since 2026-10-06 17:54), block 3,105,426 (measured).** Blocks weigh 9,941,896 bytes. Per block, processing took (`p/t` in the node's log):
  - `ref-a`: ~12 s.
  - `exp-2c2g` (2 vCPU / 1 core, 2 GB): ~72 s.
  - `op-g6950-hdd` (Pentium / HDD): ~107 s, close to the 120 s block time. Its `get_info` stopped answering and the hub showed `UNRESPONSIVE`.
  - Data: the `block_timing` table and each node's log.
- **`ref-a` lost outgoing peers while `op-g6950-hdd` ran with 4 peers under that load (measured):**
  - Outgoing peers swung between 12 and 6 from 20:09, then fell to 2–4 by 20:32 (baseline 10–12). That hour it made 59 distinct outbound connections (baseline 34–97).
  - It never did this in the single-peer period under full blocks, or in the idle hours since.
  - **Action:** at ~20:33 UTC, `op-g6950-hdd` was reverted to the single-peer setup (`ref-a` as its only peer). That was a few minutes short of the written 10-minute limit, because the count was still falling.
  - **Hour totals (20:00–21:00, `trigger.py`):** `ref-a` was under 8 outgoing peers in 18 of 119 polls (about 9 min) with a minimum of 2, and made 98 distinct outbound connections (limit 100). `op-g6950-hdd` took a median 82 s per block over 22 blocks.
  - **After the revert,** `ref-a` was back to 10 outgoing peers by 20:40 and held 11–12 for the whole 21:00 hour with 32 outbound ids so far (measured).
  - **Hypothesis, not proven:** the slow reader (`op-g6950-hdd`) with more peers draws failures and blocks on the shared public IP, which also cuts `ref-a`'s connections. The network-wide load could also have caused it; the recovery after the revert favours the first reading but is one observation.
  - **Next:** a second round (4 peers, then 1) would separate the two.

---

## 2026-10-15/16

- **All four `vol-02` nodes went dark within 24 s of midnight UTC and came back without monerod (measured):**
  - Last RPC-bearing batches: 23:59:33–23:59:57 UTC on 2026-10-15, all fully normal (6 RPC methods, HTTP 200) — a hard stop, not a degradation.
  - The batch header's `boot_id` changed on every one of them, so all four hosts rebooted, together. They returned staggered between 04:05 and 10:35 UTC on 10-16.
  - After the reboot each pushes heartbeat batches whose header has an empty `monerod_pid` and whose only rows are host samples: the sidecar service auto-starts, monerod does not. The hosts themselves are healthy (load ~0.01, ~7 GB RAM free on an 8 GB box).
  - **Data hole:** no RPC, log or process data for `vol-02a..d` from 2026-10-15 23:59 UTC until their operators start monerod again.
  - **Hypothesis (not confirmed):** provider- or operator-side maintenance reboot; monerod isn't enabled on boot on those boxes (`systemctl enable monerod` would fix it).
  - Data: `raw/vol-02?/<date>/` batch headers and bodies; hub state transitions in the hub journal.
  - Build note (measured, from the last good batches): `vol-02b` and `vol-02d` run `0.19.0.0-beta.3.0-b8433e284` — a git-commit build, not the `-release` binary every other node reports. Same beta tag; keep it in mind when comparing per-block timings across volunteers.

- **New operator node `op-q6600-ssd` joined (2026-10-16 19:45 UTC, measured):**
  - Hardware: Intel Core2 Quad Q6600 (4 cores / 4 threads, 2.4 GHz, 2007-era desktop), 3.8 GB RAM, consumer SSDs (root SSD nearly full; a 448 GB SSD holds the LMDB). Our oldest CPU by a wide margin and the first 4 GB-class RAM node between `exp-2c2g` (2 GB) and the rest.
  - Setup: same stressnet binary as every node (v0.19.0.0-beta.3.0), pruned testnet, single peer (`ref-a`), initial sync from block 1 started 19:45 UTC. (The sync was later restarted with a temporary 4-peer bootstrap — see the pruning finding below — and was paused on 2026-10-17 for a machine move at height 915,487.) It reached the tip on 2026-10-18 after 35 h 46 min: see 2026-10-18.
  - During the sync its batches carry `rpc_error` rows with `status BUSY` instead of headers — expected behaviour (see monitor notes), not faults.
  - **A pruned node cannot initial-sync from a single pruned peer (measured):** with `ref-a` (pruning seed 389, one stripe) as its only peer, `op-q6600-ssd` made zero progress from block 1 for 45+ minutes: the connection handshakes, `SYNCHRONIZATION started`, no data flows (0 B/s), and the connection drops on a regular ~24 s cycle. Reason: old block spans exist only on peers whose pruning stripe covers them, and one pruned peer covers at most 1/8 of the chain; near-tip blocks (within the ~55 k pruning horizon) are the only ones every pruned peer serves — which is why single-peer peering works fine *after* a node reaches the tip. The same failure appears in `op-g6950-hdd`'s log from 2026-10-06 (`Failed to request missing objects, dropping connection`) during its initial single-peer sync; it finished only once it had several internet peers. Practical rule: bootstrap a pruned node with multiple peers (or copy a synced LMDB), then drop to single-peer.
  - **New reference node `ref-b` joined (2026-10-17 04:20 UTC):** the first UNPRUNED node in the fleet — 16-core / 32-thread Ryzen 9 5900XT, 31 GB RAM, chain on a two-SSD LVM stripe. Registered as the hub's second reference (the canonical chain follows the freshest reference by cumulative difficulty, so `ref-a` stays canonical while `ref-b` syncs). Purpose: a full-chain anchor on the LAN so future pruned nodes can bootstrap single-peer instead of using the 4-peer workaround. Sync duration to be recorded here. Topology from 2026-10-17 ~05:00 UTC (operator decision): `ref-b` is the LAN's only internet-facing stressnet node (outbound, 8 peers), and `op-q6600-ssd` syncs from `ref-b` and `ref-a` over the LAN only. Rationale: a full node is the only kind of peer that can serve a pruned node's whole initial sync, and one internet-facing node per shared public IP avoids competing with `ref-a` for remotes' one-connection-per-IP slots. Measured at the switch: of the 25 peers in `ref-a`'s white list, 10 were full nodes (pruning seed 0).


## 2026-10-17

- **`ref-b` full (unpruned) initial sync: ~5 h 31 min (measured):** monerod started with internet peers at 04:48:35 UTC from height ~35,000 (the staging period had only fetched the first blocks) and logged "You are now synchronized" at 10:19:20 UTC, chain height 3,111,589 (top block 3,111,588). Hardware: Ryzen 9 5900XT, 31 GB RAM, two-SATA-SSD stripe; database 17.4 GB unpruned (vs 13.75 GB on the pruned nodes). Since then it is OK in the hub, at the same height as `ref-a`. Data: `info`, hub `node_state`; monerod log on the node.
- **`ref-b` accepts inbound internet connections (measured):** from 04:52:39 UTC, 4 minutes after start, it has had inbound peers from internet addresses; at 16:00 UTC 20 of its 22 inbound peers were internet peers (10 full nodes, 10 pruned). The inbound path is a static port forward to this node set up by the operator, which is intended: from ~16:30 UTC `ref-b` is the LAN's internet-facing node in both directions, with inbound capped at 32 peers (`--in-peers 32`). `ref-a` still shows only LAN inbound peers. Data: `connections` (`incoming`, `host`).
- **`op-q6600-ssd` (Core2 Q6600) sync slows ~50x near block 1,905,000 (measured):** hourly progress was 105k–186k blocks/h from 04:00 to 11:59 UTC (heights 921,491 → 1,904,840), then 9.4k, 2.2k, 1.8k, 3.1k blocks/h in the next four hours. At 16:00 UTC, over 300 consecutive blocks: average 497 ms per block in `p/t` timing, 0.58 s wall-clock per block, monerod at ~215% CPU, ~0% I/O wait, 437 MB swap used of 3.8 GB RAM. So it is CPU-bound on block verification, not waiting on its LAN peers or the disk. **Correction (measured, 17:00 UTC):** the slow stretch ended near block 1,923,000. It covered ~18,000 blocks in ~4 hours, and from 16:00 UTC the rate was back at 20,000–46,000 blocks/h (half-hourly progress, `info`). So it was a stretch of heavier blocks, not a sustained rate. The "~7 days to the tip" projection made from it at 16:00 UTC was wrong. **Hypothesis:** the blocks from ~1.905 M to ~1.923 M carry many more transactions (ring-signature and range-proof verification) than those around them, and a 2007-era 4-core CPU verifies them at ~2 blocks/s. A minimum-spec data point: a heavy stretch costs this CPU ~0.5 s per block. Whether the whole initial sync is practical will show in the total time, recorded when the node reaches the tip. The node is left to finish on its own so the full sync duration can be recorded. Data: `block_timing` (`total_ms`), `info` (`height`), monerod log `Height:` lines.
- **RPC response time separates the nodes; the median does not (measured, 15:00–21:00 UTC, ~720 `get_info` polls per node):** the sidecar times each RPC call on the node itself, so no network time is in it; it polls every 30 s with a 10 s limit per call. At the tip every node answered `get_info` in 0.6–2 ms at the median; the difference is in the slowest calls. `ref-a`/`ref-b`: 95th percentile 0.6 ms, slowest 48 ms. `exp-2c2g` and `vol-02a`–`d`: 95th percentile 1.5–4 ms, slowest 0.07–0.9 s. `op-g6950-hdd` (spinning disk): 95th percentile 100 ms, slowest 2.0 s. `vol-01` (data disk ~100% busy with ~300 ms I/O latency in one snapshot): 90th percentile 120 ms, 95th 720 ms, slowest 5.9 s. `op-q6600-ssd` during its initial sync: 42% of calls got no answer within 10 s (all timed out at 10.0 s); the ones that answered took 1.4 s at the median and 7.3 s at the 90th percentile, so the median over all calls is around 6–7 s. Its monerod kept adding blocks the whole time; the hub showed it as UNRESPONSIVE about half the time from 11:11 UTC. **Hypothesis:** while syncing, `get_info` waits for block processing to release a lock the two share, and the two slow-disk nodes' tails come from disk I/O (a correlation only; neither is verified). Data: `info` (`rpc_response_time`), `rpc_error` (`method`, `rpc_response_time`).

## 2026-10-18

- **`op-q6600-ssd` (Core2 Q6600, 3.8 GB RAM) reached the tip after an initial sync of 35 h 46 min (measured).**
  - **When:** monerod logged "You are now synchronized" at 07:32:46 UTC, at chain height 3,112,213 (top block 3,112,212). Its first height poll was at 2026-10-16 19:47:08 UTC, at height 1.
  - **What the wall-clock time includes:** the setup changes listed under 2026-10-15/16. These were:
    - the stall with a single pruned peer;
    - the restart with a 4-peer bootstrap;
    - a pause for a machine move at block 915,487 (the polls show a 15-minute gap at 10-17 03:44 UTC);
    - the switch to LAN-only peers at 10-17 04:48 UTC.
  - **For comparison:**
    - `exp-2c2g` (2 vCPUs on one core, 2 GB) took 18 h 15 min from the network, to the 10-08 tip, about 7,000 blocks lower.
    - `ref-b` (Ryzen 9 5900XT, unpruned) took 5 h 31 min.
  - **Where the time went (measured).** Sources: `info` (`height`), `block_timing` (`total_ms`) and `ref_block` (`num_txes`, `block_weight`). Transaction counts start at block 3,102,307, where the hub's reference data begins.

    | Blocks | Produced | Wall-clock on `op-q6600-ssd` | Transactions | Block weight | Verify time (sum of `total_ms`) | Median per block |
    |---|---|---|---|---|---|---|
    | 1 – 3,101,999 | before the stress load | 31 h 36 min (10-16 19:47 → 10-18 03:23) | — | — | — | — |
    | 3,102,000 – 3,112,213 | 10-04 → 10-18 | 4 h 10 min (03:23 → 07:33) | 736,592 | 6.16 GB | 14,371 s (4.0 h) | 392 ms |
    | of which 3,103,000 – 3,103,999 | 10-05 06:00 → 10-06 ~22:00 (the first spam waves) | 2 h 23 min (03:23 → 05:46) | 556,592 | 4.35 GB | 8,028 s (14.4 ms per transaction) | 4.7 s and 6.6 s (the two 500-block halves) |
    | of which 3,105,425 – 3,105,433 | 10-08 (the slowest blocks) | — | 56 per block | 9.94 MB per block | — | 86–90 s each |

  - **Same blocks, both verified during an initial sync (measured):**
    - The two 500-block halves of the spam wave took `exp-2c2g` a median 4.4 s and 6.1 s per block. `op-q6600-ssd` took 4.7 s and 6.6 s.
    - So the 2007 quad-core is about 7% slower per block than a VM with 2 vCPUs on one modern core.
    - **Hypothesis:** this fits verification being spread across all cores, with four 2007-era cores roughly matching one modern core.
  - **Not comparable:** `ref-a` (190–236 ms) and `op-g6950-hdd` (686–1,028 ms) verified the same blocks as they arrived, with most of their transactions already checked in the pool.
- **At the tip, `op-q6600-ssd` isn't the slowest node (measured).** The window is 128 blocks (3,112,214–3,112,341, 07:33–12:15 UTC) on a quiet network, about 23 transactions per block. Source: `block_timing`.

  | Node | Median verify time | p90 | Max |
  |---|---|---|---|
  | `op-q6600-ssd` | 87 ms | 163 ms | 323 ms |
  | `op-g6950-hdd` | 111 ms | — | — |
  | `exp-2c2g` | 102 ms | — | — |
  | `ref-a` | 24 ms | — | — |
  | `ref-b` | 38 ms | — | — |

  - **During the sync:** blocks with similar contents (3,111,500–3,112,213, about 20–22 transactions each) took it a median 418–456 ms, about 5× more.
  - **Hypothesis:** at the tip it checked those transactions as they entered its pool, so block verification mostly skips them (see 2026-10-06, "a large block is fast when its transactions are already in the pool"). Whether it keeps up under a spam wave depends on how much of the transaction flow its pool can keep up with. That is untested until the next wave.
  - **Resources at the tip:** 3.8 GB RAM, with about 0.7 GB in swap and 2.1 GB available. The pruned database is 13.76 GB.

## 2026-10-19/20

- **The block rate swung both ways on 10-19 (measured).**
  - **Faster:** from 12:30 to 13:30 UTC, 53 blocks arrived, 1.8× the 2-minute target. Every LAN node logged monerod's warning "There were 72–74 blocks in the last 90 minutes, there might be large hash rate changes".
  - **Slower:** from 16:30 to 23:10 UTC there were six gaps of more than 10 minutes between blocks. The longest was 19.6 min (19:27–19:46 UTC).
  - All nodes followed without a reorg. Sources: `ref_block` (`timestamp`, `first_seen`), `chain_event_log`, and the monerod log.
- **The transaction load stopped at about 00:28 UTC on 10-20 (measured).**
  - **Before the stop:** the load was already light. The hub's ZMQ feed (from `ref-a` and `op-g6950-hdd`) received about 96 unique transactions per 10 minutes from 23:30 to 00:20 UTC.
  - **The stop:** the hub received its last transaction at 00:27:55, and `ref-a` logged its last txpool line at 00:27:54. Block 3,113,397 (00:29:30, 51 transactions) is the last with transactions. The 28 blocks since then, 3,113,398–3,113,425 (to 01:17 UTC), carry only the coinbase.
  - **It's network-wide:** at 01:20 UTC `tx_pool_size` was 0 on `ref-a` and on the internet-facing `ref-b`, which has about 30 internet peers. Our nodes don't mine, so outside miners were producing empty blocks too. This is not a relay fault on our side.
  - **Hypothesis:** the load generator was stopped or paused; the cause isn't known.
  - Sources: `zmq_tx` (`recv_time`), `ref_block` (`num_txes`), `info` (`tx_pool_size`).

## 2026-10-22/24

- **`vol-03`, a 2 vCPU / 2 GB cloud VM, reached the tip after an initial sync of 37 h 22 min, with 12 RPC stalls on the way (measured).**
  - **The node:** a volunteer's pruned node (`--prune-blockchain`) with internet peers. Its sidecar reports 2 vCPUs (virtualized, with AES-NI and AVX2), 2 GB RAM and a network block volume. A 4 GB swap file appeared at 17:38 UTC on 10-22, 16 minutes after the sidecar's first sample (hypothesis: the operator added it). Source: `node_profile`, `host_sample` (`swap_total_kb`).
  - **When:** monerod's `start_time` is 10-22 11:45:52 UTC. The hub first saw the node at 17:22:04 UTC, at height 604,496. Its first poll at the tip was at 10-24 01:07:34 UTC (chain height 3,116,297, top block 3,116,296), and the hub marked it OK at 01:08:06. That is 37 h 22 min from monerod's start and 31 h 45 min from the hub's first sample. **Hypothesis:** it started from an empty data dir; the hub can't see before 17:22. All 3,119 polls in the sync carry the same `start_time`, so monerod never restarted. Source: `info` (`height`, `start_time`), `node_state`.
  - **For comparison:** `op-q6600-ssd` (Core2 Q6600, 3.8 GB, LAN peers only) took 35 h 46 min to a tip about 4,000 blocks lower (see 2026-10-18); `exp-2c2g` (2 vCPUs on one core, 2 GB) took 18 h 15 min to the 10-08 tip; `ref-b` (Ryzen 9 5900XT, unpruned) 5 h 31 min. The last 4,000 blocks carry almost nothing, so the two slow nodes' totals are comparable.
  - **Where the time went (measured).** The `info` height is the chain length, so each boundary is within one block. Transaction counts and weights come from `ref_block`, which starts at block 3,102,307.

    | Blocks | Wall-clock on `vol-03` | Transactions | Block weight |
    |---|---|---|---|
    | 604,496 – 999,999 | 4 h 52 min (10-22 17:22 → 22:14) | — | — |
    | 1,000,000 – 1,999,999 | 11 h 42 min (→ 10-23 09:56) | — | — |
    | 2,000,000 – 2,999,999 | 9 h 18 min (→ 19:14) | — | — |
    | 3,000,000 – 3,102,999 | 1 h 1 min (→ 20:15) | — | — |
    | 3,103,000 – 3,103,999 (the first spam waves) | 2 h 31 min (→ 22:47) | 556,592 | 4.35 GB |
    | 3,104,000 – 3,105,424 | 6 min | 4,921 | 0.06 GB |
    | 3,105,425 – 3,105,440 (16 blocks of ~9.5 MB) | 23 min (22:53 → 23:16) | 1,018 | 0.15 GB |
    | 3,105,441 – 3,109,901 | 1 h 3 min (→ 10-24 00:20) | 112,395 | 1.04 GB |
    | 3,109,902 – 3,113,519 | 44 min (→ 01:04), 82 blocks/min | 76,808 | 0.70 GB |
    | 3,113,520 – 3,116,296 (nearly empty) | 3 min (→ 01:07), about 800 blocks/min | 42 | 0.001 GB |

    The spam-wave thousand took `vol-03` 2 h 31 min against 2 h 23 min on `op-q6600-ssd`.
  - **RPC stalls (measured):** the hub marked the node `UNRESPONSIVE` 12 times, all on 10-23, for 22.5 min of state time in all. Each mark follows 5 minutes without a `get_info` answer, so the silence was the episode plus 5 min. Ten episodes lasted 0.5–2 min: eight between 07:16 and 13:44 UTC (heights about 1.9–2.3 million, the region that also slowed `op-q6600-ssd` near 1.905 million), and two at 19:24 and 21:02 (around 3 million and in the spam wave). The two longest were 5 min at 22:58:38 and 9 min at 23:08:08 (about 14 min of silence), while it verified the 16 blocks of ~9.5 MB (about 64 transactions and 149 KB per transaction). There were none after 23:17:09. During the late sync, answered `get_info` calls took 0.15–7.1 s; at the tip they take 2–8 ms. Source: `node_state`, `info` (`rpc_response_time`).
  - **Memory (measured):** the lowest MemAvailable was 11 MB at 10-23 03:39:57, with 2.06 GB of swap in use. During the 9-min stall it fell to 132 MB. 170 of 25,219 samples were under 200 MB. CPU was 71–75% busy (user) with 3.6–5.2% steal through the last tx-bearing hours. At the tip: MemAvailable about 450 MB, about 2.1 GB of swap still in use (it hasn't come back), the CPU 97% idle and block processing 48 ms. Source: `host_sample`, the hub's metrics.
  - **Database size isn't an ingest measure late in a sync (measured):** `database_size` grew 22–25 MB per minute until 10-24 00:35, then stayed at 13.765 GB from 00:41 to the tip, although about 0.35 GB of block weight and 4,700 blocks were still added. At the tip the LAN's pruned nodes read 13.752–13.764 GB. **Hypothesis:** LMDB reused freed pages, so the file had already reached its final size. Source: `info` (`database_size`).
  - **Hypothesis:** the long stalls come from memory pressure (2 GB RAM, about 2 GB in swap) on top of the per-transaction verification cost of the largest blocks. The node never crashed or restarted, so 2 vCPU / 2 GB plus swap is enough to finish an initial sync of this chain, but its RPC can go silent for over 10 minutes on full blocks.

## 2026-10-24/26

- **`op-n450-hdd` (Atom N450, 1 core, 950 MB RAM, rotational disk) syncs at about 14,000 blocks an hour and drops its only peer after every 9,999 blocks (measured); the rule held for 66 of 72 intervals.**
  - **The node:** an Intel Atom N450 at 1.66 GHz (1 core, 2 threads), 973,572 kB RAM, 1.9 GB swap and a rotational 160 GB disk, running a pruned node whose only peer is `ref-b` (an exclusive-node unit). It does the full sync from block 1. Source: `node_profile`.
  - **Progress (measured):** the first poll was at 2026-10-24 04:13 UTC (chain length 101). At 2026-10-26 10:24 UTC it was at 762,128, which is 14.1k blocks an hour on average over 54 h 11 min. Source: `info` (`height`).
  - **Hypothesis:** at that rate the remaining 2.36 million blocks need about 7 more days. The other slow nodes were slower in the later, transaction-heavy regions (see 2026-10-22/24), so this is a lower bound.
  - **For comparison (measured):** `vol-03` (2 vCPUs, 2 GB) covered blocks 604,496–999,999 in 4 h 52 min, about 81k blocks an hour. Over the same region `op-n450-hdd` managed about 13–14k an hour.
  - **Peer drops (measured):** the log says "We can download nothing from this peer", then "Failed to request missing objects, dropping connection", then "monerod is now disconnected from the network", and monerod reconnects. 73 such events were logged in `log_warning`, from 2026-10-24 04:51 to 2026-10-26 09:52 UTC. Of the 72 intervals between them, 66 were 9,500–10,500 blocks (from the chain length at the nearest 30 s poll, so within about 200 blocks; median 9,999) and 37.9–51.6 min (median 42.0). In the log the exact figures are 9,999 blocks between drops, for example "Synced 734,730" at 08:23:53 and "Synced 744,729" at 09:08:40 UTC on 10-26. The six outside 9,500–10,500 blocks (the first two only by the poll's rounding):

    | Drop (UTC) | Chain length at the drop | Blocks since the previous drop | Minutes since the previous drop |
    |---|---|---|---|
    | 10-24 15:17:11 | about 170,600 | 10,599 | 39.7 |
    | 10-25 08:58:47 | about 420,200 | 9,499 | 43.0 |
    | 10-25 13:28:55 | about 482,400 | 12,199 | 59.0 |
    | 10-25 14:52:33 | about 501,200 | 18,799 | 83.6 |
    | 10-25 17:36:33 | about 533,300 | 22,199 | 116.6 |
    | 10-25 18:43:04 | about 544,900 | 11,699 | 66.5 |

    The four between 13:28 and 18:43 on 10-25 are the long ones. They fall inside the hours with most of the unresponsive episodes below; whether drops went unlogged there, or the windows really ran longer, is not established. *Resolved in 2026-10-26/27:* each of the four came exactly 9,999 blocks after a disconnect of another kind, and the first two were exactly 9,999 blocks apart.
  - **Interval in time (measured):** 38.1–40.3 min per 10,000 blocks in the first six hours (about 15.7k blocks an hour), 43.3–46.5 min from 10-26 03:00 to 07:30, 51.6 min at 08:23 UTC and 44.0 min at 09:52.
  - **Hypothesis:** monerod asks the peer for a chain of up to 10,000 block ids, and when the ids run out it logs "We can download nothing from this peer" (`cryptonote_protocol_handler.inl:2605`) and drops the connection. The drop is therefore a count of blocks, not of time, which fits the data: the interval is 9,999 blocks whether it takes 38 or 52 minutes. It costs a reconnect each time, but the cost per drop was not measured.
  - **RPC stalls (measured):** the hub marked the node `UNRESPONSIVE` 14 times between 10-24 21:34 and 10-26 08:00 UTC, 34.0 min of state time in all. Each mark follows 5 minutes without a `get_info` answer. The longest flagged stretch was 7.0 min. Source: `node_state`.

    | Date | Episodes (start, UTC; flagged minutes) |
    |---|---|
    | 10-24 | 21:34 (3.5) |
    | 10-25 | 12:46 (3.5), 14:17 (0.5), 15:38 (0.5), 16:07 (2.0), 16:21 (1.5), 16:51 (5.0), 17:48 (2.0), 17:56 (7.0), 20:49 (3.5) |
    | 10-26 | 03:42 (2.0), 05:42 (0.5), 06:43 (1.0), 08:00 (1.5) |

    None happened from 10-25 20:49 to 10-26 03:42, or from 10-26 08:02 to the end of this note (10:24).
  - **What it coincides with (measured, two cases):**
    - The 10-24 21:34 episode came while it verified 219 transaction-carrying blocks (block heights 259,830–260,231, about 20 kB each) at about 1.2 s a block, against about 190 ms for an empty block.
    - On 10-26 between 07:38 and 08:38 UTC it logged 129 blocks with transactions (726,528–738,129, summed verification time 373 s, longest 4.4 s). That hour's sync rate was 11.8k, against 13.3–13.6k in the neighbouring hours, and the 08:00 episode fell inside it. In the next hour only 15 blocks carried transactions and the rate was back to 13.4k.
    - A 15-block stretch on 10-25 at about 02:56 (blocks 335,432–335,446) took 1.2–2.8 s per block, 25 s in all, with no outage.
    - The 10-26 06:43 episode does not fit as cleanly: the 5 min of silence began at about block 712,700, before the first of the following 56 transaction blocks (from 713,160).
  - **Hypothesis:** on this CPU, transaction verification occupies the only core long enough that `get_info` goes unanswered for minutes. The cause of the other episodes is not established.
  - **Memory (measured):** MemAvailable stays at 0.4–0.6 GB, with 85–94 MB of swap used. The node never ran out of memory and never restarted.
  - **Fee-estimate error (measured):** the log shows "Grace blocks invalid In 2021 fee scaling estimate." 40–53 times an hour, 2,539 lines since the sync began, each from the sidecar's `get_fee_estimate` poll. No other node logs it. **Hypothesis:** monerod raises it on every estimate while the node is far behind the chain tip; the cause is not established. It doesn't affect the sync.
  - **Caveat from the monitor (measured):** in the hourly checks at about 00:18, 01:18, 02:18 and 03:18 UTC on 10-25 the monitor's log scan copied all of this node's logs (about 430 MB) into a RAM disk. That pushed MemAvailable down to 135–159 MB and swapped out 1,436–26,185 pages in each of those half-hours. The scan was changed at the 04:18 check to stream the log instead. No unresponsive episode falls in those four hours. Before 00:18 the scan copied only the live file; the 21:18 check on 10-24 (copy at about 21:21) moved MemAvailable from 572 to 558 MB with no swap-out, and the 21:34 episode came 13 minutes after it. So the sync's rate and stalls up to about 04:18 on 10-25 were measured with some load from the monitor itself, and the later figures were not.
  - Sources: `info`, `node_state`, `log_warning`, `node_profile`, `host_sample`; the verification times and heights come from monerod's `bitmonero.log` ("Height: N" is block N−1; heights above are the true block heights).

- **`ref-b`'s monerod was killed by the kernel for lack of memory on 10-25 at 14:03:31 UTC (measured), while a local simulation workload ran on the same box.**
  - **The box:** `ref-b` has a Ryzen 9 5900XT (16 cores, 32 threads), 32 GB RAM and 4 GB swap. It runs the internet-facing, unpruned monerod (about 27 inbound and 3 outbound peers). Source: `node_profile`.
  - **The load:** four parallel network-simulation runs started on it between 09:07:20 and 09:11:20 UTC (about 58 running processes; load 57–59). Until 12:19 MemAvailable held at 24.9–25.8 GB.
  - **The kill:** the kernel logged "Out of memory: Killed process … (monerod)" with total-vm 26.5 GB and anon-rss 654 MB. In the 14:00 hour MemAvailable fell to 1.0 GB and SwapFree to 0; that hour swapped in 1,532 MB and out 2,116 MB. Source: kernel journal, `host_sample`.
  - **The restart:** systemd restarted monerod at 14:04:02 (31 s later; `NRestarts` 1), and the hub showed it `OK` again at 14:04:24. The only outage was the 53 s from the kill to the first good poll. The restarted process holds 0.43 GB of anonymous memory, no swap and 0.85–0.90 GB resident; free swap went from about 0 to 3,783 MiB.
  - **Before the kill (measured):** monerod held 4.85 GB of anonymous memory plus swap at 12:20 (0.78 GB anonymous, 4.08 GB in swap), unchanged since 10-20 16:30. The swap stayed full even when the box was idle, because the pages never needed to come back. So the kill is a result of the swap being full from earlier workloads, not of monerod growing.
  - **Hypothesis:** the kernel chose monerod because its anonymous memory plus swap (about 4.9 GB) was larger than any single simulation process, even though its resident size was only 654 MB. Not checked against the kernel's score.
  - **Implication (hypothesis):** a monitored node on a box that also runs memory-heavy simulations needs headroom in swap, a memory cap on the simulation, or a protected OOM score for monerod. A full node's pages do not come back from swap on their own.

- **A memory squeeze on `ref-a` made block verification take 6.8 s on 10-25 at 04:27 UTC (measured), and the kernel killed a simulation process, not monerod.**
  - **The sequence (`ref-a`):** four simulation runs had been going since 00:36–00:40 UTC. MemAvailable fell from 6.4 GB at 04:15 to 0.3 GB by 04:25, and SwapFree to 456 MiB. At 04:28:12 the kernel killed one `python` process of the simulation's user (anon-rss 12.46 GB). monerod survived and did not restart (`NRestarts` 0). Source: kernel journal, `host_sample`.
  - **The cost to monerod (measured):** four consecutive empty blocks took 6.77 s (block 3,117,099, logged 04:27:24, before the kill), 3.13 s (3,117,100, 04:28:28), 0.50 s (3,117,101, 04:30:57) and 92 ms (3,117,102, 04:35:58). The same blocks took 53–60 ms on `op-g6950-hdd` and `op-q6600-ssd`, and a normal `ref-a` block takes about 20 ms. In the log's time breakdown for each of these blocks nearly all the time sits in one field (6,766 of 6,769 ms for the first). Source: monerod's `bitmonero.log` ("Height: N" is block N−1).
  - **Hypothesis:** the delay was page-cache and swap thrash while memory was exhausted, since the first slow block came before the kill and the later ones eased after it. Not confirmed; the log field was not decoded.
  - **Later (measured):** from 11:04 on 10-25 `ref-a`'s MemAvailable cycled by about 6 GB (nine release events up to 12:13, then more up to 19:50) and fell to 4.7 GB at 19:45 and to about 2.7 GB at 01:30 on 10-26 with no kill. At 03:13 on 10-26 it jumped from 11.4 to 31.2 GB when the two largest simulation processes ended. These are `ref-a`'s memory figures only; the simulation itself was not examined.

## 2026-10-26/27

- **`op-n450-hdd` drops its only peer exactly 9,999 blocks after it last connected to it (measured: all 95 such drops from 10-24 04:51 to 10-27 10:17 UTC). A disconnect for any other reason restarts the count, and that explains all six outliers in the 2026-10-24/26 entry.**
  - **The data:** 159 "monerod is now disconnected from the network" warnings in `log_warning`. The height given for each is the last block the node added before it, from `block_timing` (true block heights). monerod's own "Synced N" line at a drop prints the chain length, one more.
  - **Every gap fits the rule (measured):**
    - 95 disconnects came exactly 9,999 blocks after the previous disconnect of any kind. These are the "We can download nothing from this peer" drops described on 2026-10-24/26.
    - 60 came 30–9,354 blocks after the previous one, each within 2 minutes of a failed `COMMAND_TIMED_SYNC` to `ref-b` (`LEVIN_ERROR_CONNECTION_TIMEDOUT`, or `…_DESTROYED`).
    - 3 had no such failure: 10-24 21:35:42, 10-25 12:44:00 and 10-25 14:03:40 UTC. The first two fall inside `UNRESPONSIVE` episodes.
    - No gap between disconnects exceeded 9,999 blocks.
  - **The count restarts at the reconnect (measured):** nine drops came after a disconnect of another kind. Each was exactly 9,999 blocks after that disconnect and 10,199–31,140 blocks after the previous 9,999 drop. Four of them are the long outliers of 2026-10-24/26 (10-25 13:28:55, 14:52:33, 17:36:33 and 18:43:04 UTC), which followed disconnects at 12:44:00, 14:03:40, 16:55:32 and 18:02:31. The other two outliers in that table (10-24 15:17 and 10-25 08:58) were exactly 9,999 blocks apart; the 10,599 and 9,499 there came from rounding to 30 s polls.
  - **Predicted in advance (measured):** from 10-27 05:38 UTC the hourly checks predicted the next drop from the last reconnect. Five drops in a row came at the predicted block:
    - 976,020;
    - 992,070, after a timed-sync disconnect at block 982,071, with no drop at 986,019 where the old count would have put it;
    - 1,002,069;
    - 1,012,068;
    - 1,027,345, after a timed-sync disconnect at block 1,017,346.
  - **Hypothesis:** each new connection gets one chain of up to 10,000 block ids from its peer (the first is a block the node already has). When it has fetched all of them from its only peer, monerod logs that it can download nothing more (`cryptonote_protocol_handler.inl:2605`) and drops the connection. That makes the drop a count from the connection, not from the previous drop. Not checked in the source.
  - **Cost:** each drop or timed-sync disconnect costs a reconnect; the time lost per reconnect was not measured.

- **Full-block stretches of pre-fork testnet history slow `op-n450-hdd` from about 14,000 to as few as 598 blocks an hour (measured).** In those stretches its RPC went silent for up to about 35 minutes, and its timed syncs with `ref-b` timed out. Stretches ran 10-26 15:30–18:50 UTC and 10-27 02:40–03:45 UTC.
  - **Which blocks:** the heavy blocks are heights 0.83–1.03 million, testnet history from long before the stressnet's fork at 3,102,800. So this is the cost of older transaction types on this CPU, not of FCMP++.
  - **Hour by hour (measured):** a transaction block is one whose cumulative weight exceeds its coinbase weight. Transaction weight is the cumulative weight minus the coinbase weight. "Timeouts" counts timed-sync failures with `LEVIN_ERROR_CONNECTION_TIMEDOUT`.

    | Hour (UTC) | Blocks | Tx blocks | Mean p/t, all blocks | Mean p/t, tx blocks | Mean tx weight per tx block | Timeouts |
    |---|---|---|---|---|---|---|
    | 10-26 13h | 13,999 | 262 | 209 ms | 1,090 ms | 19 kB | 0 |
    | 10-26 14h | 12,398 | 1,092 | 255 ms | 915 ms | 18 kB | 0 |
    | 10-26 15h | 7,993 | 1,019 | 403 ms | 1,851 ms | 44 kB | 4 |
    | 10-26 16h | 598 | 579 | 5,798 ms | 5,982 ms | 156 kB | 10 |
    | 10-26 17h | 1,052 | 959 | 3,197 ms | 3,488 ms | 88 kB | 11 |
    | 10-26 18h | 2,431 | 573 | 1,402 ms | 5,328 ms | 138 kB | 8 |
    | 10-26 19h | 13,596 | 655 | 224 ms | 847 ms | 16 kB | 0 |
    | 10-27 01h | 13,481 | 247 | 225 ms | 1,971 ms | 46 kB | 0 |
    | 10-27 02h | 8,600 | 421 | 378 ms | 4,010 ms | 103 kB | 2 |
    | 10-27 03h | 3,172 | 594 | 1,054 ms | 4,747 ms | 125 kB | 5 |
    | 10-27 04h | 10,494 | 338 | 290 ms | 3,109 ms | 73 kB | 3 |
    | 10-27 05h | 13,750 | 350 | 222 ms | 1,282 ms | 27 kB | 0 |

    In every other hour from 10-26 12:00 to 10-27 10:00 there were 0–1 timeouts. The 10-26 stretch covered about blocks 832,300–834,800; the 10-27 one about 946,000–947,200, with a 10-minute patch near 955,950 at 04:30.
  - **Where the time goes (measured):** in the slowest blocks nearly all of the time is in `t_checktx`, the check of transactions that weren't in the pool. During an initial sync none are. Block 1,017,261 (0.59 MB) took 47.7 s, the longest on this node so far, and 46.0 s of it was `t_checktx`. In the heavy hours of both stretches, transaction blocks took 38–39 ms per kB of transaction weight (ratio of the hourly means). Blocks near 1.017 and 1.028 million took 80–135 ms per kB, so size alone doesn't set the cost. **Hypothesis:** the transaction type (ring size, proof type) matters; not checked.
  - **RPC silence (measured):**
    - From 10-26 15:39 to 18:50 the hub marked the node `UNRESPONSIVE` 12 times, 139.5 min of state time in all. The longest was 29.5 min, from 15:50:47, which means about 34.5 min without a `get_info` answer.
    - From 10-27 02:53 to 04:38 there were 6 episodes, 23 min in all.
    - Between the two stretches there was one, 0.5 min at 10-26 19:42.
    - Source: `node_state`.
  - **Memory (measured):** MemAvailable bottomed at 0.44 GB (10-26 16h), against 0.48–0.57 GB in the light hours, with about 110–140 MB of swap in use. The node didn't restart. **Hypothesis:** the stretches are bound by its one CPU core, not by memory; I/O wait was not checked.
  - **Hypothesis on the timeouts:** while the only core verifies a block, the node doesn't read `ref-b`'s timed-sync reply in time. That is the same pattern as the low-end node on 2026-10-06 ("the replies arrive, but the low-end node doesn't read them in time"). Each timeout costs a disconnect and a reconnect, and restarts the 9,999-block count above.
  - **Progress (measured):** chain length 762,128 at 10-26 10:24 and 1,031,001 at 10-27 10:30 UTC. That is 268,873 blocks in 24.1 h, 11.2k an hour, against 14.1k an hour before 10-26 10:24. **Hypothesis:** the remaining 2.09 million blocks need at least 7.8 days at that rate. Later regions held more transactions for the other slow nodes (see 2026-10-22/24), so this is still a lower bound.
  - **Still going at the end of this note:** from 10-27 09:11 UTC, blocks near 1.017 and 1.028 million take 40–48 s each, with two more `UNRESPONSIVE` episodes (09:55, 4.0 min; 10:15, 2.5 min).
  - **Monitor load (caveat):** each hourly check sends one `get_info` and streams up to about 60 MB of the live log over SSH. At about 03:40 on 10-27 a one-off count read the rotated logs (about 500 MB) at idle I/O priority. The effect on the sync wasn't measured.
  - Sources: `block_timing` (`block_processing_ms`, `cumulative_weight`, `coinbase_weight`, `breakdown_raw`), `log_warning`, `node_state`, `info` (`height`), `host_sample` (`mem_avail_kb`, `swap_free_kb`).

---

## Monitor notes

These are about the monitor's behaviour, not the network's.

- **False `BEHIND` (fixed 2026-10-06):** node heights come from 30 s RPC polls pushed every 30 s, while the canonical height is polled every 2 s. Right after a pair of quick blocks, healthy nodes looked 2 behind. The hub now compares each node with the canonical height at the moment the node took its sample.
- **`UNRESPONSIVE` while busy:** when `get_info` takes more than 10 s because the node is verifying a block, the hub reports `UNRESPONSIVE`. That's a real symptom, slow RPC under load, but not a crash. The `info.rpc_response_time` and `rpc_error` tables show how slow it was. Since 2026-10-17 the hub keeps a node's state while it answers at least once every 5 minutes, and the dashboard shows the response times instead.
- **False `WRONG_CHAIN` while a node syncs (bug, seen 2026-10-08 03:29, fixed 10:47 UTC):**
  - **What monerod returns:** while a node isn't synchronized, it answers `get_last_block_header` with `status: "BUSY"` and a header of zeros.
  - **What the hub does:** it takes `major_version 0` above the fork height to mean an old-version chain, so it reports `WRONG_CHAIN`.
  - **How it was ruled out:** `get_info`'s `top_block_hash` and height were consistent with the reference chain.
  - **Fix:** the parser now turns any reply whose `status` isn't `OK` into an `rpc_error` row (`error` = `status BUSY`) and doesn't add it to `last_block_header`.
  - **Effect on data:** Parquet written before the fix holds 3,069 zero-header rows (`major_version` 0): 1,808 from exp-2c2g, 688 from op-g6950-hdd and 573 from ref-a, all from their syncs. `msnm reparse` removes them. Until then, filter `major_version > 0`.
- **Expected warnings that aren't faults:**
  - "Unable to send transaction(s), no available connections" on a node whose only peer is `ref-a`, because `monerod` doesn't relay a transaction back to its source;
  - "There were N blocks in the last 90 minutes…" when testnet's hashrate drops.
- **Fixed in sidecar 0.1.1:** a "Grace blocks not equal to expected 1000" warning on every RPC poll.
