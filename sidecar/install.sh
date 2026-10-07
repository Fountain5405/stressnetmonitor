#!/usr/bin/env bash
# install.sh: install, upgrade or remove the msnm sidecar on a stressnet node.
#
# As a systemd service (recommended; restarts by itself, survives reboots):
#   sudo ./sidecar/install.sh                       # asks for the hub URL and token
#   sudo ./sidecar/install.sh --hub URL --token-file FILE
#   git pull && sudo ./sidecar/install.sh           # upgrade, keeping your config
#   sudo ./sidecar/install.sh --uninstall
#
# Without systemd, for your user only (run as the user that runs monerod; no sudo):
#   ./sidecar/install.sh --no-systemd               # runs it in a detached `screen`
#   ./sidecar/install.sh --no-systemd --uninstall
#
# The systemd install creates:
#   /usr/local/bin/msnm-sidecar               the sidecar script
#   /etc/msnm-sidecar.conf                    hub URL and token (mode 600)
#   /etc/systemd/system/msnm-sidecar.service  runs it as the monerod user
#   /var/lib/msnm-sidecar                     queue for unsent data
# The --no-systemd install creates ~/.local/bin/msnm-sidecar,
# ~/.config/msnm-sidecar.conf, ~/.local/state/msnm-sidecar and an @reboot
# line in your crontab.
#
# It never changes or restarts monerod. If monerod lacks flags the sidecar
# needs, it tells you which ones to add.
set -euo pipefail

SERVICE=msnm-sidecar
SCREEN_NAME=msnm-sidecar
CRON_TAG="# msnm-sidecar"
DEFAULT_HUB_URL=""   # the public hub address, once there is one

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

usage() {
  cat <<'EOF'
Usage: sudo ./sidecar/install.sh [options]          (systemd service)
       ./sidecar/install.sh --no-systemd [options]  (your user, in `screen`)

  --hub URL             hub address given to you by the monitor operator
  --token-file FILE     read the token from FILE ('-' = stdin)
  --token TOKEN         the token itself (visible in `ps` and shell history;
                        prefer --token-file or the prompt)
  --pid PID             which monerod, if several --testnet ones are running
  --allow-remote-capture
                        let the hub request a log-tail bundle when your node
                        stalls (off by default)
  --no-start            install, but don't start it
  --force               install and start even if the checks fail
  --uninstall           stop it and remove everything installed
  --yes                 don't ask for confirmation (--uninstall)
  -h, --help            this help

Systemd install only:
  --user USER           run as USER (default: the user running monerod)
  --no-journal          don't let the sidecar read the system journal (it
                        uses it only for kernel OOM messages and core dumps)

--no-systemd install only:
  --no-cron             don't add an @reboot line to your crontab (then it
                        won't start again after a reboot)

Without --hub/--token, an existing config is kept; otherwise you are asked
for them.
EOF
}

say()  { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

HUB_ARG="" TOKEN_ARG="" TOKEN_FILE="" RUN_USER="" PID_ARG=""
REMOTE_CAPTURE="" JOURNAL=1 NO_START=0 FORCE=0 ACTION=install YES=0
MODE=systemd CRON=1

while (($#)); do
  case $1 in
    --hub) HUB_ARG=${2-}; shift 2 ;;
    --token) TOKEN_ARG=${2-}; shift 2 ;;
    --token-file) TOKEN_FILE=${2-}; shift 2 ;;
    --user) RUN_USER=${2-}; shift 2 ;;
    --pid) PID_ARG=${2-}; shift 2 ;;
    --allow-remote-capture) REMOTE_CAPTURE=1; shift ;;
    --no-journal) JOURNAL=0; shift ;;
    --no-systemd) MODE=user; shift ;;
    --no-cron) CRON=0; shift ;;
    --no-start) NO_START=1; shift ;;
    --force) FORCE=1; shift ;;
    --uninstall) ACTION=uninstall; shift ;;
    --yes|-y) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
done

if [[ $MODE == systemd ]]; then
  BIN=/usr/local/bin/msnm-sidecar
  CONF=/etc/msnm-sidecar.conf
  UNIT=/etc/systemd/system/msnm-sidecar.service
  STATE=/var/lib/msnm-sidecar
  ((EUID == 0)) || die "run as root: sudo $0  (or install for your user only with --no-systemd)"
  [[ -d /run/systemd/system ]] || die "systemd isn't running here. Use --no-systemd to run the sidecar in a screen session instead."
