import datetime as dt
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from msnm.chain import Chain, RefConfig
from msnm.config import load
from msnm.dashboard import Dashboard, DashboardConfig, History, hardware, load_history
from msnm.live import Monitor, Thresholds
from msnm.registry import Registry
from msnm.tables import ParquetSink

UTC = dt.timezone.utc
PROFILE = {
    "cpu_model": "Intel(R) Core(TM)2 Quad CPU    Q6600  @ 2.40GHz",
    "cpu_physical_cores": "4", "cpu_logical": "4", "cpu_max_khz": "2394000",
    "mem_total_kb": "4004352", "swap_total_kb": "4007932", "virtualized": "no",
    "data_devices": "dm-1 sda sdb",
    "disk.dm-1": "rotational=0 sectors=937705472 model=",
    "disk.sda": "rotational=0 sectors=488397168 model=Samsung_SSD_850",
    "disk.sdb": "rotational=1 sectors=625142448 model=WDC_WD3200AAKS-7",
    # never published
    "monerod_args": "--add-exclusive-node 192.0.2.25:28080 --data-dir /home/someone/monero",
    "data_dir": "/home/someone/monero",
    "monerod_unit": "ExecStart=/opt/monero/monerod",
}
SECRETS = ("192.0.2.", "198.51.100.", "secret-note", "myhost", "/home", "exclusive-node", "ExecStart")


