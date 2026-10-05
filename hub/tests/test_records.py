import os

import pytest

from msnm.records import BatchError, ParserState, parse_batch, parse_header

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def test_header():
    h = parse_header("#MSNM1\tseq=42\tcreated=1.5\tsidecar=0.1.0\tboot_id=b\tmonerod_pid=7")
    assert (h.seq, h.created, h.sidecar, h.boot_id, h.monerod_pid) == (42, 1.5, "0.1.0", "b", "7")
    with pytest.raises(BatchError):
        parse_header("hello")
    with pytest.raises(BatchError):
        parse_header("#MSNM1\tcreated=1")


def test_regtest_batch():
    """Real output from monerod v0.19.0.0-beta.3.0 (regtest) via the sidecar."""
    st = ParserState()
    hdr, p = parse_batch(load("batch_regtest.txt"), "n1", st)
    assert p.bad_lines == 0
    assert st.clk_tck == 100
    info = p.rows["info"][0]
    assert info["version"] == "0.19.0.0-beta.3.0-release"
    assert info["rpc_http_code"] == 200
    assert len(info["top_block_hash"]) == 64
    assert p.rows["last_block_header"][0]["major_version"] == 18
    fee = p.rows["fee_estimate"][0]
    assert fee["fee_tier_5"] == 7100000
    assert p.rows["pool_stats"][0]["txs_total"] == 0
    blocks = p.rows["block_added"]
    assert [b["height"] for b in blocks] == [0, 1, 2, 3, 4, 5]
    assert all(len(b["hash"]) == 64 and len(b["pow_hash"]) == 64 for b in blocks)
    timing = p.rows["block_timing"]
    # monerod prints "Height: N" with N = block height + 1
    assert [t["height"] for t in timing] == [1, 2, 3, 4, 5]
    t = timing[-1]
    assert t["total_ms"] == t["block_processing_ms"] + t["addblock_ms"] + t["advance_tree_ms"]
    assert len(t["breakdown_raw"].split("/")) == 13
    proc = p.rows["process_info"][0]
    assert proc["mem_rss"] > 0 and proc["cpu_time_user"] is not None
    host = p.rows["host_sample"][0]
    assert host["mem_total_kb"] > 0 and host["cpu_idle"] > 0 and host["psi_io_some_us"] is not None
    assert p.rows["host_disk"][0]["device"]
    assert any("rotating drive" in w["message"] for w in p.rows["log_warning"])


def _line(msg, level="INFO", cat="blockchain"):
    return f"L\t1791165900.1\t2026-10-05 01:56:43.008\t[P2P1]\t{level}\t{cat}\tsrc/x.cpp:1\t{msg}"


def test_log_events_and_split_block_message():
    st = ParserState()
    h = "a" * 64
    batch1 = "\n".join([
        "#MSNM1\tseq=1",
        _line("+++++ BLOCK SUCCESSFULLY ADDED"),
        _line(f"id:\t<{h}>"),
    ])
    batch2 = "\n".join([
        "#MSNM1\tseq=2",
        _line(f"PoW:\t<{'b' * 64}>"),
        _line("HEIGHT 3102801, difficulty:\t12345"),
        _line("block reward: 0.6(0.6 + 0.000123), coinbase_weight: 103, cumulative weight: 297000, 950(2/30)ms"),
        _line("Height: 3102802 coinbase weight: 103 cumm: 297000 p/t: 950 (2/30/0/1/0/3/40/800/5/2/1/60/120)ms"),
        _line(f"Transaction added to pool: txid <{'0' * 64}> weight: 3500 fee/byte: 2.5e-05, count: 812, pool total weight: 2900000", cat="txpool"),
        _line("###### REORGANIZE on height: 3102790 of 3102801 with cum_difficulty 1"),
        _line("REORGANIZE SUCCESS! on height: 3102790, new blockchain size: 3102803"),
        _line("----- BLOCK ADDED AS ALTERNATIVE ON HEIGHT 3102801"),
        _line(f"id:\t<{'c' * 64}>"),
        _line("Failed to parse block", level="ERROR", cat="cn"),
    ])
    _, p1 = parse_batch(batch1, "n", st)
    assert "block_added" not in p1.rows        # message continues in the next batch
    _, p2 = parse_batch(batch2, "n", st)
    b = p2.rows["block_added"][0]
    assert (b["height"], b["hash"], b["block_processing_ms"], b["fee"]) == (3102801, h, 950.0, 0.000123)
    t = p2.rows["block_timing"][0]
    assert t["height"] == 3102801 and t["t_checktx_ms"] == 800 and t["advance_tree_ms"] == 120
    assert t["total_ms"] == 950 + 60 + 120
    tx = p2.rows["tx_pool_log"][0]
    assert tx["weight"] == 3500 and tx["pool_count"] == 812
    kinds = [e["kind"] for e in p2.rows["chain_event_log"]]
    assert kinds == ["reorganize", "reorganize_success", "alt_block"]
    assert p2.rows["chain_event_log"][2]["hash"] == "c" * 64
    assert p2.rows["log_warning"][0]["level"] == "ERROR"


def test_rpc_failures_become_rpc_error_rows():
    st = ParserState()
    text = "\n".join([
        "#MSNM1\tseq=1",
        "R\t1.0\tget_info\t000\t10.0\t",
        'R\t1.0\tget_connections\t200\t0.01\t{"id":"0","jsonrpc":"2.0","error":{"code":-1,"message":"denied"}}',
        "R\t1.0\tget_bans\t200\t0.01\t{not json",
    ])
    _, p = parse_batch(text, "n", st)
    errs = {e["method"]: e for e in p.rows["rpc_error"]}
    assert errs["get_info"]["error"] == "no response"
    assert "denied" in errs["get_connections"]["error"]
    assert errs["get_bans"]["error"].startswith("bad json")
    assert "info" not in p.rows
