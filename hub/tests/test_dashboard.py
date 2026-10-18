import datetime as dt
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from msnm.chain import Chain, RefConfig
from msnm.config import load
from msnm import cpudb
from msnm.dashboard import ID_ALPHABET, Dashboard, DashboardConfig, History, hardware, load_history, public_id
from msnm.live import Monitor, Thresholds
from msnm.registry import Registry
from msnm.tables import ParquetSink

UTC = dt.timezone.utc
PROFILE = {
    "cpu_model": "Intel(R) Core(TM)2 Quad CPU    Q6600  @ 2.40GHz",
    "cpu_physical_cores": "4", "cpu_logical": "4", "cpu_max_khz": "2394000",
    "arch": "x86_64", "cpu_flags": "ssse3",
    "cpu_flags_checked": "ssse3,sse4_1,sse4_2,aes,avx,avx2,avx512f,sha_ni,bmi2,adx,asimd,sha2,pmull",
    "cpu_cache": "l1d=32K l1dn=4 l1i=32K l1in=4 l2=4096K l2n=2",
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
INTERNAL = ("vol-01", "ref-a", "hidden", "volunteer", "operator")   # node ids and tiers stay on the hub
SECRET = "test-secret-0123456789"


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
    dcfg = DashboardConfig(enabled=True, **({"hide": ["hidden"], "id_secret": SECRET} | cfg))
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
    assert (hw["codename"], hw["gen"], hw["process"], hw["year"]) == ("Kentsfield", "Core", "65 nm", 2007)
    assert (hw["l2_bytes"], hw["l3_bytes"], hw["cache_src"]) == (8 << 20, None, "measured")
    assert [x["n"] for x in hw["isa"] if x["on"]] == ["SSSE3"] and len(hw["isa"]) == 10   # every flag checked
    vm = hardware({"virtualized": "yes", "data_devices": "vda", "disk.vda": "rotational=1 sectors=8 model="})
    assert vm["vm"] and vm["disks"][0]["kind"] == "virtual disk"
    xeon = hardware({"virtualized": "yes", "cpu_model": "Intel(R) Xeon(R) Silver 4116 CPU @ 2.10GHz",
                     "cpu_cache": "l2=4096K l2n=6 l3=16384K l3n=6", "cpu_flags": "aes,avx"})
    # in a VM the reported cache is the hypervisor's: the chip spec is used
    assert (xeon["codename"], xeon["l3_bytes"], xeon["cache_src"]) == ("Skylake-SP", 16896 << 10, "spec")
    # an old sidecar (no cpu_flags_checked) checked fewer flags
    assert [x["n"] for x in xeon["isa"]] == ["AES-NI", "AVX", "AVX2", "AVX-512", "SHA", "BMI2", "ADX"]
    qemu = hardware({"cpu_model": "Intel Core Processor (Haswell, no TSX, IBRS)", "virtualized": "yes"})
    assert qemu["virtual_cpu"] and qemu["gen"] == "virtual CPU (Haswell feature set)" and qemu["l3_bytes"] is None
    assert hardware({})["isa"] == [] and hardware({})["gen"] == ""
    assert not any(s in json.dumps(hw) for s in SECRETS)
    assert hardware({"cpu_model": "<script>alert(1)</script>"})["cpu"] == "scriptalert(1)/script"


def test_live_publishes_allow_listed_fields_only(tmp_path):
    dash, mon, chain, reg, hist, now = _world(tmp_path)
    for nid in ("ref-a", "vol-01", "hidden"):
        _node(mon, reg, nid, 100)
    mon.evaluate()
    live = dash.build_live(time.time())
    blob = json.dumps(live)
    assert [n["id"] for n in live["nodes"]] == [dash.public("ref-a"), dash.public("vol-01")]   # refs first; no hidden
    assert not any(s in blob for s in SECRETS + INTERNAL)
    net = live["network"]
    assert (net["height"], net["pool_txs"], net["weight_median"], net["hashrate"]) == (100, 7, 300000, 10)
    assert [b["h"] for b in live["blocks"]][:2] == [100, 99]
    v = live["nodes"][1]
    assert (v["state"], v["lag"], v["ref"]) == ("OK", 0, False) and not {"tier", "position", "note"} & set(v)
    assert len(v["id"]) == 4 and v["id"] != live["nodes"][0]["id"]
    assert v["hw"]["cpu"].startswith("Intel Core2 Quad") and v["sync"] is None
    mon.nodes["vol-01"].profile = {}
    hist.profiles["vol-01"] = {"cpu_model": "From Parquet"}
    assert dash.build_live(time.time())["nodes"][1]["hw"]["cpu"] == "From Parquet"
    reg.revoke("vol-01")
    assert [n["id"] for n in dash.build_live(time.time())["nodes"]] == [dash.public("ref-a")]


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
    pid = dash.public("vol-01")
    assert out["per_block"][pid]["ms"][i] == 812.5
    assert out["per_block"][pid]["delay"][i] == 1.0   # 1.5 s after first seen, less the 0.5 s clock offset
    assert not hist.block_ms["other"]                       # far below the tip: not kept
    mins = out["minutes"]
    k = mins["t"].index(m * 60)
    s = mins["nodes"][pid]
    assert (s["lag"][k], s["pool"][k], s["cpu"][k], s["mem"][k]) == (1, 11, 0.5, 0.75)
    assert mins["tx_in"][k] == 8 and mins["tx_in"][k - 1] is None and mins["tx_in"][-1] is None
    assert len(mins["t"]) == 1440 and out["order"] == [pid]
    assert not any(x in json.dumps(out) for x in INTERNAL)
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
    sink.add("info", {"node_id": "n", "time": _t(base + 1), "height": 101, "tx_pool_size": 4,
                      "rpc_response_time": 0.004})
    sink.add("rpc_error", {"node_id": "n", "time": _t(base + 2), "method": "get_info", "rpc_http_code": 0,
                           "rpc_response_time": 10.0, "error": "no response"})
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
    assert [x[1:] for x in old.rpc["n"]] == [(0.004, False), (10.0, True)]
    assert old.minutes["n"][m][6:] == [10.0, 1]   # slowest get_info in the minute, timeouts
    assert old.accepted["n"][100][1] == pytest.approx(base + 2.0)   # log time less the clock offset
    assert old.tx_in["ref-a"] == {m: 3}
    assert old.profiles["n"]["cpu_model"] == "New"   # the latest, however old
    live = History()
    live.on_zmq_tx("ref-a", 2, base)
    live.merge(old)
    assert live.tx_in["ref-a"][m] == 5 and live.block_ms["n"][100] == 33.0 and len(live.rpc["n"]) == 2


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
        assert (await r.json())["nodes"][0]["id"] == dash.public("ref-a")
        r = await client.get("/api/history.json")
        assert (await r.json())["order"] == [dash.public("ref-a")]
        for bad in ("/static/index.html", "/static/..%2Fdashboard.py", "/v1/ping", "/metrics", "/api/nope"):
            r = await client.get(bad)
            assert r.status == 404, bad
            assert "Content-Security-Policy" in r.headers
        assert (await client.post("/api/live.json")).status == 405


def test_config(tmp_path):
    p = tmp_path / "hub.yaml"
    p.write_text("dashboard:\n  enabled: true\n  id_secret: 'a-test-secret-1234'\n  port: 8800\n  hide: [x]\n"
                 "  links: [{label: Code, url: 'https://example.org/repo'}]\n")
    cfg = load(str(p))
    assert cfg.dashboard.enabled and cfg.dashboard.port == 8800 and cfg.dashboard.hide == ["x"]
    p.write_text("dashboard:\n  links: [{label: x, url: 'javascript:alert(1)'}]\n")
    with pytest.raises(ValueError):
        load(str(p))
    p.write_text("dashboard:\n  enabled: true\n")   # no id_secret
    with pytest.raises(ValueError):
        load(str(p))
    p.write_text("dashboard:\n  enabled: true\n  id_secret: 'a-test-secret-1234'\n  aliases: {x: '<b>'}\n")
    with pytest.raises(ValueError):
        load(str(p))


def test_public_ids(tmp_path, monkeypatch):
    a = public_id("secret-one-0123456", "vol-01")
    assert a == public_id("secret-one-0123456", "vol-01") and len(a) == 4 and set(a) <= set(ID_ALPHABET)
    assert a != public_id("secret-two-0123456", "vol-01")
    dash, mon, chain, reg, hist, now = _world(tmp_path, aliases={"vol-01": "VOL1"})
    assert dash.public("vol-01") == "VOL1"
    # two hashes that collide both get a longer ID; an alias is kept
    monkeypatch.setattr("msnm.dashboard.public_id", lambda s, nid, n=4: "SAME" if n == 4 else nid.upper().ljust(n, "Z"))
    dash._id_cache = ((), {})
    ids = dash._ids()
    assert ids["vol-01"] == "VOL1" and (ids["ref-a"], ids["hidden"]) == ("REF-AZZZ", "HIDDENZZ")


def test_cpudb():
    cases = {
        "Intel(R) Pentium(R) CPU        G6950  @ 2.80GHz": ("Clarkdale", "32 nm", 2010),
        "AMD Ryzen 9 3900X 12-Core Processor": ("Matisse", "7 nm", 2019),
        "AMD Ryzen 9 5900XT 16-Core Processor": ("Vermeer", "7 nm", 2024),
        "Intel(R) Xeon(R) CPU E5-2690 v4 @ 2.60GHz": ("Broadwell-EP", "14 nm", 2016),
        "AMD EPYC 7551 32-Core Processor": ("Naples", "14 nm", 2017),
        "Intel(R) Core(TM) i7-4770 CPU @ 3.40GHz": (None, "22 nm", 2013),
        "Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz": (None, "14 nm", None),
        "Intel(R) Xeon(R) Gold 6230 CPU @ 2.10GHz": ("Cascade Lake-SP", "14 nm", 2019),
        "AMD Ryzen 5 5600G with Radeon Graphics": ("Cezanne", "7 nm", 2021),
        "AMD EPYC-Rome Processor": (None, None, None),
        "AMD EPYC 7B13 64-Core Processor": None,
        "AMD Ryzen 7 5700U with Radeon Graphics": None,
        "Some Future CPU": None,
    }
    for model, want in cases.items():
        spec = cpudb.lookup(model)
        assert (None if spec is None else (spec.codename, spec.process, spec.year)) == want, model
    assert cpudb.lookup("Intel(R) Xeon(R) CPU E5-2690 v4 @ 2.60GHz").l3_kb == 35840   # exact model: cache too
    assert cpudb.lookup("AMD EPYC-Rome Processor").virtual
    assert cpudb.isa("aarch64", "asimd,aes", None) == [
        {"n": "NEON", "on": True}, {"n": "AES", "on": True}, {"n": "SHA2", "on": False}, {"n": "PMULL", "on": False}]
    assert cpudb.parse_cache("l1d=32K l1dn=4 l2=512K l2n=12 l3=16384K l3n=2") == {"l2": 6 << 20, "l3": 32 << 20}
    assert cpudb.parse_cache("l2=bogus") == {}


def test_rpc_times(tmp_path):
    dash, mon, chain, reg, hist, now = _world(tmp_path)
    _node(mon, reg, "vol-01", 100)
    mon.evaluate()
    t0 = now - 600
    info = [{"time": _t(t0 + 30 * i), "height": 101, "rpc_http_code": 200, "rpc_response_time": 0.002 * (i + 1)}
            for i in range(8)]
    errors = [{"time": _t(t0 + 30 * i + 15), "method": "get_info", "rpc_http_code": 0, "rpc_response_time": 10.01}
              for i in range(2)]
    errors.append({"time": _t(t0 + 5), "method": "get_info", "rpc_http_code": 0, "rpc_response_time": 0.001})
    errors.append({"time": _t(t0 + 6), "method": "get_fee_estimate", "rpc_http_code": 0, "rpc_response_time": 10.0})
    hist.record("vol-01", {"info": info, "rpc_error": errors}, offset=0, tip=100)
    r = dash.build_live(now)["nodes"][0]["rpc"]
    # 8 answers (2..16 ms) and 2 timeouts that count as the slowest; the
    # refused call (down, not slow) and other methods are left out
    assert (r["n"], r["timeouts"], r["limit_ms"], r["slow"]) == (10, 2, 10000, True)
    assert (r["p50_ms"], r["p95_ms"]) == (10.0, 10000)
    s = dash.build_history(now)["minutes"]["nodes"][dash.public("vol-01")]["rpc"]
    assert 10000 in s and all(v is None or 2.0 <= v <= 10000 for v in s)
    fast = History()
    fast.record("n", {"info": info}, offset=0, tip=None)
    assert fast.rpc_stats("n", now)["slow"] is False and fast.rpc_stats("n", now + 2000) is None


def test_slow_rpc_keeps_state_until_no_answer_for_5_min(tmp_path):
    dash, mon, chain, reg, hist, now = _world(tmp_path)
    nl = _node(mon, reg, "vol-01", 100)
    nl.info_fail_streak = 3              # missed some polls, but answered within 5 min
    mon.evaluate()
    assert nl.state == "OK"
    nl.info_time = nl.last_batch - 400   # then no answer for over 5 min
    mon.evaluate()
    assert nl.state == "UNRESPONSIVE" and nl.reason.startswith("no get_info answer for ")
    new = mon.live(next(n for n in reg.all() if n.node_id == "ref-a"))
    new.last_batch, new.info_fail_streak = time.time(), 2   # never answered at all
    mon.evaluate()
    assert (new.state, new.reason) == ("UNRESPONSIVE", "get_info failed 2x")


def test_sidecar_versions(tmp_path):
    from msnm.dashboard import sidecar_version, version_newer
    assert sidecar_version(" 0.1.6 ") == "0.1.6" and sidecar_version("0.1.6<b>") is None and sidecar_version(None) is None
    assert version_newer("0.1.10", "0.1.9") and not version_newer("0.1.5", "0.1.5") and not version_newer(None, "0.1.5")
    dash, mon, chain, reg, hist, now = _world(tmp_path)
    dash.sidecar_latest = "0.1.6"
    for nid, v in (("ref-a", "0.1.6"), ("vol-01", "0.1.3")):
        _node(mon, reg, nid, 100).profile["sidecar_version"] = v
    mon.evaluate()
    live = dash.build_live(time.time())
    assert live["sidecar_latest"] == "0.1.6"
    assert [(n["sidecar"], n["sidecar_update"]) for n in live["nodes"]] == [("0.1.6", False), ("0.1.3", True)]
