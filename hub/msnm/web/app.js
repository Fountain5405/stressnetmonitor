// msnm public dashboard. Polls /api/live.json (5 s) and /api/history.json (60 s).
// All data is rendered as text (textContent), never as HTML.
"use strict";
(function () {
  const LIVE_MS = 5000, HIST_MS = 60000, OFFLINE_MS = 20000;
  const MONO = 'ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace';
  const ACCENT = "#ff6a00";
  const PALETTE = ["#ff6a00", "#4cc9f0", "#3ddc97", "#c77dff", "#f5b83d", "#ff4d6d",
                   "#7aa2ff", "#2ec4b6", "#f78c6b", "#a3e635", "#e879f9", "#94a3b8"];
  const SVGNS = "http://www.w3.org/2000/svg";

  const $ = (id) => document.getElementById(id);
  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  // ------------------------------------------------------------ formatting
  const nf = new Intl.NumberFormat("en-US");
  const ok = (v) => v != null && Number.isFinite(v);
  const int = (v) => (ok(v) ? nf.format(Math.round(v)) : "—");
  const fixed = (v, d) => (ok(v) ? v.toFixed(d) : "—");
  const pct = (r) => (ok(r) ? Math.round(r * 100) + "%" : "—");
  function bytes(b) {
    if (!ok(b)) return "—";
    const u = ["B", "kB", "MB", "GB", "TB"];
    let i = 0;
    while (Math.abs(b) >= 1000 && i < u.length - 1) { b /= 1000; i++; }
    return (i === 0 ? b.toFixed(0) : b.toFixed(b < 10 ? 2 : b < 100 ? 1 : 0)) + " " + u[i];
  }
  function gib(b) {
    if (!ok(b)) return "—";
    const g = b / 2 ** 30;
    return (g < 10 ? g.toFixed(1) : Math.round(g)) + " GB";
  }
  function si(v) {
    if (!ok(v)) return "—";
    const u = ["", "k", "M", "G", "T", "P"];
    let i = 0;
    while (Math.abs(v) >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return (i === 0 ? Math.round(v).toString() : v.toFixed(v < 10 ? 2 : v < 100 ? 1 : 0)) + (u[i] ? " " + u[i] : "");
  }
  function dur(s) {
    if (!ok(s)) return "—";
    s = Math.max(0, s);
    if (s < 60) return Math.round(s) + " s";
    if (s < 3600) return Math.floor(s / 60) + " m " + String(Math.round(s % 60)).padStart(2, "0") + " s";
    if (s < 86400) return Math.floor(s / 3600) + " h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + " m";
    return Math.floor(s / 86400) + " d " + Math.floor((s % 86400) / 3600) + " h";
  }
  function ago(s) {
    if (!ok(s)) return "—";
    s = Math.max(0, s);
    if (s < 60) return Math.round(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m ago";
  }
  function ms(v) {
    if (!ok(v)) return "—";
    if (v < 1) return "<1 ms";
    if (v < 1000) return Math.round(v) + " ms";
    return (v / 1000).toFixed(v < 10000 ? 2 : 1) + " s";
  }
  function secs(v) {
    if (!ok(v)) return "—";
    const a = Math.abs(v);
    if (a < 10) return v.toFixed(2) + " s";
    if (a < 100) return v.toFixed(1) + " s";
    return dur(v);
  }
  function mmss(s) {
    if (!ok(s)) return "—";
    const m = Math.floor(s / 60), r = Math.round(s % 60);
    return m + ":" + String(r).padStart(2, "0");
  }
  function hashrate(v) {
    if (!ok(v)) return "—";
    const u = ["H/s", "kH/s", "MH/s", "GH/s", "TH/s"];
    let i = 0;
    while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
    return v.toFixed(v < 10 ? 2 : v < 100 ? 1 : 0) + " " + u[i];
  }
  function utc(ts, withSec) {
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getUTCHours()) + ":" + p(d.getUTCMinutes()) + (withSec ? ":" + p(d.getUTCSeconds()) : "");
  }
  function setValue(el, text, unit) {
    el.textContent = text;
    if (unit) el.append(h("span", "unit", unit));
  }

  const colors = new Map();
  function colorFor(id) {
    if (!colors.has(id)) colors.set(id, PALETTE[colors.size % PALETTE.length]);
    return colors.get(id);
  }

  // ----------------------------------------------------------------- state
  let live = null, hist = null, skew = 0, lastOk = 0, prevTip = null, prevTop = null;
  const nowS = () => Date.now() / 1000 + skew;

  async function getJSON(url) {
    const r = await fetch(url, { credentials: "omit" });
    if (!r.ok) throw new Error(url + ": HTTP " + r.status);
    return r.json();
  }

  async function pollLive() {
    if (!document.hidden || !live) {
      try {
        const d = await getJSON("/api/live.json");
        skew = d.now - Date.now() / 1000;
        live = d;
        lastOk = Date.now();
        render();
      } catch (e) {
        console.warn(e);
      }
    }
    setTimeout(pollLive, LIVE_MS);
  }

  async function pollHist() {
    if (!document.hidden || !hist) {
      try {
        hist = await getJSON("/api/history.json");
        renderCharts();
        if (live) renderNodes(live.nodes);
      } catch (e) {
        console.warn(e);
      }
    }
    setTimeout(pollHist, HIST_MS);
  }

  function tick() {
    const t = nowS();
    $("clock").textContent = utc(t, true);
    const off = !lastOk || Date.now() - lastOk > OFFLINE_MS;
    $("live-dot").classList.toggle("off", off);
    const conn = $("conn");
    conn.textContent = !lastOk ? "connecting…" : off ? "hub unreachable — retrying" : "live";
    conn.classList.toggle("bad", off && !!lastOk);
    if (live && live.network) {
      const n = live.network;
      const seen = ok(n.tip_seen) ? n.tip_seen : n.tip_timestamp;
      $("k-age").textContent = ok(seen) ? dur(t - seen) : "—";
    }
  }

  // ------------------------------------------------------------ live view
  function render() {
    renderNetwork(live.network || {}, live.nodes || []);
    renderBlocks(live.blocks || [], live.network || {});
    renderNodes(live.nodes || []);
    renderLinks(live.links || []);
    $("gen").textContent = "data at " + utc(live.now, true) + " UTC";
    tick();
  }

  function flash(el) {
    el.classList.remove("flash");
    void el.offsetWidth;  // restart the animation
    el.classList.add("flash");
  }

  function renderNetwork(n, nodes) {
    const kh = $("k-height");
    kh.textContent = int(n.height);
    if (prevTip != null && ok(n.height) && n.height > prevTip) flash(kh);
    if (ok(n.height)) prevTip = n.height;
    $("k-hf").textContent = ok(n.major_version) ? "HF v" + n.major_version : "";
    $("k-hash").textContent = n.hash ? n.hash.slice(0, 16) + "…" : "";
    $("k-age-foot").textContent = ok(n.txs) ? int(n.txs) + " tx · " + bytes(n.weight) : "";

    setValue($("k-tps"), fixed(n.tps_1h, n.tps_1h != null && n.tps_1h < 10 ? 2 : 1), "tx/s");
    let tf = int(n.txs_1h) + " tx in " + int(n.blocks_1h) + " blocks (1 h)";
    if (ok(n.tx_in_per_min)) tf += " · " + fixed(n.tx_in_per_min / 60, 2) + "/s arriving";
    $("k-tps-foot").textContent = tf;

    setValue($("k-pool"), int(n.pool_txs), "tx");
    $("k-pool-foot").textContent = ok(n.pool_bytes) ? bytes(n.pool_bytes) + " waiting" : "";

    $("k-weight").textContent = bytes(n.weight);
    const fill = ok(n.weight) && ok(n.weight_median) && n.weight_median > 0 ? Math.min(1, n.weight / (2 * n.weight_median)) : 0;
    $("k-weight-bar").style.width = (fill > 0 ? Math.max(1.5, fill * 100) : 0).toFixed(1) + "%";
    $("k-weight-foot").textContent = "median " + bytes(n.weight_median) + " · 30-block avg " + bytes(n.weight_30);

    $("k-interval").textContent = mmss(n.interval_30);
    $("k-interval-foot").textContent = "avg of 30 blocks · target 2:00";
    $("k-hash-rate").textContent = hashrate(n.hashrate);
    $("k-hash-foot").textContent = ok(n.difficulty) ? "difficulty " + si(n.difficulty) : "";

    const counts = {};
    for (const x of nodes) counts[x.state] = (counts[x.state] || 0) + 1;
    setValue($("k-nodes"), (counts.OK || 0) + "/" + nodes.length, "OK");
    $("k-nodes-foot").textContent = Object.keys(counts).filter((s) => s !== "OK").sort()
      .map((s) => counts[s] + " " + s.toLowerCase().replace("_", " ")).join(" · ") || "all in sync";
  }

  function renderBlocks(blocks, n) {
    const box = $("blocks");
    const top = blocks.length ? blocks[0].h : null;
    const maxW = Math.max(1, ...blocks.map((b) => b.weight || 0), 2 * (n.weight_median || 0));
    box.textContent = "";
    blocks.forEach((b, i) => {
      const c = h("div", "blk");
      if (prevTop != null && b.h > prevTop) c.classList.add("new");
      c.append(h("div", "h", int(b.h)));
      const r1 = h("div", "row");
      r1.append(h("span", null, int(b.txs) + " tx"), h("span", null, bytes(b.weight)));
      const older = blocks[i + 1];
      const r2 = h("div", "row");
      const seen = ok(b.seen) ? b.seen : b.ts;
      r2.append(h("span", null, ago(nowS() - seen)),
                h("span", null, older && ok(b.ts) && ok(older.ts) ? "+" + mmss(b.ts - older.ts) : ""));
      const f = h("i", "fill");
      f.style.width = Math.max(2, (100 * (b.weight || 0)) / maxW).toFixed(1) + "%";
      c.append(r1, r2, f);
      c.title = "block " + b.h + "\n" + (b.hash || "") + "\nsize " + bytes(b.size) + ", weight " + bytes(b.weight);
      box.append(c);
    });
    if (top != null) prevTop = top;
  }

  function renderLinks(links) {
    const p = $("links");
    if (p.childElementCount === links.length) return;
    p.textContent = "";
    for (const l of links) {
      if (typeof l.url !== "string" || !/^https?:\/\//.test(l.url)) continue;
      const a = h("a", null, l.label || l.url);
      a.href = l.url;
      a.rel = "noopener noreferrer";
      a.target = "_blank";
      p.append(a);
    }
  }

  // ----------------------------------------------------------------- nodes
  const METRICS = [
    ["height", "Height", "Top block of this node"],
    ["lag", "Lag", "Blocks behind the network tip"],
    ["peers", "Peers", "P2P connections: outgoing ↑ / incoming ↓"],
    ["verify", "Verify", "Time to verify and add the latest block; below: median of the last 30 blocks"],
    ["delay", "Delay", "Seconds after a reference first saw the latest block until this node accepted it; below: median of the last 30"],
    ["pool", "Pool", "Transactions in this node's pool; below: its share of the reference's pool"],
    ["cpu", "CPU", "Host CPU busy; below: monerod's own use, in cores"],
    ["ram", "RAM", "Host memory in use; below: the total"],
    ["disk", "Disk", "Data disk busy time; below: average I/O latency"],
  ];
  const BARS = new Set(["cpu", "ram", "disk"]);
  const cards = new Map();

  function makeCard(id) {
    const c = { bars: {}, m: {}, sub: {} };
    c.root = h("article", "node");
    const head = h("div", "head");
    const name = h("div", "name");
    c.dot = h("i", "dot");
    name.append(c.dot, h("span", null, id));
    c.pill = h("span", "pill");
    head.append(name, c.pill);
    c.tags = h("div", "tags");
    c.hw = h("div", "hw");
    c.note = h("div", "note");
    c.reason = h("div", "reason");
    const metrics = h("div", "metrics");
    for (const [key, label, tip] of METRICS) {
      const m = h("div", "metric");
      const k = h("div", "k", label);
      if (tip) k.title = tip;
      const v = h("div", "v", "—");
      const sub = h("div", "s");
      m.append(k, v, sub);
      c.sub[key] = sub;
      if (BARS.has(key)) {
        const bar = h("div", "bar");
        c.bars[key] = h("i");
        bar.append(c.bars[key]);
        m.append(bar);
      }
      c.m[key] = v;
      metrics.append(m);
    }
    c.metrics = metrics;
    c.sync = h("div", "sync");
    const track = h("div", "track");
    c.syncBar = h("i");
    track.append(c.syncBar);
    c.syncTxt = h("div", "txt");
    c.sync.append(track, c.syncTxt);
    c.spark = h("div", "spark");
    c.svg = document.createElementNS(SVGNS, "svg");
    c.svg.setAttribute("viewBox", "0 0 100 26");
    c.svg.setAttribute("preserveAspectRatio", "none");
    c.sparkLbl = h("span", null, "");
    c.spark.append(c.svg, c.sparkLbl);
    const foot = h("div", "foot-line");
    c.ver = h("span");
    c.seen = h("span");
    foot.append(c.ver, c.seen);
    c.root.append(head, c.tags, c.hw, c.note, c.reason, metrics, c.sync, c.spark, foot);
    return c;
  }

  function setMetric(c, key, main, sub) {
    c.m[key].textContent = main;
    c.sub[key].textContent = sub || "";
  }

  function bar(c, key, r) {
    c.bars[key].style.width = (ok(r) ? Math.max(0, Math.min(1, r)) * 100 : 0).toFixed(1) + "%";
  }

  function hwText(hw) {
    if (!hw) return ["", ""];
    const parts = [];
    if (hw.cpu) parts.push(hw.cpu);
    if (hw.vm && ok(hw.threads)) parts.push(hw.threads + " vCPU");
    else if (ok(hw.cores)) parts.push(hw.cores + "c/" + (ok(hw.threads) ? hw.threads : hw.cores) + "t");
    if (ok(hw.ram_bytes)) parts.push(gib(hw.ram_bytes) + " RAM");
    const disks = (hw.disks || []).map((d) => [d.model, d.kind, ok(d.bytes) ? bytes(d.bytes) : ""].filter(Boolean).join(" "));
    return [parts.join(" · "), disks.join(" + ")];
  }

  function updateCard(c, n, net) {
    const color = colorFor(n.id);
    c.root.className = "node s-" + n.state;
    c.dot.style.background = color;
    c.dot.style.boxShadow = "0 0 10px " + color;
    c.pill.textContent = n.state.replace("_", " ");
    c.pill.title = "since " + utc(n.since) + " UTC";

    c.tags.textContent = "";
    const tags = [];
    if (n.ref) tags.push(["ref", "reference"]);
    tags.push(["", n.tier]);
    tags.push(["", n.position === "lan" ? "operator LAN" : "remote"]);
    if (n.hw && n.hw.vm) tags.push(["", "VM"]);
    for (const [cls, text] of tags) c.tags.append(h("span", "tag " + cls, text));

    const [hw, disk] = hwText(n.hw);
    c.hw.textContent = hw;
    if (disk) c.hw.append(h("div", "disk", disk));
    c.note.textContent = n.note || "";
    c.reason.textContent = n.state === "OK" ? "" : n.reason || "";

    setMetric(c, "height", int(n.height), "");
    setMetric(c, "lag", n.state === "DOWN" ? "—" : int(n.lag), n.lag > 0 ? "blocks" : "");
    setMetric(c, "peers", "↑" + (ok(n.peers_out) ? n.peers_out : "—") + " ↓" + (ok(n.peers_in) ? n.peers_in : "—"), "out / in");
    setMetric(c, "verify", ms(n.block_ms), ok(n.ms_med) ? "med " + ms(n.ms_med) : "");
    setMetric(c, "delay", secs(n.delay_s), ok(n.delay_med) ? "med " + secs(n.delay_med) : "");
    const cover = ok(n.pool_txs) && ok(net.pool_txs) && net.pool_txs > 0 ? n.pool_txs / net.pool_txs : null;
    setMetric(c, "pool", int(n.pool_txs), ok(cover) && !n.ref ? pct(Math.min(cover, 9.99)) + " of ref" : "");
    setMetric(c, "cpu", pct(n.cpu), ok(n.mcpu) ? fixed(n.mcpu, 1) + " cores" : "");
    bar(c, "cpu", n.cpu);
    const used = ok(n.mem_total) && ok(n.mem_avail) && n.mem_total > 0 ? 1 - n.mem_avail / n.mem_total : null;
    setMetric(c, "ram", pct(used), ok(n.mem_total) ? "of " + gib(n.mem_total) : "");
    bar(c, "ram", used);
    setMetric(c, "disk", pct(n.disk_util), ok(n.disk_await_ms) ? fixed(n.disk_await_ms, 1) + " ms I/O" : "");
    bar(c, "disk", n.disk_util);

    if (n.sync) {
      c.sync.hidden = false;
      c.syncBar.style.width = (Math.min(1, n.sync.pct || 0) * 100).toFixed(2) + "%";
      let t = fixed((n.sync.pct || 0) * 100, 2) + "% · " + int(n.height) + " / " + int(n.sync.target);
      if (ok(n.sync.rate_h) && n.sync.rate_h > 0) t += " · " + si(n.sync.rate_h) + " blk/h · ETA " + dur(n.sync.eta_s);
      c.syncTxt.textContent = t;
    } else {
      c.sync.hidden = true;
    }

    drawSpark(c, n.id, color);
    c.ver.textContent = [n.version ? "v" + n.version.replace(/^v/, "") : "", ok(n.uptime_s) ? "up " + dur(n.uptime_s) : ""].filter(Boolean).join(" · ");
    c.seen.textContent = ok(n.age_s) ? "report " + dur(n.age_s) + " ago" : "";
  }

  function drawSpark(c, id, color) {
    const svg = c.svg;
    svg.textContent = "";
    const pb = hist && hist.per_block && hist.per_block[id];
    const vals = pb ? pb.ms.slice(-60).filter(ok) : [];
    if (vals.length < 2) {
      c.sparkLbl.textContent = "";
      c.spark.hidden = true;
      return;
    }
    c.spark.hidden = false;
    const ys = vals.map((v) => Math.log10(Math.max(v, 0.5)));
    const lo = Math.min(...ys), hi = Math.max(...ys);
    const span = hi - lo || 1;
    const pts = ys.map((y, i) => [(i / (ys.length - 1)) * 100, 24 - ((y - lo) / span) * 22]);
    const line = pts.map((p) => p[0].toFixed(2) + "," + p[1].toFixed(2)).join(" ");
    const area = document.createElementNS(SVGNS, "polygon");
    area.setAttribute("points", "0,26 " + line + " 100,26");
    area.setAttribute("fill", color);
    area.setAttribute("fill-opacity", "0.12");
    const pl = document.createElementNS(SVGNS, "polyline");
    pl.setAttribute("points", line);
    pl.setAttribute("fill", "none");
    pl.setAttribute("stroke", color);
    pl.setAttribute("stroke-width", "1.6");
    pl.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(area, pl);
    c.sparkLbl.textContent = "verify, last " + vals.length + " blocks: " + ms(Math.min(...vals)) + "–" + ms(Math.max(...vals));
  }

  function renderNodes(nodes) {
    const box = $("nodes");
    const net = (live && live.network) || {};
    const seen = new Set();
    for (const n of nodes) {
      seen.add(n.id);
      let c = cards.get(n.id);
      if (!c) {
        c = makeCard(n.id);
        cards.set(n.id, c);
      }
      updateCard(c, n, net);
      box.append(c.root);  // keeps server order (moves existing cards)
    }
    for (const [id, c] of cards) {
      if (!seen.has(id)) {
        c.root.remove();
        cards.delete(id);
      }
    }
  }

  // ---------------------------------------------------------------- charts
  const AXIS = {
    stroke: "#8b98a5",
    grid: { stroke: "rgba(255,255,255,0.055)", width: 1 },
    ticks: { stroke: "rgba(255,255,255,0.12)", width: 1, size: 4 },
    font: "11px " + MONO,
  };
  const xAxis = () => Object.assign({}, AXIS, { space: 70 });
  const yAxis = (fmt, extra) => Object.assign({}, AXIS, { size: 62, values: (u, vals) => vals.map(fmt) }, extra || {});
  const timeSeries = { value: (u, v) => (v == null ? "—" : utc(v)) };

  function nodeSeries(order, fmt) {
    return order.map((id) => ({
      label: id,
      stroke: colorFor(id),
      width: 1.4,
      spanGaps: true,
      points: { show: false },
      value: (u, v) => (v == null ? "—" : fmt(v)),
    }));
  }

  // per-node arrays aligned with a shared x
  const pick = (obj, order, key) => order.map((id) => ((obj && obj[id] && obj[id][key]) || []));

  function rollingTps(b, k) {
    const out = new Array(b.t.length).fill(null);
    for (let i = k; i < b.t.length; i++) {
      let n = 0;
      for (let j = i - k + 1; j <= i; j++) n += b.txs[j] || 0;
      const dt = b.t[i] - b.t[i - k];
      out[i] = dt > 0 ? n / dt : null;
    }
    return out;
  }

  const CHARTS = [
    {
      box: "charts-net", key: "blocks", title: "Blocks",
      desc: "Weight of each block (bars) and its transaction count (line).",
      data: (H) => [H.blocks.t, H.blocks.weight, H.blocks.txs],
      opts: () => ({
        scales: { w: { range: (u, lo, hi) => [0, Math.max(1000, hi) * 1.08] }, tx: { range: (u, lo, hi) => [0, Math.max(1, hi) * 1.08] } },
        series: [
          timeSeries,
          { label: "weight", scale: "w", stroke: ACCENT, fill: "rgba(255,106,0,0.35)", width: 1,
            paths: uPlot.paths.bars({ size: [0.7, 40, 1] }), points: { show: false },
            value: (u, v) => (v == null ? "—" : bytes(v)) },
          { label: "transactions", scale: "tx", stroke: "#4cc9f0", width: 1.4, points: { show: false },
            value: (u, v) => (v == null ? "—" : int(v)) },
        ],
        axes: [xAxis(), yAxis(bytes, { scale: "w", size: 66 }), yAxis((v) => si(v), { scale: "tx", side: 1, size: 48, grid: { show: false } })],
      }),
    },
    {
      box: "charts-net", key: "tput", title: "Transaction throughput",
      desc: "Confirmed in blocks (rolling 10 blocks) vs arriving in the reference's pool (per minute).",
      data: (H) => uPlot.join([
        [H.blocks.t, rollingTps(H.blocks, 10)],
        [H.minutes.t, H.minutes.tx_in.map((v) => (v == null ? null : v / 60))],
      ]),
      opts: () => ({
        scales: { y: { range: (u, lo, hi) => [0, Math.max(0.1, hi) * 1.1] } },
        series: [
          timeSeries,
          { label: "confirmed", stroke: ACCENT, width: 2, fill: "rgba(255,106,0,0.09)", spanGaps: true, points: { show: false },
            value: (u, v) => (v == null ? "—" : v.toFixed(2) + " tx/s") },
          { label: "arriving", stroke: "#3ddc97", width: 1.3, spanGaps: true, points: { show: false },
            value: (u, v) => (v == null ? "—" : v.toFixed(2) + " tx/s") },
        ],
        axes: [xAxis(), yAxis((v) => fixed(v, v < 10 ? 1 : 0))],
      }),
    },
    {
      box: "charts-nodes", key: "verify", title: "Block verify time",
      desc: "Time each node took to verify and add each block (log scale).",
      nodes: true,
      data: (H) => [H.blocks.t, ...pick(H.per_block, H.order, "ms").map((a) => a.map((v) => (v == null ? null : Math.max(v, 0.5))))],
      opts: (H) => ({
        scales: { y: { distr: 3 } },
        series: [timeSeries, ...nodeSeries(H.order, ms).map((s) => Object.assign(s, { points: { show: true, size: 3, width: 1 } }))],
        axes: [xAxis(), yAxis(ms, { size: 58 })],
      }),
    },
    {
      box: "charts-nodes", key: "delay", title: "Block arrival delay",
      desc: "Seconds after a reference first saw each block until the node accepted it (axis capped at 10 min).",
      nodes: true,
      data: (H) => [H.blocks.t, ...pick(H.per_block, H.order, "delay")],
      opts: (H) => ({
        scales: { y: { distr: 4, asinh: 0.1, range: (u, lo, hi) => [Math.max(lo == null ? 0 : lo, -10), Math.min(Math.max(hi == null ? 1 : hi, 1), 600)] } },
        series: [timeSeries, ...nodeSeries(H.order, secs).map((s) => Object.assign(s, { points: { show: true, size: 3, width: 1 } }))],
        axes: [xAxis(), yAxis((v) => secs(v), { size: 58 })],
      }),
    },
    {
      box: "charts-nodes", key: "lag", title: "Lag behind the tip",
      desc: "Blocks behind the network tip, per minute (scale compresses large values, so syncing nodes fit).",
      nodes: true,
      data: (H) => [H.minutes.t, ...pick(H.minutes.nodes, H.order, "lag")],
      opts: (H) => ({
        scales: { y: { distr: 4, asinh: 1, range: (u, lo, hi) => [0, Math.max(5, hi)] } },
        series: [timeSeries, ...nodeSeries(H.order, int)],
        axes: [xAxis(), yAxis(si)],
      }),
    },
    {
      box: "charts-nodes", key: "pool", title: "Pool size by node",
      desc: "Transactions each node holds in its pool, per minute. A node that can't keep up with relay holds fewer.",
      nodes: true,
      data: (H) => [H.minutes.t, ...pick(H.minutes.nodes, H.order, "pool")],
      opts: (H) => ({
        scales: { y: { range: (u, lo, hi) => [0, Math.max(10, hi) * 1.08] } },
        series: [timeSeries, ...nodeSeries(H.order, int)],
        axes: [xAxis(), yAxis(si)],
      }),
    },
    {
      box: "charts-nodes", key: "cpu", title: "Host CPU busy",
      desc: "Share of CPU time the node's host spent busy, per minute.",
      nodes: true,
      data: (H) => [H.minutes.t, ...pick(H.minutes.nodes, H.order, "cpu").map((a) => a.map((v) => (v == null ? null : v * 100)))],
      opts: (H) => ({
        scales: { y: { range: [0, 100] } },
        series: [timeSeries, ...nodeSeries(H.order, (v) => Math.round(v) + "%")],
        axes: [xAxis(), yAxis((v) => v + "%", { size: 48 })],
      }),
    },
    {
      box: "charts-nodes", key: "mem", title: "Memory in use",
      desc: "Share of the host's RAM in use (highest in each minute).",
      nodes: true,
      data: (H) => [H.minutes.t, ...pick(H.minutes.nodes, H.order, "mem").map((a) => a.map((v) => (v == null ? null : v * 100)))],
      opts: (H) => ({
        scales: { y: { range: [0, 100] } },
        series: [timeSeries, ...nodeSeries(H.order, (v) => Math.round(v) + "%")],
        axes: [xAxis(), yAxis((v) => v + "%", { size: 48 })],
      }),
    },
  ];

  const HEIGHT = 230;
  const resize = new ResizeObserver((entries) => {
    for (const e of entries) {
      const spec = e.target._spec;
      if (spec && spec.u) spec.u.setSize({ width: Math.floor(e.contentRect.width), height: HEIGHT });
    }
  });

  function buildChartBoxes() {
    for (const spec of CHARTS) {
      const card = h("div", "chart");
      card.append(h("h3", null, spec.title), h("p", null, spec.desc));
      spec.plot = h("div", "plot");
      spec.plot._spec = spec;
      card.append(spec.plot);
      $(spec.box).append(card);
      resize.observe(spec.plot);
    }
  }

  // With the cursor off a chart, its legend shows the latest values.
  function lastIdx(arr) {
    for (let i = arr.length - 1; i >= 0; i--) if (arr[i] != null) return i;
    return null;
  }
  function showLatest(u) {
    if (u.cursor.idx != null || !u.data[0].length) return;
    u.setLegend({ idxs: u.data.map((d, i) => (i === 0 ? d.length - 1 : lastIdx(d))) }, false);
  }
  const LEGEND_HOOKS = { ready: [showLatest], setData: [showLatest], setCursor: [showLatest] };

  function renderCharts() {
    if (!hist) return;
    if (typeof uPlot === "undefined") {
      for (const spec of CHARTS) spec.plot.replaceChildren(h("div", "empty", "Charts could not load in this browser."));
      return;
    }
    hist.order.forEach(colorFor);
    for (const spec of CHARTS) {
      try {
        const data = spec.data(hist);
        const sig = spec.nodes ? hist.order.join(",") : "net";
        if (!spec.u || spec.sig !== sig) {
          if (spec.u) spec.u.destroy();
          spec.plot.textContent = "";
          const opts = Object.assign({
            width: Math.max(280, Math.floor(spec.plot.clientWidth)),
            height: HEIGHT,
            tzDate: (ts) => uPlot.tzDate(new Date(ts * 1000), "Etc/UTC"),
            cursor: { sync: { key: "msnm", scales: ["x", null] }, points: { size: 6 } },
            legend: { live: true },
            hooks: LEGEND_HOOKS,
          }, spec.opts(hist));
          spec.u = new uPlot(opts, data, spec.plot);
          spec.sig = sig;
        } else {
          spec.u.setData(data);
        }
      } catch (e) {
        console.warn("chart " + spec.key, e);
      }
    }
  }

  // ----------------------------------------------------------------- start
  function start() {
    buildChartBoxes();
    tick();
    setInterval(tick, 1000);
    pollLive();
    pollHist();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
