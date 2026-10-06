# Running the sidecar

The sidecar is one bash script, [`sidecar/msnm-sidecar.sh`](../sidecar/msnm-sidecar.sh). It runs next to your stressnet `monerod` and sends measurements to the monitor. It opens no ports and runs at the lowest CPU and disk priority. Please read it before you run it; it's meant to be read.

## Quick install

You need Linux, a stressnet node (`monerod` v0.19.0.0-beta.3.0 with `--testnet`) and about five minutes.

1. **Get a token and the hub URL.** Ask the monitor operator in the stressnet Matrix room. Each node gets its own token; it's what lets the monitor trust your data, so keep it private.
2. **Restart `monerod` with three extra flags**, so it logs the measurements the sidecar collects. Details are in [`monerod` setup](#monerod-setup) below.

   ```
   --show-time-stats 1 --log-level 0,blockchain:INFO,txpool:INFO
   ```

3. **Run the installer** on the machine running `monerod`:

   ```bash
   git clone https://github.com/Fountain5405/stressnetmonitor.git
   cd stressnetmonitor
   sudo ./sidecar/install.sh --hub <hub URL>
   ```

   It asks for your token (input is hidden). Then it:
   - finds your `monerod --testnet` process and the user it runs as;
   - installs the sidecar as a systemd service running as that user;
   - checks that it can reach `monerod` and the hub, and starts only if everything passes;
   - tells you if `monerod` is missing any of the flags above. It never changes or restarts `monerod` itself.

   If a check fails, for example because of a mistyped token, nothing is changed. Fix the problem and run it again.

**Without systemd or sudo:** run the installer as the user that runs `monerod`, with `--no-systemd`. See [Running without systemd](#running-without-systemd).

**Upgrade:** `git pull && sudo ./sidecar/install.sh`. Your settings are kept.
**Remove:** `sudo ./sidecar/install.sh --uninstall`, then ask the operator to revoke your token.
`./sidecar/install.sh --help` lists every option.

To see exactly what the sidecar sends, without sending anything: `sudo -u <monerod-user> msnm-sidecar --once | less`.

## `monerod` setup

### Flags the sidecar needs

| Flag | What it adds |
|---|---|
| `--show-time-stats 1` | A timing breakdown for every block your node adds. This is the most important measurement. |
| `--log-level 0,blockchain:INFO,txpool:INFO` | One log line per added block and one per transaction entering your pool. Only about 1% of the transaction lines are sent, chosen by txid. |

Keep the RPC port bound to `127.0.0.1` (the default). The sidecar needs the full, unrestricted RPC, but only locally. If you also want to serve a public RPC, use `--rpc-restricted-bind-port` for that.

`monerod` can also take these settings from its config file (`bitmonero.conf`):

```
show-time-stats=1
log-level=0,blockchain:INFO,txpool:INFO
```

### Log size

With `txpool:INFO`, `monerod` logs every transaction it receives:
- At the stressnet's ~10 transactions/s, the log grows by about **25 MB an hour, roughly 600 MB a day**.
- During the initial sync it grows much faster, about 100 MB every few minutes, because every block is logged.

`monerod` rotates its log at 100 MB and keeps 10 old files by default, so logs never take more than about 1.1 GB. On a small disk, keep fewer with `--max-log-files 3`. The sidecar follows the live log as it's written, so it never needs the old files.

### Seed nodes (v0.19.0.0-beta.3.0)

The seed nodes built into v0.19.0.0-beta.3.0 no longer work, so a fresh node may never find peers. Until v0.19.0.0-beta.3.1 is released with new ones, add:

```
--seed-node 185.141.216.177:28180 --seed-node 208.123.187.228:28080 --seed-node 185.141.216.147:28080 --seed-node 209.141.41.69:28080
```

Source: [seraphis-migration/monero#497](https://github.com/seraphis-migration/monero/pull/497).

## Running without systemd

If your system doesn't use systemd, or you'd rather not install a service, run the installer **as the user that runs `monerod`**, without sudo:

```bash
./sidecar/install.sh --no-systemd --hub <hub URL>
```

It installs everything in your home directory:

| Path | What |
|---|---|
| `~/.local/bin/msnm-sidecar` | the sidecar |
| `~/.config/msnm-sidecar.conf` | its settings (mode 600) |
| `~/.local/state/msnm-sidecar` | data waiting to be sent |

It then:
- starts the sidecar detached in `screen`, or in the background if `screen` isn't installed, at the lowest CPU and disk priority;
- adds an `@reboot` line to your crontab so it starts again after a reboot. Use `--no-cron` to skip that.

- See it running: `screen -r msnm-sidecar` (detach again with Ctrl-a d).
- Upgrade: `git pull && ./sidecar/install.sh --no-systemd`.
- Remove: `./sidecar/install.sh --no-systemd --uninstall`. This also removes the crontab line.

Compared with the systemd service, two things are missing:
- If the sidecar itself crashes, nothing restarts it until the next reboot. The service restarts within 10 s.
- Crash reports only include the kernel's out-of-memory messages if your user can read the system journal (the `systemd-journal` or `adm` group).

By hand, the same thing is:

```bash
cp sidecar/msnm-sidecar.conf.example ~/.config/msnm-sidecar.conf
chmod 600 ~/.config/msnm-sidecar.conf               # then set HUB_URL and TOKEN in it
./sidecar/msnm-sidecar.sh -c ~/.config/msnm-sidecar.conf --check
screen -dmS msnm-sidecar nice -n 19 ionice -c3 ./sidecar/msnm-sidecar.sh -c ~/.config/msnm-sidecar.conf
```

## What it sends

Every 30 s (by default) it uploads one compressed batch containing:

- **`monerod` RPC responses**, queried on your node's local RPC port (`127.0.0.1`): `get_info`, `get_last_block_header`, `get_fee_estimate`, `get_connections`, `get_bans` and `get_transaction_pool_stats`. Your RPC port does **not** need to be reachable from the internet.
- **Host counters from `/proc`**, every 5 s: CPU time, memory, swap, load, paging, disk I/O for the device that holds the blockchain, total network bytes, and pressure stall information.
- **`monerod` process counters**: CPU time, memory, page faults and I/O.
- **Selected `monerod` log lines**:
  - block-added messages with their timing breakdown;
  - a 1% sample of "Transaction added to pool" lines, chosen by txid;
  - reorg and alternative-block messages;
  - WARNING/ERROR lines, at most 60 per minute.
- **A hardware profile**, once per hour: CPU model and flags, core count, RAM, disk type, filesystem, OS and kernel, `monerod` version and command line. The `--rpc-login` and proxy arguments are redacted, and IP addresses are removed.
- **When `monerod` exits unexpectedly**, a crash bundle:
  - the last 5000 log lines, with IP addresses removed;
  - kernel OOM and segfault messages;
  - a core dump backtrace, if `systemd-coredump` and `gdb` are installed.

What it does **not** do:
- It opens no ports.
- It runs no commands sent by the hub.
- It sends nothing besides the above.

The one optional exception: with `--allow-remote-capture` (the `ALLOW_REMOTE_CAPTURE=1` setting), the hub may ask for a log-tail bundle when it sees your node stalled.

Your node appears in published results only under the pseudonym the monitor operator gives it. Your IP address is never published.

## Optional: backtraces for crash reports

Release `monerod` binaries include debug symbols, so a core dump gives a readable backtrace:

```bash
sudo apt install systemd-coredump gdb     # Debian/Ubuntu
```

Core dumps of a large `monerod` can be several GB. `systemd-coredump` limits and rotates them (`/etc/systemd/coredump.conf`).

## Cost

In steady state, the sidecar uses only bash built-ins. It starts one `curl` per RPC poll, and one `gzip`, `du` and `curl` per upload. It runs at the lowest CPU and disk priority.

- **Measured:** on a Ryzen 9900X VM, with intervals 2.5–7.5× more frequent than the defaults, it used 0.65% of one core (child processes included), 5 MB RSS and 334 B/s of upload.
- **Expected at the default intervals:** about a third of that CPU on a modern machine, and a few times more on old CPUs.
- **Upload** grows with peer count, because `get_connections` returns one entry per peer: roughly 0.1–1 KB/s.

If the hub is unreachable, batches are queued (at most `SPOOL_MAX_MB`, 200 MB by default, oldest dropped first) and sent later.

## Manual install (systemd)

If you'd rather not run the installer, these are the steps it performs:

```bash
sudo install -m 755 sidecar/msnm-sidecar.sh /usr/local/bin/msnm-sidecar
sudo install -m 600 -o <monerod-user> sidecar/msnm-sidecar.conf.example /etc/msnm-sidecar.conf
sudoedit /etc/msnm-sidecar.conf          # set HUB_URL and TOKEN
sudo -u <monerod-user> MSNM_CONFIG=/etc/msnm-sidecar.conf msnm-sidecar --check
sudo cp sidecar/msnm-sidecar.service /etc/systemd/system/
sudo systemctl edit --full msnm-sidecar  # set User= to the monerod user
sudo systemctl enable --now msnm-sidecar
journalctl -u msnm-sidecar -f
```

To remove it by hand:

```bash
sudo systemctl disable --now msnm-sidecar
sudo rm /usr/local/bin/msnm-sidecar /etc/msnm-sidecar.conf /etc/systemd/system/msnm-sidecar.service
sudo rm -rf /var/lib/msnm-sidecar
```
