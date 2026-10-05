#!/usr/bin/env bash
# msnm-sidecar: StressNet Monitor sidecar.
#
# Runs next to monerod and pushes raw measurements to the monitor hub:
#   - monerod RPC responses (queried on the local, unrestricted RPC port)
#   - host and monerod process counters from /proc
#   - selected monerod log lines (per-block timings, sampled tx arrivals,
#     reorgs, warnings/errors)
#   - crash bundles (log tail, OOM messages, core dump backtrace)
#
# It opens no ports and only makes outbound HTTPS requests to the hub.
# It does not parse JSON; the hub does. Steady-state sampling uses bash
# builtins only; external programs run once per RPC poll (curl) and once
# per push (gzip, curl, du).
#
# Usage: msnm-sidecar.sh [-c CONFIG] [--check | --once]
# See docs/sidecar.md.

set -u
export LC_ALL=C   # EPOCHREALTIME and numbers must use '.' as decimal point

SIDECAR_VERSION="0.1.2"

# ----------------------------------------------------------------- config ---

HUB_URL=""                 # e.g. https://msnm.example.org (no trailing slash)
TOKEN=""                   # issued by the hub operator
RPC_URL=""                 # default: derived from monerod's command line
RPC_LOGIN=""               # user:pass; default: monerod's --rpc-login
MONEROD_PID=""             # default: auto-detect
MONEROD_MATCH="--testnet"  # auto-detect only monerod processes with this arg
MONEROD_LOG=""             # default: derived from monerod's command line
DATA_DIR=""                # default: derived from monerod's command line
SPOOL_DIR="${SPOOL_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/msnm-sidecar}"
HOST_INTERVAL=5            # seconds between /proc samples
RPC_INTERVAL=30            # seconds between RPC polls
PUSH_INTERVAL=30           # seconds between pushes to the hub
RPC_TIMEOUT=10             # per RPC request
PUSH_TIMEOUT=60
SPOOL_MAX_MB=200           # oldest unsent batches are dropped beyond this
TX_SAMPLE_REGEX='0[0-2]'   # txid prefixes to report (~1.2% of txs)
LOG_WARN_MAX_PER_MIN=60    # cap on WARNING/ERROR log lines per minute
CAPTURE_CRASH=1            # upload a bundle when monerod exits unexpectedly
CAPTURE_GDB=1              # include a core dump backtrace when available
ALLOW_REMOTE_CAPTURE=0     # let the hub request a log-tail bundle (stalls)

CONFIG="${MSNM_CONFIG:-/etc/msnm-sidecar.conf}"
MODE=run

usage() {
  cat <<EOF
msnm-sidecar $SIDECAR_VERSION

Usage: $0 [-c CONFIG] [--check | --once]

  -c CONFIG  config file (default: \$MSNM_CONFIG or /etc/msnm-sidecar.conf)
  --check    detect monerod, test RPC and the hub connection, then exit
  --once     collect one batch and print it to stdout (nothing is sent)
EOF
}

while (($#)); do
  case $1 in
    -c|--config) CONFIG=$2; shift 2 ;;
    --check) MODE=check; shift ;;
    --once) MODE=once; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ -r $CONFIG ]]; then
  # shellcheck source=/dev/null
  source "$CONFIG"
elif [[ $MODE == run && -z ${MSNM_CONFIG:-} && $CONFIG != /etc/msnm-sidecar.conf ]]; then
  echo "config file not readable: $CONFIG" >&2
  exit 2
fi

# ---------------------------------------------------------------- helpers ---

say() { printf '%s msnm-sidecar: %s\n' "${EPOCHREALTIME%.*}" "$*" >&2; }
die() { say "error: $*"; exit 1; }

if ((BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4))); then
  echo "bash >= 4.4 required (found $BASH_VERSION)" >&2
  exit 1
fi

# NOW is set by now(): seconds since epoch with microseconds, e.g. 1791165403.123456
if [[ -n ${EPOCHREALTIME:-} ]]; then
  now() { NOW=$EPOCHREALTIME; }
else
  # bash < 5: whole seconds only, still without forking
  now() { printf -v NOW '%(%s)T.000000' -1; }