else
  [[ -z $RUN_USER ]] || die "--user is for the systemd install; with --no-systemd, run this script as that user"
  RUN_USER=$(id -un)
  BIN=$HOME/.local/bin/msnm-sidecar
  CONF=${XDG_CONFIG_HOME:-$HOME/.config}/msnm-sidecar.conf
  STATE=${XDG_STATE_HOME:-$HOME/.local/state}/msnm-sidecar
  if [[ -e /etc/systemd/system/msnm-sidecar.service ]]; then
    die "the sidecar is already installed as a systemd service; remove it first (sudo $0 --uninstall)"
  fi
fi

have_tty() { [[ -t 0 ]] || { : < /dev/tty; } 2>/dev/null; }

ask_yes() {
  ((YES)) && return 0
  have_tty || die "$1: no terminal to ask on; pass --yes"
  local a
  read -r -p "$1 [y/N] " a < /dev/tty
  [[ $a == [yY]* ]]
}

# --no-systemd: start, stop and the @reboot line ------------------------------

# Lowest CPU and IO priority, as the systemd unit does.
NICE=(nice -n 19)
command -v ionice >/dev/null && NICE+=(ionice -c3)

start_cmd() {   # the command line that starts the sidecar detached
  if command -v screen >/dev/null; then
    printf 'screen -dmS %s %s %q -c %q' "$SCREEN_NAME" "${NICE[*]}" "$BIN" "$CONF"
  else
    printf '%s %q -c %q >> %q 2>&1' "${NICE[*]}" "$BIN" "$CONF" "$STATE/sidecar.log"
  fi
}

# The sidecar's `#!/usr/bin/env bash` makes its process name "bash", so match
# the full command line "bash <BIN> ...". That excludes the SCREEN wrapper,
# whose command line starts with "SCREEN".
# shellcheck disable=SC2016  # the $ is a literal character for sed
sidecar_re() { printf '^(/[^ ]*/)?bash %s( |$)' "$(printf '%s' "$BIN" | sed 's/[][\.*^$()+?{}|]/\\&/g')"; }
user_running() { pgrep -u "$EUID" -f -- "$(sidecar_re)" >/dev/null; }

user_stop() {
  pkill -u "$EUID" -f -- "$(sidecar_re)" 2>/dev/null || return 0
  local i
  for i in $(seq 10); do user_running || return 0; sleep 1; done
  pkill -KILL -u "$EUID" -f -- "$(sidecar_re)" 2>/dev/null || true
}

user_start() {
  if command -v screen >/dev/null; then
    screen -dmS "$SCREEN_NAME" "${NICE[@]}" "$BIN" -c "$CONF"
  else
    setsid nohup "${NICE[@]}" "$BIN" -c "$CONF" >> "$STATE/sidecar.log" 2>&1 < /dev/null &
  fi
  sleep 3
  user_running
}

cron_set() {   # cron_set add|remove
  command -v crontab >/dev/null || { [[ $1 == add ]] && warn "crontab not found: the sidecar won't start again after a reboot"; return 0; }
  local cur line
  cur=$(crontab -l 2>/dev/null | grep -vF "$CRON_TAG" || true)
  if [[ $1 == add ]]; then
    # cron gives screen no terminal type; it needs one to start detached.
    line="@reboot sleep 60; TERM=xterm $(start_cmd) $CRON_TAG"
    if [[ -n $cur ]]; then printf '%s\n%s\n' "$cur" "$line"; else printf '%s\n' "$line"; fi | crontab -
  else
    if [[ -n $cur ]]; then printf '%s\n' "$cur" | crontab -; else crontab -r 2>/dev/null || true; fi
  fi
}

# ------------------------------------------------------------- uninstall ---

if [[ $ACTION == uninstall ]]; then
  if [[ $MODE == systemd ]]; then
    ask_yes "Stop msnm-sidecar and remove $BIN, $CONF, $UNIT and $STATE?" || die "aborted"
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$BIN" "$UNIT" "$CONF"
    rm -rf "$STATE"
    systemctl daemon-reload
  else
    ask_yes "Stop msnm-sidecar and remove $BIN, $CONF, $STATE and its crontab line?" || die "aborted"
    user_stop
    cron_set remove
    rm -f "$BIN" "$CONF"
    rm -rf "$STATE"
  fi
  say "Removed. Ask the monitor operator to revoke your token."
  exit 0
