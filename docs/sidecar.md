# Running the sidecar

The sidecar is one bash script, [`sidecar/msnm-sidecar.sh`](../sidecar/msnm-sidecar.sh), that runs next to your stressnet `monerod` and sends measurements to the monitor. Please read it before you run it; it is meant to be read.

## What it sends

Every 30 s (by default) it uploads one compressed batch containing:

- **`monerod` RPC responses**, queried on your node's local RPC port (`127.0.0.1`): `get_info`, `get_last_block_header`, `get_fee_estimate`, `get_connections`, `get_bans` and `get_transaction_pool_stats`. Your RPC port does **not** need to be reachable from the internet.
- **Host counters from `/proc`**, every 5 s: CPU time, memory, swap, load, paging, disk I/O for the device that holds the blockchain, total network bytes, and pressure stall information.
- **`monerod` process counters**: CPU time, memory, page faults and I/O.
- **Selected `monerod` log lines**: block-added messages with their timing breakdown, a 1% sample of "Transaction added to pool" lines (chosen by txid), reorg and alternative-block messages, and WARNING/ERROR lines (at most 60 per minute).
- **A hardware profile**, once per hour: CPU model and flags, core count, RAM, disk type, filesystem, OS and kernel, `monerod` version and command line. The `--rpc-login` and proxy arguments are redacted, and IP addresses are removed.
- **When `monerod` exits unexpectedly**, a crash bundle: the last 5000 log lines (IP addresses removed), kernel OOM/segfault messages, and a core dump backtrace if `systemd-coredump` and `gdb` are installed.

What it does **not** do: it opens no ports, runs no commands sent by the hub, and sends nothing besides the above. The one optional exception: if you set `ALLOW_REMOTE_CAPTURE=1`, the hub may ask for a log-tail bundle when it sees your node stalled.

Your node appears in public dashboards only under the pseudonym the monitor operator gives it. Your IP address is never published.

## Requirements

- Linux, bash ≥ 4.4 (bash 5 gives microsecond timestamps), `curl`, `tail`, `grep`, and `gzip` (optional)
- Run it as the **same user as `monerod`** (or root), so it can read `monerod`'s log and `/proc/<pid>/io`
- `monerod` flags for full data:

  ```
  monerod --testnet --show-time-stats 1 --log-level 0,blockchain:INFO,txpool:INFO ...
  ```

  `--show-time-stats 1` adds the per-block timing breakdown. `blockchain:INFO` logs added blocks. `txpool:INFO` logs one line per transaction entering the pool (only 1% of them are sent). Leave the RPC bound to `127.0.0.1` (the default).

## Install

1. **Get a token.** Ask the monitor operator (in the stressnet Matrix room) for a token and the hub URL. Each node gets its own token, so only registered nodes can send data.
2. **Start `monerod` with the flags above** and let it sync.
3. **Run the installer** from a clone of this repository, on the machine running `monerod`:

   ```bash
   git clone https://github.com/Fountain5405/stressnetmonitor.git
   cd stressnetmonitor
   sudo ./sidecar/install.sh
   ```

   It asks for the hub URL and token (the token isn't echoed). It then:
   - finds your `monerod --testnet` process and the user it runs as;
   - installs the sidecar, its config (`/etc/msnm-sidecar.conf`, mode 600) and a systemd service that runs as that user;
   - runs `msnm-sidecar --check`, and starts the service only if everything passes;
   - tells you if `monerod` is missing flags the sidecar needs. It never changes or restarts `monerod` itself.

   If the check fails (for example, a mistyped token), nothing is changed. Fix the problem and run it again.

To upgrade later: `git pull && sudo ./sidecar/install.sh`. Your config is kept.
To remove everything: `sudo ./sidecar/install.sh --uninstall`.
`sudo ./sidecar/install.sh --help` lists the options (`--hub`, `--token-file`, `--user`, `--pid`, `--allow-remote-capture`, …).

### Manual install

If you'd rather not run the installer, these are the steps it performs:

```bash
sudo install -m 755 sidecar/msnm-sidecar.sh /usr/local/bin/msnm-sidecar
sudo install -m 600 -o <monerod-user> sidecar/msnm-sidecar.conf.example /etc/msnm-sidecar.conf
sudoedit /etc/msnm-sidecar.conf          # set HUB_URL and TOKEN
sudo -u <monerod-user> MSNM_CONFIG=/etc/msnm-sidecar.conf msnm-sidecar --check
```

`--check` finds `monerod`, tests its RPC and the hub connection, and reports what's missing. `--once` prints one batch to the screen without sending it, so you can see exactly what would be sent (`sudo -u <monerod-user> msnm-sidecar --once | less`).

Then run it as a service:

```bash
sudo cp sidecar/msnm-sidecar.service /etc/systemd/system/
sudo systemctl edit --full msnm-sidecar     # set User= to the monerod user
sudo systemctl enable --now msnm-sidecar
journalctl -u msnm-sidecar -f
```

### Optional: backtraces for crash reports

Release `monerod` binaries include debug symbols, so a core dump gives a readable backtrace.

```bash
sudo apt install systemd-coredump gdb     # Debian/Ubuntu
```

Core dumps of a large `monerod` can be several GB. `systemd-coredump` limits and rotates them (`/etc/systemd/coredump.conf`).

## Cost

In steady state, the sidecar uses only bash built-ins. It starts one `curl` per RPC poll, and one `gzip`, `du` and `curl` per upload. The systemd unit runs it at the lowest CPU and IO priority.

Measured on a Ryzen 9900X VM, with intervals 2.5–7.5× more frequent than the defaults: 0.65% of one core (child processes included), 5 MB RSS, 334 B/s of upload. At the default intervals, expect about a third of that CPU on a modern machine and a few times more on old CPUs. Upload grows with peer count, because `get_connections` returns one entry per peer: roughly 0.1–1 KB/s.

If the hub is unreachable, batches are queued in the spool directory (at most `SPOOL_MAX_MB`, oldest dropped first) and sent later.

## Stopping and removing

`sudo ./sidecar/install.sh --uninstall` does this:

```bash
sudo systemctl disable --now msnm-sidecar
sudo rm /usr/local/bin/msnm-sidecar /etc/msnm-sidecar.conf /etc/systemd/system/msnm-sidecar.service
sudo rm -rf /var/lib/msnm-sidecar
```

Ask the monitor operator to revoke your token.