fi
now_us() { now; NOW_US=$(( 10#${NOW/./} )); }

# Sleep without forking: read with a timeout on a file descriptor that never
# delivers data. Falls back to /bin/sleep if the trick is unavailable.
# (The braces keep 2>/dev/null temporary; on a bare exec it would be permanent.)
if { exec {SLEEP_FD}<> <(:); } 2>/dev/null; then
  snooze() { read -r -t "$1" -u "$SLEEP_FD" _ || true; }
else
  snooze() { sleep "$1"; }
fi

for cmd in curl tail grep; do
  command -v "$cmd" >/dev/null || die "required command not found: $cmd"
done
HAVE_GZIP=0; command -v gzip >/dev/null && HAVE_GZIP=1

# ------------------------------------------------------------------ spool ---

# --check and --once send nothing, so they get a private throwaway spool:
# system users often have no writable home, and the service's spool belongs
# to the running service.
CONF_SPOOL_DIR=$SPOOL_DIR
if [[ $MODE != run ]]; then
  SPOOL_DIR=$(mktemp -d "${TMPDIR:-/tmp}/msnm-sidecar-$MODE.XXXXXX") || die "mktemp failed"
  trap 'rm -rf "$SPOOL_DIR"' EXIT
fi

OUTBOX="$SPOOL_DIR/outbox"   # batches and bundles waiting to be pushed
INBOX="$SPOOL_DIR/inbox"     # complete record files from background jobs
WORK="$SPOOL_DIR/work"
mkdir -p "$OUTBOX" "$INBOX" "$WORK" || die "cannot create spool dir $SPOOL_DIR"
HOST_CUR="$SPOOL_DIR/host.cur"   # written only by the main loop
LOG_CUR="$SPOOL_DIR/log.cur"     # written only by the log follower
SEQ_FILE="$SPOOL_DIR/seq"

SEQ=0
if [[ -r $SEQ_FILE ]]; then read -r SEQ < "$SEQ_FILE" || SEQ=0; fi
[[ $SEQ =~ ^[0-9]+$ ]] || SEQ=0

BOOT_ID=""
[[ -r /proc/sys/kernel/random/boot_id ]] && read -r BOOT_ID < /proc/sys/kernel/random/boot_id

# Record helpers. Record format (one per line, tab separated):
#   P <ts> <key> <value>                     profile (hardware, config)
#   E <ts> <event> <detail>                  sidecar/monerod events
#   S <ts> <k=v ...>                         host sample
#   M <ts> <k=v ...>                         monerod process sample
#   R <ts> <method> <http_code> <secs> <json> RPC response (newlines removed)
#   L <ts> <raw monerod log line>            selected log line
rec() { local IFS=$'\t'; printf '%s\n' "$*" >> "$HOST_CUR"; }
event() { now; rec E "$NOW" "$1" "${2:-}"; }

# Scrub IPv4/IPv6 addresses and onion/i2p hosts from text sent off-host.
SCRUB_SED='s/([0-9]{1,3}\.){3}[0-9]{1,3}/x.x.x.x/g; s/\[[0-9a-fA-F:]{2,}(%[a-z0-9]+)?\]/[ipv6]/g; s/[a-z2-7]{16,56}\.(onion|b32\.i2p)/x.\1/g'
scrub_args() {
  local a out=() hide=0
  for a in "$@"; do
    if ((hide)); then out+=("<redacted>"); hide=0; continue; fi
    case $a in
      --rpc-login|--rpc-ssl-private-key|--rpc-ssl-certificate|--tx-proxy|--anonymous-inbound) out+=("$a"); hide=1 ;;
      --rpc-login=*|--tx-proxy=*|--anonymous-inbound=*) out+=("${a%%=*}=<redacted>") ;;
      *) out+=("$a") ;;
    esac
  done
  local IFS=' '
  SCRUBBED="${out[*]}"
  [[ $SCRUBBED =~ [0-9]+\.[0-9]+\.[0-9]+\.[0-9]+ ]] && SCRUBBED=$(sed -E "$SCRUB_SED" <<< "$SCRUBBED")
}

# ------------------------------------------------------- monerod discovery ---

MPID=""
MARGS=()
MONEROD_UNIT=""