fi

# --------------------------------------------------------------- monerod ---

for f in msnm-sidecar.sh msnm-sidecar.conf.example msnm-sidecar.service; do
  [[ -r $HERE/$f ]] || die "$HERE/$f not found; run install.sh from a clone of the repository"
done
command -v curl >/dev/null || die "curl is required (apt install curl / dnf install curl)"
((BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4))) \
  || die "bash >= 4.4 required (found $BASH_VERSION)"

# monerod processes started with --testnet (the stressnet uses testnet's network).
PIDS=()
if [[ -n $PID_ARG ]]; then
  [[ $PID_ARG =~ ^[0-9]+$ && -r /proc/$PID_ARG/cmdline ]] || die "no process with pid $PID_ARG"
  PIDS=("$PID_ARG")
else
  for p in /proc/[0-9]*; do
    read -r comm < "$p/comm" 2>/dev/null || continue
    [[ $comm == monerod ]] || continue
    mapfile -d '' args < "$p/cmdline" 2>/dev/null || continue
    for a in "${args[@]}"; do [[ $a == --testnet ]] && { PIDS+=("${p#/proc/}"); break; }; done
  done
fi

proc_user() {
  local k v uid=""
  while read -r k v _; do [[ $k == Uid: ]] && { uid=$v; break; }; done < "/proc/$1/status"
  getent passwd "$uid" | cut -d: -f1
}

MPID=""
case ${#PIDS[@]} in
  0)
    [[ -n $RUN_USER ]] || die "no monerod --testnet process is running. Start your stressnet node first, or pass --user USER."
    warn "monerod is not running, so its flags can't be checked" ;;
  1)
    MPID=${PIDS[0]} ;;
  *)
    for p in "${PIDS[@]}"; do note "pid $p (user $(proc_user "$p"))"; done
    die "several monerod --testnet processes are running; choose one with --pid" ;;
esac

if [[ -n $MPID ]]; then
  MUSER=$(proc_user "$MPID")
  if [[ $MODE == user && $MUSER != "$RUN_USER" ]]; then
    die "monerod runs as '$MUSER', but you are '$RUN_USER'. Run this as '$MUSER' (e.g. sudo -u $MUSER -H $0 --no-systemd), or use the systemd install (sudo $0)."
  fi
  [[ -z $RUN_USER ]] && RUN_USER=$MUSER
  [[ $RUN_USER == "$MUSER" ]] || warn "monerod runs as '$MUSER' but the sidecar will run as '$RUN_USER'; it may not be able to read monerod's log"
fi
getent passwd "$RUN_USER" >/dev/null || die "no such user: $RUN_USER"
RUN_GROUP=$(id -gn "$RUN_USER")
[[ $RUN_USER == root ]] && warn "the sidecar will run as root, because monerod does. Running monerod as an unprivileged user is safer."

