import time

import pytest

from msnm.config import load
from msnm.registry import Registry


def _reg(tmp_path) -> Registry:
    reg = Registry(str(tmp_path / "r.sqlite"))
    reg.add("n1", "volunteer", "lan")
    return reg


def test_set_build_validation(tmp_path):
    reg = _reg(tmp_path)
    for bad in ("Upper", "-x", "a b", "", "x" * 25, "a_b"):
        with pytest.raises(ValueError):
            reg.set_build("n1", [bad])
    with pytest.raises(ValueError):
        reg.set_build("n1", [f"m{i}" for i in range(9)])
    with pytest.raises(KeyError):
        reg.set_build("nope", ["a"])
    with pytest.raises(ValueError):
        reg.set_build("n1", ["a"], time.time() + 120)
    reg.set_build("n1", ["a"], time.time() + 30)    # small clock skew is fine
    assert reg.builds()[0].mods == ("a",)
    with pytest.raises(KeyError):
        reg.remove_build("n1", 1.0)


def test_history_order_and_build_at(tmp_path):
    reg = _reg(tmp_path)
    reg.add("n0", "volunteer", "lan")
    reg.set_build("n1", ["b", "a", "a"], 200)
    reg.set_build("n1", [], 300)
    reg.set_build("n1", ["c"], 100)
    reg.set_build("n0", ["z"], 50)
    assert [(b.node_id, b.since, b.mods) for b in reg.builds()] == [
        ("n0", 50, ("z",)), ("n1", 100, ("c",)), ("n1", 200, ("a", "b")), ("n1", 300, ())]
    assert [b.since for b in reg.builds("n1")] == [100, 200, 300]
    assert [reg.build_at("n1", t) for t in (99, 100, 250, 300, 9999)] == [(), ("c",), ("a", "b"), (), ()]
    reg.set_build("n1", ["d"], 200)    # same key replaces
    assert reg.build_at("n1", 250) == ("d",)
    reg.remove_build("n1", 200)
    assert reg.build_at("n1", 250) == ("c",)


def test_config_mods(tmp_path):
    p = tmp_path / "hub.yaml"
    p.write_text("")
    assert load(str(p)).mods == {}
    p.write_text("mods:\n  lowmem-v2: lower-memory proof verification\n")
    assert load(str(p)).mods == {"lowmem-v2": "lower-memory proof verification"}
    for bad in ("{ok: 'a <b> c'}", "{ok: 'see http://x'}", "{ok: '" + "x" * 81 + "'}", "{ok: ''}",
                "{Bad: fine}", '{ok: "tab\\there"}', "[a]"):
        p.write_text(f"mods: {bad}\n")
        with pytest.raises(ValueError):
            load(str(p))