# get_marg NAME -> MVAL (value of --NAME from monerod's command line or config file)
get_marg() {
  local name=$1 i n=${#MARGS[@]}
  MVAL=""
  for ((i = 0; i < n; i++)); do
    case ${MARGS[i]} in
      "--$name") MVAL=${MARGS[i+1]:-}; return 0 ;;
      "--$name="*) MVAL=${MARGS[i]#*=}; return 0 ;;
    esac
  done
  if [[ -n ${MCONF_FILE:-} && -r $MCONF_FILE ]]; then
    local k v
    while IFS='=' read -r k v; do
      k=${k//[[:space:]]/}
      [[ $k == "$name" ]] && { MVAL=${v#"${v%%[![:space:]]*}"}; return 0; }
    done < "$MCONF_FILE"
  fi
  return 1
}
has_marg() {
  local a
  for a in "${MARGS[@]}"; do [[ $a == "--$1" || $a == "--$1=1" || $a == "--$1=true" ]] && return 0; done
  if [[ -n ${MCONF_FILE:-} && -r $MCONF_FILE ]]; then
    local k v
    while IFS='=' read -r k v; do
      k=${k//[[:space:]]/}; v=${v//[[:space:]]/}
      [[ $k == "$1" && ( -z $v || $v == 1 || $v == true ) ]] && return 0
    done < "$MCONF_FILE"
  fi
  return 1
}

find_monerod() {
  local p comm args found=() a ok
  if [[ -n $MONEROD_PID ]]; then
    [[ -d /proc/$MONEROD_PID ]] && found=("$MONEROD_PID")
  else
    for p in /proc/[0-9]*; do
      read -r comm < "$p/comm" 2>/dev/null || continue
      [[ $comm == monerod ]] || continue
      ok=1
      if [[ -n $MONEROD_MATCH ]]; then
        ok=0
        mapfile -d '' args < "$p/cmdline" 2>/dev/null || continue
        for a in "${args[@]}"; do [[ $a == "$MONEROD_MATCH" ]] && { ok=1; break; }; done
      fi
      ((ok)) && found+=("${p#/proc/}")
    done
  fi
  if ((${#found[@]} != 1)); then
    MPID=""
    FIND_ERR="found ${#found[@]} monerod processes (match: '${MONEROD_MATCH}')"
    ((${#found[@]} > 1)) && FIND_ERR+="; set MONEROD_PID or MONEROD_MATCH"
    return 1
  fi
  MPID=${found[0]}
  mapfile -d '' MARGS < "/proc/$MPID/cmdline" 2>/dev/null || MARGS=()
  MARGS=("${MARGS[@]:1}")
  derive_paths
  return 0
}

# Work out data dir, log file and RPC URL from monerod's arguments, matching
# monerod's own defaults (see src/cryptonote_core/cryptonote_core.cpp and
# src/daemon/main.cpp).
derive_paths() {
  local uid="" line base net="" home="" k
  while read -r k line; do [[ $k == Uid: ]] && { uid=${line%%[[:space:]]*}; break; }; done < "/proc/$MPID/status"
  while IFS=: read -r _ _ k _ _ line _; do
    [[ $k == "$uid" ]] && { home=$line; break; }
  done < /etc/passwd
  MCONF_FILE=""
  get_marg config-file && MCONF_FILE=$MVAL
  has_marg testnet && net=testnet
  has_marg stagenet && net=stagenet
  has_marg regtest && net=fake
  if get_marg data-dir; then base=$MVAL; else base="$home/.bitmonero"; fi
  M_DATA_DIR=$base${net:+/$net}
  [[ -z $MCONF_FILE && -r $M_DATA_DIR/bitmonero.conf ]] && MCONF_FILE=$M_DATA_DIR/bitmonero.conf
  if [[ -n $DATA_DIR ]]; then M_DATA_DIR=$DATA_DIR; fi
  if [[ -n $MONEROD_LOG ]]; then
    M_LOG=$MONEROD_LOG
  elif get_marg log-file; then
    M_LOG=$MVAL
    [[ $M_LOG == /* ]] || M_LOG="$M_DATA_DIR/$M_LOG"
  else
    M_LOG="$M_DATA_DIR/bitmonero.log"
  fi
  if [[ -n $RPC_URL ]]; then
    M_RPC=$RPC_URL
  else
    local ip=127.0.0.1 port
    case $net in testnet) port=28081 ;; stagenet) port=38081 ;; *) port=18081 ;; esac
    get_marg rpc-bind-port && port=$MVAL
    if get_marg rpc-bind-ip && [[ $MVAL != 0.0.0.0 ]]; then ip=$MVAL; fi
    M_RPC="http://$ip:$port"
  fi
  M_RPC_LOGIN=$RPC_LOGIN
  [[ -z $M_RPC_LOGIN ]] && get_marg rpc-login && M_RPC_LOGIN=$MVAL
  M_RESTRICTED=0; has_marg restricted-rpc && M_RESTRICTED=1
  M_SHOW_TIME_STATS=0; get_marg show-time-stats && [[ $MVAL != 0 ]] && M_SHOW_TIME_STATS=1
  MONEROD_UNIT=""
  local cg
  while IFS= read -r cg; do
    [[ $cg == 0::* ]] || continue
    cg=${cg#0::}
    [[ $cg =~ /([^/]+\.service) ]] && MONEROD_UNIT=${BASH_REMATCH[1]}
    M_CGROUP=$cg
  done < "/proc/$MPID/cgroup"
}

# --------------------------------------------------------- hardware profile ---

# Block devices backing a path: DISK_DEVS (kernel names, e.g. "dm-0 nvme0n1").
resolve_disk() {
  local path best="" bestdev="" fstype="" src="" mp majmin rest
  local -a f
  path=$(readlink -f "$1" 2>/dev/null) || path=$1
  while read -r -a f; do
    # mountinfo: id parent maj:min root mountpoint opts ... - fstype source superopts
    mp=${f[4]}; majmin=${f[2]}
    mp=${mp//\\040/ }
    if [[ $path == "$mp" || $path == "$mp"/* || $mp == / ]] && ((${#mp} >= ${#best})); then
      best=$mp; bestdev=$majmin
      rest="${f[*]}"; rest=${rest#* - }
      fstype=${rest%% *}; rest=${rest#* }; src=${rest%% *}
    fi
  done < /proc/self/mountinfo
  DISK_FSTYPE=$fstype; DISK_DEVS=""
  local link name
  link=$(readlink -f "/sys/dev/block/$bestdev" 2>/dev/null)
  if [[ ! -e $link && $src == /dev/* ]]; then
    link=$(readlink -f "$src" 2>/dev/null); link=${link##*/}
    link=$(readlink -f "/sys/class/block/$link" 2>/dev/null)
  fi
  if [[ -n $link && -e $link ]]; then
    name=${link##*/}
    # A partition's parent is the whole disk.
    if [[ -e $link/partition ]]; then name=${link%/*}; name=${name##*/}; fi
    DISK_DEVS=$name
    local s
    for s in /sys/block/"$name"/slaves/*; do
      [[ -e $s ]] || continue
      s=${s##*/}
      [[ -e /sys/class/block/$s/partition ]] && { s=$(readlink -f "/sys/class/block/$s"); s=${s%/*}; s=${s##*/}; }
      DISK_DEVS+=" $s"
    done
  fi
}

emit_profile() {
  now
  local t=$NOW v k line model="" logical=0 flags="" hyper=no
  local -A cores=()
  rec P "$t" sidecar_version "$SIDECAR_VERSION"
  rec P "$t" bash_version "$BASH_VERSION"
  rec P "$t" curl_version "$CURL_VERSION"
  rec P "$t" boot_id "$BOOT_ID"
  [[ -r /proc/sys/kernel/osrelease ]] && { read -r v < /proc/sys/kernel/osrelease; rec P "$t" kernel "$v"; }
  rec P "$t" arch "$ARCH"
  if [[ -r /etc/os-release ]]; then
    while IFS='=' read -r k v; do [[ $k == PRETTY_NAME ]] && { v=${v#\"}; rec P "$t" os "${v%\"}"; }; done < /etc/os-release
  fi
  local phys="" core=""
  while IFS=: read -r k v; do
    k=${k%"${k##*[![:space:]]}"}; v=${v# }
    case $k in
      processor) ((logical++)) ;;
      "model name"|"Model"|"cpu model") [[ -z $model ]] && model=$v ;;
      "physical id") phys=$v ;;
      "core id") core=$v; cores["$phys:$core"]=1 ;;
      flags|Features) [[ -z $flags ]] && flags=" $v " ;;
    esac
  done < /proc/cpuinfo
  rec P "$t" cpu_model "$model"
  rec P "$t" cpu_logical "$logical"
  rec P "$t" cpu_physical_cores "${#cores[@]}"
  [[ $flags == *" hypervisor "* ]] && hyper=yes
  rec P "$t" virtualized "$hyper"
  local fl sel=""
  for fl in aes avx avx2 avx512f sha_ni bmi2 adx asimd sha2 pmull; do [[ $flags == *" $fl "* ]] && sel+="$fl,"; done
  rec P "$t" cpu_flags "${sel%,}"
  if [[ -r /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq ]]; then
    read -r v < /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq; rec P "$t" cpu_max_khz "$v"
  fi
  if [[ -r /sys/class/dmi/id/product_name ]]; then
    read -r v < /sys/class/dmi/id/product_name 2>/dev/null && rec P "$t" machine "$v"
  fi
  while read -r k v _; do
    case $k in MemTotal:) rec P "$t" mem_total_kb "$v" ;; SwapTotal:) rec P "$t" swap_total_kb "$v" ;; esac
  done < /proc/meminfo
  rec P "$t" clk_tck "$CLK_TCK"
  rec P "$t" page_size "$PAGE_SIZE"
  rec P "$t" have_gzip "$HAVE_GZIP"
  rec P "$t" config "host_interval=$HOST_INTERVAL rpc_interval=$RPC_INTERVAL push_interval=$PUSH_INTERVAL tx_sample=$TX_SAMPLE_REGEX capture_crash=$CAPTURE_CRASH capture_gdb=$CAPTURE_GDB remote_capture=$ALLOW_REMOTE_CAPTURE"
  [[ -n $MPID ]] || return 0
  scrub_args "${MARGS[@]}"
  rec P "$t" monerod_pid "$MPID"
  rec P "$t" monerod_args "$SCRUBBED"
  rec P "$t" monerod_exe "$(readlink "/proc/$MPID/exe" 2>/dev/null)"
  rec P "$t" monerod_unit "$MONEROD_UNIT"
  rec P "$t" monerod_restricted_rpc "$M_RESTRICTED"
  rec P "$t" monerod_show_time_stats "$M_SHOW_TIME_STATS"
  rec P "$t" data_dir "$M_DATA_DIR"
  resolve_disk "$M_DATA_DIR"
  rec P "$t" data_fstype "$DISK_FSTYPE"
  rec P "$t" data_devices "$DISK_DEVS"
  local d rot size dmodel
  for d in $DISK_DEVS; do
    rot=""; size=""; dmodel=""
    [[ -r /sys/block/$d/queue/rotational ]] && read -r rot < "/sys/block/$d/queue/rotational"
    [[ -r /sys/block/$d/size ]] && read -r size < "/sys/block/$d/size"
    [[ -r /sys/block/$d/device/model ]] && read -r dmodel < "/sys/block/$d/device/model"
    rec P "$t" "disk.$d" "rotational=$rot sectors=$size model=${dmodel// /_}"
  done
  # cgroup v2 resource limits applied to monerod (relevant if it runs in a
  # container or a systemd unit with limits)
  if [[ -n ${M_CGROUP:-} && -d /sys/fs/cgroup$M_CGROUP ]]; then
    local cgd=/sys/fs/cgroup$M_CGROUP lim=""
    for k in memory.max memory.high memory.swap.max cpu.max cpuset.cpus.effective io.max; do
      if [[ -r $cgd/$k ]]; then v=""; read -r v < "$cgd/$k"; lim+="$k=${v// /_} "; fi
    done
    rec P "$t" monerod_cgroup_limits "${lim% }"
  fi
}

# ----------------------------------------------------------- host sampling ---

sample_host() {
  local k a b c d e f g h i line out
  now
  out="cpu="
  # /proc/stat: aggregate cpu line, procs_running, procs_blocked, ctxt
  while read -r k a b c d e f g h _; do
    case $k in
      cpu) out+="$a,$b,$c,$d,$e,$f,$g,$h" ;;
      ctxt) out+=" ctxt=$a" ;;
      procs_running) out+=" procs_running=$a" ;;
      procs_blocked) out+=" procs_blocked=$a" ;;
    esac
  done < /proc/stat
  while read -r k a _; do
    case $k in
      MemTotal:) out+=" mem_total_kb=$a" ;; MemFree:) out+=" mem_free_kb=$a" ;;
      MemAvailable:) out+=" mem_avail_kb=$a" ;; Buffers:) out+=" buffers_kb=$a" ;;
      Cached:) out+=" cached_kb=$a" ;; Dirty:) out+=" dirty_kb=$a" ;;
      Writeback:) out+=" writeback_kb=$a" ;; SwapTotal:) out+=" swap_total_kb=$a" ;;
      SwapFree:) out+=" swap_free_kb=$a" ;;
    esac
  done < /proc/meminfo
  read -r a b c _ < /proc/loadavg
  out+=" load=$a,$b,$c"
  # pgmajfault/pswpin/pswpout: paging activity (swap and file-backed faults)
  while read -r k a; do
    case $k in pgmajfault|pswpin|pswpout) out+=" $k=$a" ;; esac
  done < /proc/vmstat
  if [[ -n $DISK_DEVS ]]; then
    # diskstats: maj min name reads rmerged rsect rms writes wmerged wsect wms inflight ioms wioms
    while read -r _ _ k a _ b c d _ e f g h i _; do
      [[ " $DISK_DEVS " == *" $k "* ]] && out+=" disk.$k=$a,$b,$c,$d,$e,$f,$g,$h,$i"
    done < /proc/diskstats
  fi
  local rx=0 tx=0 ifc
  local -a nf
  while IFS= read -r line; do
    [[ $line == *:* ]] || continue
    ifc=${line%%:*}; ifc=${ifc//[[:space:]]/}
    [[ $ifc == lo ]] && continue
    read -r -a nf <<< "${line#*:}"
    rx=$((rx + nf[0])); tx=$((tx + nf[8]))
  done < /proc/net/dev
  out+=" net_rx=$rx net_tx=$tx"
  if [[ -d /proc/pressure ]]; then
    for k in cpu io memory; do
      [[ -r /proc/pressure/$k ]] || continue
      while read -r a b c d e; do
        out+=" psi_${k}_$a=${e#total=}"
      done < "/proc/pressure/$k"
    done
  fi
  rec S "$NOW" "$out"
}