# monerod's options come from its command line and, failing that, its config
# file (--config-file, or bitmonero.conf in the data dir).
MARGS=() MCONF=""
marg() {   # marg NAME -> prints the value of --NAME; status 1 if absent
  local name=$1 i k v
  for ((i = 0; i < ${#MARGS[@]}; i++)); do
    case ${MARGS[i]} in
      "--$name") printf '%s' "${MARGS[i+1]:-}"; return 0 ;;
      "--$name="*) printf '%s' "${MARGS[i]#*=}"; return 0 ;;
    esac
  done
  if [[ -n $MCONF && -r $MCONF ]]; then
    while IFS='=' read -r k v; do
      k=${k//[[:space:]]/}
      [[ $k == "$name" ]] && { v=${v#"${v%%[![:space:]]*}"}; printf '%s' "${v%"${v##*[![:space:]]}"}"; return 0; }
    done < "$MCONF"
  fi
  return 1
}

MISSING=()
if [[ -n $MPID ]]; then
  mapfile -d '' MARGS < "/proc/$MPID/cmdline"
  MARGS=("${MARGS[@]:1}")
  if MCONF=$(marg config-file); then :; else
    home=$(getent passwd "$MUSER" | cut -d: -f6)
    ddir=$(marg data-dir) || ddir="$home/.bitmonero"
    MCONF="$ddir/testnet/bitmonero.conf"
  fi

  sts=$(marg show-time-stats) || sts=0
  [[ $sts == 0 || -z $sts ]] && MISSING+=("--show-time-stats 1")

  # Level 1+ includes blockchain and txpool INFO; otherwise both categories
  # must be listed at INFO or more verbose.
  ll=$(marg log-level) || ll=0
  if ! [[ $ll =~ ^[1-4]($|,) ]]; then
    [[ $ll =~ (^|,)(blockchain|\*):(INFO|DEBUG|TRACE) && $ll =~ (^|,)(txpool|\*):(INFO|DEBUG|TRACE) ]] \
      || MISSING+=("--log-level 0,blockchain:INFO,txpool:INFO")
  fi
  # The sidecar needs monerod's full RPC on localhost.
  for a in "${MARGS[@]}"; do
    [[ $a == --restricted-rpc ]] && warn "monerod runs with --restricted-rpc; the sidecar needs the unrestricted RPC port (bind it to 127.0.0.1 and use --rpc-restricted-bind-port for public access)"
  done
fi

# ------------------------------------------------------------ hub & token ---

# Read KEY="value" from the existing config without sourcing it: in the
# systemd install the file is owned by the sidecar user, so sourcing it as
# root would hand that user root.
conf_get() {
  local line
  [[ -r $CONF ]] || return 1
  line=$(grep -E "^$1=" "$CONF" | tail -n 1) || return 1
  line=${line#*=}; line=${line#\"}; line=${line%\"}
  printf '%s' "$line"
}

HUB=$HUB_ARG
[[ -z $HUB ]] && HUB=$(conf_get HUB_URL || true)
[[ $HUB == https://msnm.example.org ]] && HUB=""
[[ -z $HUB ]] && HUB=$DEFAULT_HUB_URL
if [[ -z $HUB ]]; then
  have_tty || die "pass --hub URL (no terminal to ask on)"
  read -r -p "Hub URL (from the monitor operator): " HUB < /dev/tty
fi
HUB=${HUB%/}
[[ $HUB =~ ^https?://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$ ]] || die "not a valid hub URL: '$HUB'"
[[ $HUB == http://* ]] && warn "$HUB is plain http: your token and data travel unencrypted. Use https unless the hub is on your own LAN."

TOKEN=$TOKEN_ARG
if [[ -n $TOKEN_FILE ]]; then
  if [[ $TOKEN_FILE == - ]]; then read -r TOKEN || true
  else [[ -r $TOKEN_FILE ]] || die "can't read $TOKEN_FILE"; read -r TOKEN < "$TOKEN_FILE" || true
  fi
fi
[[ -z $TOKEN ]] && TOKEN=$(conf_get TOKEN || true)
if [[ -z $TOKEN ]]; then
  have_tty || die "pass --token-file FILE (no terminal to ask on)"
  read -r -s -p "Token (from the monitor operator, not shown): " TOKEN < /dev/tty; echo
fi
TOKEN=${TOKEN//[[:space:]]/}
[[ $TOKEN =~ ^[A-Za-z0-9_-]{20,128}$ ]] || die "that doesn't look like a monitor token (expected 43 characters of A-Z a-z 0-9 _ -)"

# ------------------------------------------------------------------ files ---

old_ver=""
[[ -r $BIN ]] && old_ver=$(grep -m1 -oE '^SIDECAR_VERSION="[^"]+"' "$BIN" | cut -d'"' -f2 || true)
new_ver=$(grep -m1 -oE '^SIDECAR_VERSION="[^"]+"' "$HERE/msnm-sidecar.sh" | cut -d'"' -f2)

mkdir -p "$(dirname "$BIN")" "$(dirname "$CONF")"
# Copy rather than run from the clone, so a `git pull` doesn't change the
# script under a running sidecar; re-run this installer to upgrade.
install -m 0755 "$HERE/msnm-sidecar.sh" "$BIN"
if [[ -n $old_ver && $old_ver != "$new_ver" ]]; then say "Upgraded $BIN: $old_ver -> $new_ver"
else say "Installed $BIN ($new_ver)"; fi

# Config: keep everything in an existing file, set HUB_URL and TOKEN (and
# ALLOW_REMOTE_CAPTURE if asked). It is written to a temporary file and only
# replaces the real one once --check passes, so a mistyped token can't break
# a working install.
src=$HERE/msnm-sidecar.conf.example
[[ -r $CONF ]] && src=$CONF
tmp=$(mktemp "$(dirname "$CONF")/.msnm-sidecar.conf.XXXXXX")
trap 'rm -f "$tmp"' EXIT
awk -v hub="$HUB" -v tok="$TOKEN" -v rc="$REMOTE_CAPTURE" '
  /^HUB_URL=/ { if (!h) print "HUB_URL=\"" hub "\""; h = 1; next }
  /^TOKEN=/   { if (!t) print "TOKEN=\"" tok "\""; t = 1; next }
  rc != "" && /^#?ALLOW_REMOTE_CAPTURE=/ { if (!r) print "ALLOW_REMOTE_CAPTURE=" rc; r = 1; next }
  { print }
  END {
    if (!h) print "HUB_URL=\"" hub "\""
    if (!t) print "TOKEN=\"" tok "\""
    if (rc != "" && !r) print "ALLOW_REMOTE_CAPTURE=" rc
  }' "$src" > "$tmp"
[[ $MODE == systemd ]] && chown "$RUN_USER:$RUN_GROUP" "$tmp"
chmod 600 "$tmp"
commit_conf() {
  mv -f "$tmp" "$CONF"
  say "Wrote $CONF (readable only by $RUN_USER)"
}

if [[ $MODE == systemd ]]; then
  install -d -m 0750 -o "$RUN_USER" -g "$RUN_GROUP" "$STATE"

  # Unit: the repository's template with User= set. The journal group lets the
  # sidecar read kernel OOM messages and core dumps for crash reports; it
  # applies to this service only.
  groups_line=""
  ((JOURNAL)) && getent group systemd-journal >/dev/null && groups_line="SupplementaryGroups=systemd-journal"
  tmpu=$(mktemp /etc/systemd/system/.msnm-sidecar.service.XXXXXX)
  trap 'rm -f "$tmp" "$tmpu"' EXIT
  awk -v user="$RUN_USER" -v groups="$groups_line" '
    /^User=/ { print "User=" user; next }
    /^#?SupplementaryGroups=/ { if (groups != "") print groups; else print "#SupplementaryGroups=systemd-journal"; next }
    { print }' "$HERE/msnm-sidecar.service" > "$tmpu"
  chmod 644 "$tmpu"
  mv -f "$tmpu" "$UNIT"
  systemctl daemon-reload
  say "Installed $UNIT (User=$RUN_USER${groups_line:+, journal access for crash reports})"
else
  mkdir -p "$STATE"; chmod 700 "$STATE"
fi

# ---------------------------------------------------------------- checks ---

say "Checking (msnm-sidecar --check as $RUN_USER):"
runas() {
  if [[ $MODE == user ]]; then "$@"
  elif command -v runuser >/dev/null; then runuser -u "$RUN_USER" -- "$@"
  else sudo -u "$RUN_USER" -- "$@"; fi
}
CHECK_OK=0   # pipefail: the pipeline fails when --check does
runas env MSNM_CONFIG="$tmp" SPOOL_DIR="$STATE" "$BIN" --check 2>&1 | sed 's/^/    /' && CHECK_OK=1

if ((${#MISSING[@]})); then
  echo
  warn "monerod is missing flags the sidecar needs for full data:"
  for m in "${MISSING[@]}"; do note "$m"; done
  note "Without them the sidecar still runs, but per-block timings (the most"
  note "important measurement) and transaction arrivals are not collected."
  unit=""
  [[ -r /proc/$MPID/cgroup ]] && unit=$(grep -oE '[^/]+\.service$' "/proc/$MPID/cgroup" | head -n 1 || true)
  if [[ -n $unit && $unit != user@* ]]; then
    note "monerod runs as the systemd unit $unit. Add them to its ExecStart:"
    note "  sudo systemctl edit --full $unit && sudo systemctl restart $unit"
  elif [[ -r $MCONF ]]; then
    note "Or add these lines to $MCONF and restart monerod:"
    note "  show-time-stats=1"
    note "  log-level=0,blockchain:INFO,txpool:INFO"
  else
    note "Add them to monerod's command line and restart it."
  fi
  note "The sidecar notices a restarted monerod by itself."
fi

if [[ $MODE == user && $RUN_USER != root ]] && ! id -nG | grep -qwE 'systemd-journal|adm'; then
  echo
  note "Note: you aren't in the systemd-journal or adm group, so crash reports"
  note "won't include kernel out-of-memory messages."
fi

# ----------------------------------------------------------------- start ---

# Wait for proof that data arrives: the sidecar records the hub's first
# accepted batch in its status file (sidecar >= 0.1.4).
wait_connected() {   # wait_connected <epoch the sidecar was started after>
  local since=$1 i k v started="" first="" err="" node=""
  printf '%s' "Waiting for the hub to accept the first batch (up to 2 min)"
  for i in $(seq 1 24); do
    sleep 5; printf '.'
    started="" first="" err="" node=""
    if [[ -r $STATE/status ]]; then
      while IFS=$'\t' read -r k v; do
        case $k in started) started=$v ;; first_ok) first=$v ;; last_error) err=$v ;; hub_node) node=$v ;; esac
      done < "$STATE/status"
    fi
    [[ -n $first && ${started:-0} -ge $since ]] && break
  done
  echo
  if [[ -n $first && ${started:-0} -ge $since ]]; then
    say "Connected: the hub accepted this node's first batch at $(date -u -d "@$first" '+%H:%M:%S UTC')${node:+ (node $node)}. Data is flowing."
    return 0
  fi
  warn "Not confirmed yet${err:+: $err}."
  note "The sidecar keeps retrying every 30 s and keeps the data until the hub takes it."
  return 1
}

echo
if ! ((CHECK_OK || FORCE)); then
  say "Some checks failed (see above). $CONF was not changed and the sidecar was left as it was."
  note "Fix the problem and run this script again, or pass --force to install"
  note "and start anyway (if only the hub is unreachable, data is queued and"
  note "sent once it is reachable)."
  exit 1
fi
commit_conf

if [[ $MODE == systemd ]]; then
  if ((NO_START)); then
    say "Not started (--no-start). Start it with: sudo systemctl enable --now $SERVICE"
  else
    systemctl enable --quiet "$SERVICE"
    since=$(date +%s)
    systemctl restart "$SERVICE"
    sleep 3
    if systemctl is-active --quiet "$SERVICE"; then
      say "$SERVICE is running."
      wait_connected "$since" || true
    else
      journalctl -u "$SERVICE" -n 20 --no-pager >&2 || true
      die "$SERVICE failed to start (log above)"
    fi
  fi
  cat <<EOF

    Is it working?             sudo $BIN --status
    See exactly what is sent:  sudo -u $RUN_USER $BIN --once | less
    Follow the sidecar's log:  journalctl -u $SERVICE -f
    Upgrade later:             git pull && sudo $HERE/install.sh
    Remove:                    sudo $HERE/install.sh --uninstall
EOF
else
  if ((CRON)); then cron_set add; say "Added an @reboot line to your crontab, so it starts again after a reboot."
  else cron_set remove; fi
  if ((NO_START)); then
    say "Not started (--no-start). Start it with: $(start_cmd)"
  else
    user_stop   # an older copy, when upgrading
    since=$(date +%s)
    user_start || die "the sidecar didn't stay running; run it in the foreground to see why: $BIN -c $CONF"
    say "msnm-sidecar is running (lowest CPU and IO priority)."
    wait_connected "$since" || true
  fi
  if command -v screen >/dev/null; then where="screen -r $SCREEN_NAME   (detach again: Ctrl-a d)"
  else where="tail -f $STATE/sidecar.log"; fi
  cat <<EOF

    Is it working?             $BIN -c $CONF --status
    See exactly what is sent:  $BIN -c $CONF --once | less
    Follow the sidecar's log:  $where
    Upgrade later:             git pull && $HERE/install.sh --no-systemd
    Remove:                    $HERE/install.sh --no-systemd --uninstall
EOF
fi
