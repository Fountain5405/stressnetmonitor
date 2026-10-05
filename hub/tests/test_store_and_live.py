import os
import time

import pyarrow.dataset as ds

from msnm.chain import Chain, RefConfig
from msnm.live import Monitor, Thresholds
from msnm.rawstore import RawStore, verify_ledger
from msnm.records import ParserState, parse_batch
from msnm.registry import Registry
from msnm.tables import ParquetSink

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def test_registry_tokens(tmp_path):
    reg = Registry(str(tmp_path / "r.sqlite"))
    tok = reg.add("node-a", "volunteer", "remote", "note")
    assert reg.by_token(tok).node_id == "node-a"
    assert reg.by_token("nope") is None
    new = reg.rotate("node-a")
    reg._cache_time = 0
    assert reg.by_token(tok) is None and reg.by_token(new).node_id == "node-a"
    reg.revoke("node-a")
    assert reg.by_token(new) is None


def test_rawstore_dedup_and_ledger(tmp_path):
    rs = RawStore(str(tmp_path))
    meta = {"recv_time": time.time()}
    assert rs.save_batch("n", 1, b"abc", True, dict(meta))[0] is True
    assert rs.save_batch("n", 1, b"abc", True, dict(meta))[0] is False   # retry
    assert rs.save_batch("n", 1, b"xyz", True, dict(meta))[0] is True    # same seq, new content
    rs.save_bundle("n", "bundle.1.crash.tar.gz", b"tgz", dict(meta))
    assert verify_ledger(str(tmp_path)) == []
    # tampering with an archived file is detected
    e = next(rs.iter_ledger())
    with open(os.path.join(rs.raw, e["path"]), "wb") as f:
        f.write(b"changed")
    assert any("content changed" in p for p in verify_ledger(str(tmp_path)))


def test_parquet_roundtrip(tmp_path):
    sink = ParquetSink(str(tmp_path))
    with open(os.path.join(FIX, "batch_regtest.txt")) as f:
        _, p = parse_batch(f.read(), "n1", ParserState())
    sink.extend(p.rows)
    assert sink.flush() > 0
    info = ds.dataset(str(tmp_path / "info"), format="parquet", partitioning="hive").to_table()
    assert info.schema.field("height").type == "int32"
    assert str(info.schema.field("time").type) == "timestamp[us, tz=UTC]"
    assert info.column("node_id").to_pylist() == ["n1"]


def _monitor(tmp_path, canon_height=100):
    sink = ParquetSink(str(tmp_path))
    chain = Chain([RefConfig("ref", "http://x")], sink)
    ref = chain.refs["ref"]
    ref.top_height, ref.top_hash, ref.last_ok = canon_height, "f" * 64, time.time()
    ref.hashes = {h: f"{h:064x}" for h in range(canon_height + 1)}
    th = Thresholds(fork_confirm_s=0, silent_after_s=60)
    return Monitor(chain, sink, th, fork_height=50, min_major_version=17), chain


def _info_batch(height, top_hash, major=17, t=None):
    t = t or time.time()
    return "\n".join([
        "#MSNM1\tseq=1",
        f'R\t{t}\tget_info\t200\t0.01\t{{"result":{{"height":{height + 1},"top_block_hash":"{top_hash}",'
        f'"busy_syncing":false,"synchronized":true,"status":"OK"}}}}',
        f'R\t{t}\tget_last_block_header\t200\t0.01\t{{"result":{{"block_header":{{"height":{height},'
        f'"major_version":{major}}},"status":"OK"}}}}',
    ])


def _feed(mon, node, text):
    hdr, p = parse_batch(text, node.node_id, ParserState())
    mon.ingest(node, hdr, p, time.time(), time.time(), 100)


def test_node_states(tmp_path):
    from msnm.registry import Node
    mon, chain = _monitor(tmp_path)
    node = Node("a", "operator", "lan", "", 0, None)
    _feed(mon, node, _info_batch(100, f"{100:064x}"))
    mon.evaluate()
    assert mon.nodes["a"].state == "OK"
    _feed(mon, node, _info_batch(95, f"{95:064x}"))
    mon.evaluate()
    assert mon.nodes["a"].state == "BEHIND"
    _feed(mon, node, _info_batch(100, "e" * 64))
    mon.evaluate()
    assert mon.nodes["a"].state == "FORKED"
    _feed(mon, node, _info_batch(100, f"{100:064x}", major=16))
    mon.evaluate()
    assert mon.nodes["a"].state == "WRONG_CHAIN"
    mon.nodes["a"].last_batch -= 120
    mon.evaluate()
    assert mon.nodes["a"].state == "DOWN"
    # every transition was recorded
    assert len(mon.sink._rows["node_state"]) == 5