sample_monerod() {
  [[ -n $MPID ]] || return 0
  local line rest k v out
  local -a f
  read -r line < "/proc/$MPID/stat" 2>/dev/null || return 1
  rest=${line##*) }
  read -r -a f <<< "$rest"
  # fields after comm: f[0]=state(3) f[7]=minflt(10) f[9]=majflt(12)
  # f[11]=utime(14) f[12]=stime(15) f[17]=num_threads(20) f[19]=starttime(22)
  now
  out="pid=$MPID state=${f[0]} minflt=${f[7]} majflt=${f[9]} utime=${f[11]} stime=${f[12]} num_threads=${f[17]} starttime=${f[19]}"
  while read -r k v _; do
    case $k in
      VmRSS:) out+=" rss_kb=$v" ;; VmHWM:) out+=" hwm_kb=$v" ;; VmSize:) out+=" vms_kb=$v" ;;
      VmSwap:) out+=" swap_kb=$v" ;; RssFile:) out+=" rss_file_kb=$v" ;; RssAnon:) out+=" rss_anon_kb=$v" ;;
      voluntary_ctxt_switches:) out+=" vcsw=$v" ;; nonvoluntary_ctxt_switches:) out+=" nvcsw=$v" ;;
    esac
  done < "/proc/$MPID/status"
  if [[ -r /proc/$MPID/io ]]; then
    while read -r k v; do
      case $k in read_bytes:) out+=" io_read_bytes=$v" ;; write_bytes:) out+=" io_write_bytes=$v" ;; esac
    done < "/proc/$MPID/io" 2>/dev/null
  fi
  rec M "$NOW" "$out"
}

