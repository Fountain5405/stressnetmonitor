# CLAUDE.md

This is a public repo. Never commit LAN IPs, hostnames, tokens, personal paths, or emails. Live config (`hub.yaml`) and `.claude/` are gitignored. Commits use the GitHub noreply identity that is already set in the local git config.

## Invariants
- **The sidecar (`sidecar/msnm-sidecar.sh`) stays light.** Steady-state sampling uses bash builtins only. External processes run once per RPC poll (curl) and once per push. Don't add jq or JSON parsing: the sidecar ships raw RPC JSON and the hub parses it. Run `.venv/bin/shellcheck -s bash -S warning` before committing.
- **Bash trap:** `exec {fd}<> x 2>/dev/null` makes the `2>/dev/null` permanent for the whole script. Wrap it in `{ exec ...; } 2>/dev/null`.
- **Parquet tables are a pure function of the raw batches.** `msnm reparse` must be able to rebuild them, so parsing must not depend on hub-time state. Hub-observed tables (`ref_block`, `zmq_*`, `node_state`) are the only exceptions.
- **Parquet types stay R-friendly**: int32 heights, float64 counters, UTC timestamps, no uint64. Tables and columns that overlap Rucknium/monerod-monitor keep its names.
- **monerod facts the code depends on:**
  - `get_info.height` is the chain length; the top block is `height - 1`.
  - The `--show-time-stats` line prints `Height: N` for block N-1.
  - `--testnet` always appends `/testnet` to the data dir.
  - Logs are UTC.

## Commands
- Tests: `cd hub && ../.venv/bin/python -m pytest -q`
- Hub: `.venv/bin/msnm -c hub.yaml serve | node add/list/revoke | reparse | verify-ledger | compact`