def _t(x: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(x, UTC)


def _world(tmp_path, tip=100, **cfg):
    sink = ParquetSink(str(tmp_path / "parquet"))
    chain = Chain([RefConfig("ref-a", "http://x")], sink)
    ref = chain.refs["ref-a"]
    now = time.time()
    ref.top_height, ref.top_hash, ref.last_ok = tip, f"{tip:064x}", now
    for h in range(tip - 9, tip + 1):
        bh, t = f"{h:064x}", now - (tip - h) * 120
        ref.hashes[h] = bh
        ref.headers[h] = {"height": h, "hash": bh, "timestamp": int(t), "block_size": 1000, "block_weight": 2000,
                          "num_txes": 5, "difficulty": 1200, "major_version": 18}
        chain.first_seen[(h, bh)] = t
    reg = Registry(str(tmp_path / "r.sqlite"))
    for nid, tier in (("vol-01", "volunteer"), ("ref-a", "operator"), ("hidden", "operator")):
        reg.add(nid, tier, "lan", note="secret-note 198.51.100.99 myhost")
    mon = Monitor(chain, sink, Thresholds(fork_confirm_s=0, silent_after_s=60), fork_height=50, min_major_version=17)
    hist = History()
    dcfg = DashboardConfig(enabled=True, **({"hide": ["hidden"], "notes": {"vol-01": "a note"}} | cfg))
    return Dashboard(dcfg, mon, chain, hist, reg, ["ref-a"]), mon, chain, reg, hist, now


def _node(mon, reg, nid, top):
    nl = mon.live(next(n for n in reg.all() if n.node_id == nid))
    nl.last_batch = nl.info_time = time.time()
    nl.info = {"height": top + 1, "tx_pool_size": 7, "outgoing_connections_count": 8,
               "incoming_connections_count": 2, "version": "0.19.0.0-beta.3.0-release",
               "start_time": time.time() - 3600, "synchronized": True, "top_block_hash": f"{top:064x}",
               "block_weight_median": 300000, "block_weight_limit": 600000}
    nl.profile = dict(PROFILE)
    return nl


def test_hardware_allow_list():
    hw = hardware(PROFILE)
    assert hw["cpu"] == "Intel Core2 Quad Q6600 @ 2.40GHz"
    assert (hw["cores"], hw["threads"], hw["mhz"]) == (4, 4, 2394)
    assert hw["ram_bytes"] == 4004352 * 1024
    assert [d["kind"] for d in hw["disks"]] == ["SSD", "HDD"]   # the LVM device is skipped
    assert hw["disks"][0]["model"] == "Samsung SSD 850"
    vm = hardware({"virtualized": "yes", "data_devices": "vda", "disk.vda": "rotational=1 sectors=8 model="})
    assert vm["vm"] and vm["disks"][0]["kind"] == "virtual disk"
    assert not any(s in json.dumps(hw) for s in SECRETS)
    assert hardware({"cpu_model": "<script>alert(1)</script>"})["cpu"] == "scriptalert(1)/script"


def test_live_publishes_allow_listed_fields_only(tmp_path):
    dash, mon, chain, reg, hist, now = _world(tmp_path)
    for nid in ("ref-a", "vol-01", "hidden"):
        _node(mon, reg, nid, 100)
    mon.evaluate()
    live = dash.build_live(time.time())
    blob = json.dumps(live)
    assert [n["id"] for n in live["nodes"]] == ["ref-a", "vol-01"]   # references first; hidden left out
    assert not any(s in blob for s in SECRETS)
    net = live["network"]
    assert (net["height"], net["pool_txs"], net["weight_median"], net["hashrate"]) == (100, 7, 300000, 10)
    assert [b["h"] for b in live["blocks"]][:2] == [100, 99]
    v = live["nodes"][1]
    assert (v["state"], v["lag"], v["note"], v["tier"]) == ("OK", 0, "a note", "volunteer")
    assert v["hw"]["cpu"].startswith("Intel Core2 Quad") and v["sync"] is None
    mon.nodes["vol-01"].profile = {}
    hist.profiles["vol-01"] = {"cpu_model": "From Parquet"}
    assert dash.build_live(time.time())["nodes"][1]["hw"]["cpu"] == "From Parquet"
    reg.revoke("vol-01")
    assert [n["id"] for n in dash.build_live(time.time())["nodes"]] == ["ref-a"]


def test_history_series(tmp_path):
    dash, mon, chain, reg, hist, now = _world(tmp_path)
    _node(mon, reg, "vol-01", 100)
    mon.evaluate()
    sampled = chain.first_seen[(99, f"{99:064x}")] + 10   # block 99 is the tip; the node reports 98
    m = int(sampled // 60)
    base = m * 60 + 1
    zero = {k: 0 for k in ("cpu_nice", "cpu_system", "cpu_iowait", "cpu_irq", "cpu_softirq", "cpu_steal")}
    seen100 = chain.first_seen[(100, f"{100:064x}")]
    hist.record("vol-01", {
        "info": [{"time": _t(sampled + 0.5), "height": 99, "tx_pool_size": 11}],   # node clock 0.5 s ahead
        "host_sample": [
            {"time": _t(base), "cpu_user": 100, "cpu_idle": 300, **zero, "mem_total_kb": 1000, "mem_avail_kb": 250},
            {"time": _t(base + 20), "cpu_user": 150, "cpu_idle": 350, **zero, "mem_total_kb": 1000, "mem_avail_kb": 400},
        ],
        "block_timing": [{"height": 100, "total_ms": 812.5}],
        "block_added": [{"height": 100, "hash": f"{100:064x}", "log_time": _t(seen100 + 1.5)}],
    }, offset=0.5, tip=100)
    hist.record("other", {"block_timing": [{"height": 5, "total_ms": 1.0}]}, offset=0, tip=5000)
    hist.on_zmq_tx("ref-a", 5, base)
    hist.on_zmq_tx("ref-a", 3, base + 5)
    out = dash.build_history(now)
    i = out["blocks"]["h"].index(100)
    assert out["per_block"]["vol-01"]["ms"][i] == 812.5
    assert out["per_block"]["vol-01"]["delay"][i] == 1.0   # 1.5 s after first seen, less the 0.5 s clock offset
    assert not hist.block_ms["other"]                       # far below the tip: not kept
    mins = out["minutes"]
    k = mins["t"].index(m * 60)
    s = mins["nodes"]["vol-01"]
    assert (s["lag"][k], s["pool"][k], s["cpu"][k], s["mem"][k]) == (1, 11, 0.5, 0.75)
    assert mins["tx_in"][k] == 8 and mins["tx_in"][k - 1] is None and mins["tx_in"][-1] is None
    assert len(mins["t"]) == 1440 and out["order"] == ["vol-01"]
    live = dash.build_live(now)
    assert live["nodes"][0]["ms_med"] == 812.5 and live["network"]["tx_in_per_min"] == 0.8


def test_load_history_from_parquet(tmp_path):
    root = tmp_path / "parquet"
    sink = ParquetSink(str(root))
    now = time.time()
    m = int(now // 60) - 3
    base = m * 60 + 5
    bh = f"{100:064x}"
    sink.add("ref_block", {"ref_id": "ref-a", "first_seen": _t(base), "height": 100, "hash": bh, "backfilled": False})
    sink.add("info", {"node_id": "n", "time": _t(base + 1), "height": 101, "tx_pool_size": 4})
    sink.add("batch", {"node_id": "n", "time": _t(base + 1), "seq": 1, "clock_offset_s": 0.25})
    sink.add("block_timing", {"node_id": "n", "time": _t(base + 2), "height": 100, "total_ms": 33.0})
    sink.add("block_added", {"node_id": "n", "time": _t(base + 2), "log_time": _t(base + 2.25), "height": 100,
                             "hash": bh})
    for x in range(3):
        sink.add("zmq_tx", {"node_id": "ref-a", "recv_time": _t(base + x), "txid": str(x)})
    sink.add("info", {"node_id": "n", "time": _t(now - 3 * 86400), "height": 7})   # outside the window
    sink.add("node_profile", {"node_id": "n", "time": _t(now - 5 * 86400), "key": "cpu_model", "value": "Old"})
    sink.add("node_profile", {"node_id": "n", "time": _t(now - 86400), "key": "cpu_model", "value": "New"})
    sink.flush()
    old = load_history(str(root), until=now)
    assert old.seen[(100, bh)] == pytest.approx(base)
    assert old.minutes["n"][m][:2] == [100, 4] and len(old.minutes["n"]) == 1
    assert old.minutes["n"][m][5] == pytest.approx(base + 0.75)   # sampled at base + 1, node clock 0.25 s ahead
    assert old.block_ms["n"][100] == 33.0
    assert old.accepted["n"][100][1] == pytest.approx(base + 2.0)   # log time less the clock offset
    assert old.tx_in["ref-a"] == {m: 3}
    assert old.profiles["n"]["cpu_model"] == "New"   # the latest, however old
    live = History()
    live.on_zmq_tx("ref-a", 2, base)
    live.merge(old)
    assert live.tx_in["ref-a"][m] == 5 and live.block_ms["n"][100] == 33.0


async def test_http(tmp_path):
    dash, mon, chain, reg, hist, now = _world(tmp_path, title="Test <net>")
    _node(mon, reg, "ref-a", 100)
    mon.evaluate()
    async with TestClient(TestServer(dash.app())) as client:
        r = await client.get("/")
        assert r.status == 200 and "default-src 'none'" in r.headers["Content-Security-Policy"]
        page = await r.text()
        assert "{{" not in page and "/static/app.js?v=" in page and "Test &lt;net&gt;" in page
        r = await client.get("/static/app.js")
        assert r.status == 200 and r.headers["Content-Type"].startswith("text/javascript")
        r = await client.get("/api/live.json", headers={"Accept-Encoding": "gzip"})
        assert r.headers["Content-Encoding"] == "gzip" and r.headers["Cache-Control"] == "public, max-age=2"
        assert (await r.json())["nodes"][0]["id"] == "ref-a"
        r = await client.get("/api/history.json")
        assert (await r.json())["order"] == ["ref-a"]
        for bad in ("/static/index.html", "/static/..%2Fdashboard.py", "/v1/ping", "/metrics", "/api/nope"):
            r = await client.get(bad)
            assert r.status == 404, bad
            assert "Content-Security-Policy" in r.headers
        assert (await client.post("/api/live.json")).status == 405


def test_config(tmp_path):
    p = tmp_path / "hub.yaml"
    p.write_text("dashboard:\n  enabled: true\n  port: 8800\n  hide: [x]\n"
                 "  links: [{label: Code, url: 'https://example.org/repo'}]\n")
    cfg = load(str(p))
    assert cfg.dashboard.enabled and cfg.dashboard.port == 8800 and cfg.dashboard.hide == ["x"]
    p.write_text("dashboard:\n  links: [{label: x, url: 'javascript:alert(1)'}]\n")
    with pytest.raises(ValueError):
        load(str(p))