# ------------------------------------------------------------- RPC polling ---

RPC_METHODS=(get_info get_last_block_header get_fee_estimate get_connections get_bans)
# From the 2026 fee-scaling fork, monerod logs a "possible wallet fingerprint"
# WARNING for any grace_blocks other than 1000 (omitted means 0). Asking with
# 1000 also matches what wallets see. On a node still syncing below that fork
# the request errors out, which is harmless: it is recorded as rpc_error.
declare -A RPC_PARAMS=([get_fee_estimate]='{"grace_blocks":1000}')
SEP=$'\x1e'
MARK="${SEP}MSNM${SEP}"

# One curl process performs every request in turn (--next). Each response is
# followed by a marker line carrying its HTTP status and duration. If a
# request fails (e.g. monerod is hung and get_info times out), --fail-early
# skips the rest so the failure is reported after one timeout, not six.
collect_rpc() {
  local args=(--fail-early) m first=1 out t0 tmp body="" i=0 line code secs
  local -a parts
  local auth=()
  [[ -n $M_RPC_LOGIN ]] && auth=(--digest -u "$M_RPC_LOGIN")
  for m in "${RPC_METHODS[@]}"; do
    ((first)) || args+=(--next); first=0
    args+=(-sS --max-time "$RPC_TIMEOUT" "${auth[@]}" -H 'Content-Type: application/json'
           --data "{\"jsonrpc\":\"2.0\",\"id\":\"0\",\"method\":\"$m\"${RPC_PARAMS[$m]:+,\"params\":${RPC_PARAMS[$m]}}}"
           -w "\n${MARK}%{http_code}${SEP}%{time_total}\n" "$M_RPC/json_rpc")
  done
  args+=(--next -sS --max-time "$RPC_TIMEOUT" "${auth[@]}" -H 'Content-Type: application/json'
         --data '{}' -w "\n${MARK}%{http_code}${SEP}%{time_total}\n" "$M_RPC/get_transaction_pool_stats")
  local methods=("${RPC_METHODS[@]}" get_transaction_pool_stats)
  now; t0=$NOW
  out=$(curl "${args[@]}" 2>/dev/null)
  tmp="$INBOX/.rpc.$BASHPID"
  {
    while IFS= read -r line; do
      if [[ $line == "$MARK"* ]]; then
        IFS=$SEP read -r -a parts <<< "${line#"$MARK"}"
        code=${parts[0]:-000}; secs=${parts[1]:-0}
        body=${body//$'\t'/ }; body=${body//$'\r'/}
        printf 'R\t%s\t%s\t%s\t%s\t%s\n' "$t0" "${methods[i]:-unknown}" "$code" "$secs" "$body"
        body=""; ((i++))
      else
        body+="$line "
      fi
    done <<< "$out"
    # Requests that produced no marker at all (curl killed or failed early)
    while ((i < ${#methods[@]})); do
      printf 'R\t%s\t%s\t000\t0\t\n' "$t0" "${methods[i]}"; ((i++))
    done
  } > "$tmp"
  mv "$tmp" "$INBOX/rpc.$t0"
}

# ------------------------------------------------------------ log follower ---

LOG_PGID=""

start_log_follower() {
  stop_log_follower
  [[ -n ${M_LOG:-} ]] || return 0
  local T=$'\t' pattern
  pattern="BLOCK SUCCESSFULLY ADDED|${T}id:${T}<|${T}PoW:${T}<|${T}HEIGHT [0-9]+, difficulty:|${T}block reward: "
  pattern+="|${T}Height: [0-9]+ coinbase weight|REORGANIZE|BLOCK ADDED AS ALTERNATIVE|BLOCK ADDED AS INVALID"
  pattern+="|orphaned and rejected|Transaction added to pool: txid <${TX_SAMPLE_REGEX}"
  pattern+="|${T}(WARNING|ERROR|FATAL)${T}"
  local tail_opts=(-n 0 -F)
  ((TAIL_HAS_PID)) && tail_opts+=(--pid=$$)
  set -m   # job control: the follower gets its own process group
  {
    tail "${tail_opts[@]}" "$M_LOG" 2>/dev/null | grep --line-buffered -E "$pattern" | {
      local line minute=0 warn=0 dropped=0 m
      while IFS= read -r line; do
        now
        m=${NOW%%.*}; m=$((m / 60))
        if ((m != minute)); then
          ((dropped)) && printf 'E\t%s\tlog_warn_dropped\t%s\n' "$NOW" "$dropped" >> "$LOG_CUR"
          minute=$m; warn=0; dropped=0
        fi
        if [[ $line =~ $'\t'(WARNING|ERROR|FATAL)$'\t' ]]; then
          if ((++warn > LOG_WARN_MAX_PER_MIN)); then ((dropped++)); continue; fi
        fi
        printf 'L\t%s\t%s\n' "$NOW" "$line" >> "$LOG_CUR"
      done
    }
  } &
  LOG_PGID=$!
  set +m
  LOG_FOLLOWED=$M_LOG
}

stop_log_follower() {
  [[ -n $LOG_PGID ]] && kill -- "-$LOG_PGID" 2>/dev/null
  LOG_PGID=""
}

# ----------------------------------------------------------- crash capture ---

# Collect what's useful for a bug report after monerod exits, then queue it.
capture_bundle() {
  local kind=$1 pid=$2 log=$3 unit=$4 t dir
  now; t=$NOW
  dir="$WORK/bundle.$kind.${t%.*}"
  mkdir -p "$dir" || return 1
  {
    echo "kind=$kind"; echo "time=$t"; echo "pid=$pid"; echo "unit=$unit"
    echo "sidecar_version=$SIDECAR_VERSION"
  } > "$dir/meta.txt"
  [[ $kind == crash ]] && sleep 5   # give systemd-coredump time to finish
  if [[ -r $log ]]; then
    tail -n 5000 "$log" | sed -E "$SCRUB_SED" > "$dir/monerod-log-tail.txt"
  fi
  if [[ $kind == crash ]]; then
    { journalctl -k --since "-20min" --no-pager 2>/dev/null || dmesg 2>/dev/null | tail -n 500; } \
      | grep -iE 'oom|out of memory|killed process|monerod|segfault|general protection' \
      | sed -E "$SCRUB_SED" > "$dir/kernel.txt"
    if [[ -n $unit ]] && command -v systemctl >/dev/null; then
      systemctl show "$unit" -p Result -p ExecMainCode -p ExecMainStatus -p NRestarts > "$dir/systemd.txt" 2>&1
    fi
    if command -v coredumpctl >/dev/null; then
      coredumpctl --no-pager info "$pid" > "$dir/coredump-info.txt" 2>&1
      if ((CAPTURE_GDB)) && command -v gdb >/dev/null && grep -q 'Storage: .*present' "$dir/coredump-info.txt" 2>/dev/null; then
        timeout 600 coredumpctl --no-pager debug "$pid" \
          --debugger-arguments='-batch -ex "info sharedlibrary" -ex "thread apply all bt"' \
          > "$dir/backtrace.txt" 2>&1
        sed -i -E "$SCRUB_SED" "$dir/backtrace.txt"
      fi
    fi
  fi
  tar -C "$WORK" -czf "$OUTBOX/bundle.${t%.*}.$kind.tar.gz" "${dir##*/}" && rm -rf "$dir"
}

# --------------------------------------------------------- batching & push ---

rotate() {
  local f files=() batch hdr
  mv -f "$HOST_CUR" "$WORK/1.host" 2>/dev/null && files+=("$WORK/1.host")
  mv -f "$LOG_CUR" "$WORK/2.log" 2>/dev/null && files+=("$WORK/2.log")
  for f in "$INBOX"/rpc.*; do
    [[ -e $f ]] || continue
    mv -f "$f" "$WORK/3.${f##*/}" && files+=("$WORK/3.${f##*/}")
  done
  ((${#files[@]})) || return 0
  SEQ=$((SEQ + 1))
  printf '%s\n' "$SEQ" > "$SEQ_FILE"
  now
  hdr=$'#MSNM1\t'"seq=$SEQ"$'\t'"created=$NOW"$'\t'"sidecar=$SIDECAR_VERSION"$'\t'"boot_id=$BOOT_ID"$'\t'"monerod_pid=$MPID"
  printf -v batch '%s/batch.%012d' "$OUTBOX" "$SEQ"
  if ((HAVE_GZIP)); then
    { printf '%s\n' "$hdr"; cat "${files[@]}"; } | gzip -c > "$batch.tmp" && mv "$batch.tmp" "$batch.gz"
  else
    { printf '%s\n' "$hdr"; cat "${files[@]}"; } > "$batch.tmp" && mv "$batch.tmp" "$batch.txt"
  fi
  rm -f "${files[@]}"
  enforce_spool_limit
}

enforce_spool_limit() {
  local kb f
  kb=$(du -sk "$OUTBOX" 2>/dev/null) || return 0
  kb=${kb%%[[:space:]]*}
  ((kb > SPOOL_MAX_MB * 1024)) || return 0
  for f in "$OUTBOX"/batch.*; do
    [[ -e $f ]] || continue
    rm -f "$f"
    event spool_dropped "${f##*/}"
    kb=$(du -sk "$OUTBOX" 2>/dev/null); kb=${kb%%[[:space:]]*}
    ((kb > SPOOL_MAX_MB * 1024)) || break
  done
}

# Push queued batches and bundles, oldest first. Runs in the background.
push_outbox() {
  local f path code enc resp="$SPOOL_DIR/push.resp" sent
  for f in "$OUTBOX"/batch.* "$OUTBOX"/bundle.*; do
    [[ -e $f && $f != *.tmp ]] || continue
    enc=()
    case $f in
      */batch.*.gz) path=/v1/ingest; enc=(-H 'Content-Encoding: gzip' -H 'Content-Type: text/plain') ;;
      */batch.*.txt) path=/v1/ingest; enc=(-H 'Content-Type: text/plain') ;;
      */bundle.*) path="/v1/bundle?name=${f##*/}"; enc=(-H 'Content-Type: application/gzip') ;;
      *) continue ;;
    esac
    sent=$EPOCHREALTIME
    code=$(curl -sS -o "$resp" -w '%{http_code}' --max-time "$PUSH_TIMEOUT" \
      -H "Authorization: Bearer $TOKEN" -H "X-MSNM-Sent: $sent" \
      -H "X-MSNM-Sidecar: $SIDECAR_VERSION" "${enc[@]}" \
      --data-binary @"$f" "$HUB_URL$path" 2>/dev/null)
    case $code in
      2??) rm -f "$f"; handle_response "$resp" ;;
      401|403) say "hub rejected token (HTTP $code); check TOKEN"; return 1 ;;
      413) say "hub rejected ${f##*/} as too large; dropping"; rm -f "$f" ;;
      *) return 1 ;;   # hub unreachable or erroring: retry next round
    esac
  done
}

# The hub can ask for a log-tail bundle (e.g. when it sees this node stalled).
handle_response() {
  local line
  [[ -r $1 ]] || return 0
  while IFS=$'\t' read -r line _; do
    case $line in
      capture_logs)
        if ((ALLOW_REMOTE_CAPTURE)) && [[ -n $MPID ]]; then
          capture_bundle stall "$MPID" "$M_LOG" "$MONEROD_UNIT" &
        fi ;;
    esac
  done < "$1"
}

# ------------------------------------------------------------------- setup ---

CURL_VERSION=$(curl --version 2>/dev/null | { read -r _ v _; echo "$v"; })
ARCH=$(uname -m 2>/dev/null)
CLK_TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)
PAGE_SIZE=$(getconf PAGESIZE 2>/dev/null || echo 4096)
TAIL_HAS_PID=0
tail --help 2>&1 | grep -q -- '--pid' && TAIL_HAS_PID=1
DISK_DEVS=""

check() {
  local ok=1
  echo "msnm-sidecar $SIDECAR_VERSION (bash $BASH_VERSION, curl $CURL_VERSION)"
  [[ -n ${EPOCHREALTIME:-} ]] || echo "  note: bash < 5, timestamps have 1 s resolution"
  if find_monerod; then
    echo "monerod:   pid $MPID${MONEROD_UNIT:+ (unit $MONEROD_UNIT)}"
    echo "data dir:  $M_DATA_DIR"
    if [[ -r $M_LOG ]]; then echo "log file:  $M_LOG (readable)"
    else ok=0; echo "log file:  $M_LOG (NOT READABLE)"; fi
    echo "rpc:       $M_RPC${M_RPC_LOGIN:+ (with login)}"
    ((M_RESTRICTED)) && { echo "  warning: monerod runs with --restricted-rpc; some data will be missing"; }
    ((M_SHOW_TIME_STATS)) || echo "  warning: monerod lacks --show-time-stats 1; per-block timings will be missing"
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "$RPC_TIMEOUT" ${M_RPC_LOGIN:+--digest -u "$M_RPC_LOGIN"} \
      -H 'Content-Type: application/json' --data '{"jsonrpc":"2.0","id":"0","method":"get_info"}' "$M_RPC/json_rpc" 2>/dev/null)
    if [[ $code == 200 ]]; then echo "  rpc get_info: OK"; else ok=0; echo "  rpc get_info: FAILED (HTTP $code)"; fi
    resolve_disk "$M_DATA_DIR"
    echo "disk:      $DISK_DEVS ($DISK_FSTYPE)"
  else
    ok=0; echo "monerod:   NOT FOUND: $FIND_ERR"
  fi
  if [[ -n $HUB_URL ]]; then
    local code
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 -H "Authorization: Bearer $TOKEN" "$HUB_URL/v1/ping" 2>/dev/null)
    case $code in
      200) echo "hub:       $HUB_URL OK (token accepted)" ;;
      401|403) ok=0; echo "hub:       $HUB_URL reachable, but the TOKEN was rejected" ;;
      *) ok=0; echo "hub:       $HUB_URL NOT reachable (HTTP $code)" ;;
    esac
  else
    ok=0; echo "hub:       HUB_URL not set"
  fi
  echo "spool:     $CONF_SPOOL_DIR"
  ((ok)) && echo "all checks passed" || echo "some checks FAILED"
  ((ok))
}

case $MODE in
  check) check; exit $? ;;
  once)
    find_monerod || say "monerod not found: $FIND_ERR"
    HOST_CUR="$WORK/once.host"; : > "$HOST_CUR"
    emit_profile; sample_host; sample_monerod
    [[ -n $MPID ]] && { collect_rpc; cat "$HOST_CUR" "$INBOX"/rpc.* 2>/dev/null; rm -f "$INBOX"/rpc.*; } || cat "$HOST_CUR"
    rm -f "$HOST_CUR"
    exit 0 ;;
esac

[[ -n $HUB_URL ]] || say "HUB_URL not set: batches will be queued in $OUTBOX but not sent"
[[ -n $TOKEN || -z $HUB_URL ]] || die "TOKEN not set"

RPC_PID=""; PUSH_PID=""
cleanup() {
  stop_log_follower
  local p
  for p in $RPC_PID $PUSH_PID; do kill "$p" 2>/dev/null; done
  rotate 2>/dev/null
  exit 0
}
trap cleanup INT TERM

event sidecar_start "version=$SIDECAR_VERSION"
if find_monerod; then
  event monerod_found "pid=$MPID"
  say "monitoring monerod pid $MPID (rpc $M_RPC, log $M_LOG)"
  start_log_follower
else
  say "waiting for monerod: $FIND_ERR"
fi
emit_profile

now_us
next_host=$NOW_US
next_rpc=$NOW_US
next_push=$((NOW_US + PUSH_INTERVAL * 1000000))
next_profile=$((NOW_US + 3600 * 1000000))
next_find=0

while :; do
  now_us
  if ((NOW_US >= next_host)); then
    # monerod gone? record it and capture a bundle, then look for it again.
    if [[ -n $MPID && ! -d /proc/$MPID ]]; then
      event monerod_exit "pid=$MPID"
      say "monerod pid $MPID exited"
      ((CAPTURE_CRASH)) && capture_bundle crash "$MPID" "$M_LOG" "$MONEROD_UNIT" &
      MPID=""
    fi
    if [[ -z $MPID ]] && ((NOW_US >= next_find)); then
      next_find=$((NOW_US + 15 * 1000000))
      if find_monerod; then
        event monerod_found "pid=$MPID"
        say "monitoring monerod pid $MPID"
        [[ $M_LOG != "${LOG_FOLLOWED:-}" || -z $LOG_PGID ]] && start_log_follower
        emit_profile
      fi
    fi
    sample_host
    sample_monerod
    next_host=$((next_host + HOST_INTERVAL * 1000000))
    ((next_host <= NOW_US)) && next_host=$((NOW_US + HOST_INTERVAL * 1000000))
  fi
  if ((NOW_US >= next_rpc)); then
    if [[ -n $MPID ]] && ! { [[ -n $RPC_PID ]] && kill -0 "$RPC_PID" 2>/dev/null; }; then
      collect_rpc &
      RPC_PID=$!
    fi
    next_rpc=$((next_rpc + RPC_INTERVAL * 1000000))
    ((next_rpc <= NOW_US)) && next_rpc=$((NOW_US + RPC_INTERVAL * 1000000))
  fi
  if ((NOW_US >= next_profile)); then
    emit_profile
    next_profile=$((NOW_US + 3600 * 1000000))
  fi
  if ((NOW_US >= next_push)); then
    rotate
    if [[ -n $HUB_URL ]] && ! { [[ -n $PUSH_PID ]] && kill -0 "$PUSH_PID" 2>/dev/null; }; then
      push_outbox &
      PUSH_PID=$!
    fi
    next_push=$((next_push + PUSH_INTERVAL * 1000000))
    ((next_push <= NOW_US)) && next_push=$((NOW_US + PUSH_INTERVAL * 1000000))
  fi
  now_us
  wake=$next_host
  ((next_rpc < wake)) && wake=$next_rpc
  ((next_push < wake)) && wake=$next_push
  delay=$((wake - NOW_US))
  ((delay < 10000)) && delay=10000
  printf -v delay_s '%d.%06d' $((delay / 1000000)) $((delay % 1000000))
  snooze "$delay_s"
done
