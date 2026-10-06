import os
import stat

from msnm.cli import main


def _cfg(tmp_path, public_url="https://hub.example.org"):
    p = tmp_path / "hub.yaml"
    p.write_text(f"data_dir: {tmp_path / 'data'}\npublic_url: {public_url}\n")
    return str(p)


def _token(capsys):
    out = capsys.readouterr().out
    return next(line.rsplit(" ", 1)[1] for line in out.splitlines() if "Token (shown once" in line
                or line.startswith("new token for"))


def test_node_add_writes_plain_message(tmp_path, capsys):
    assert main(["-c", _cfg(tmp_path), "node", "add", "vol-x", "--tier", "volunteer",
                 "--position", "remote"]) == 0
    token = _token(capsys)
    path = tmp_path / "data" / "welcome" / "vol-x.txt"
    text = path.read_text()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert f"Token: {token}" in text
    assert "sudo ./sidecar/install.sh --hub https://hub.example.org\n" in text
    assert "`" not in text and "**" not in text   # plain text, no markdown


def test_node_add_several_writes_one_message(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    assert main(["-c", cfg, "node", "add", "op2a", "op2b", "--tier", "volunteer",
                 "--position", "remote"]) == 0
    out = capsys.readouterr().out
    tokens = dict(line.split(" added", 1)[0].split()[1:] + [line.rsplit(" ", 1)[1]]
                  for line in out.splitlines() if "Token (shown once)" in line)
    assert set(tokens) == {"op2a", "op2b"}
    text = (tmp_path / "data" / "welcome" / "op2a+op2b.txt").read_text()
    assert f"op2a: {tokens['op2a']}\nop2b: {tokens['op2b']}\n" in text
    assert "You have 2 nodes" in text and "Token:" not in text
    # A taken or repeated id rejects the whole batch before anything is added.
    assert main(["-c", cfg, "node", "add", "op2c", "op2a", "--tier", "volunteer",
                 "--position", "remote"]) == 1
    assert main(["-c", cfg, "node", "add", "op2d", "op2d", "--tier", "volunteer",
                 "--position", "remote"]) == 1
    capsys.readouterr()
    main(["-c", cfg, "node", "list"])
    listed = capsys.readouterr().out
    assert "op2c" not in listed and "op2d" not in listed


def test_rotate_token_rewrites_message(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    main(["-c", cfg, "node", "add", "vol-y", "--tier", "volunteer", "--position", "remote"])
    old = _token(capsys)
    assert main(["-c", cfg, "node", "rotate-token", "vol-y", "--hub-url", "https://other.example"]) == 0
    new = _token(capsys)
    text = (tmp_path / "data" / "welcome" / "vol-y.txt").read_text()
    assert new != old and new in text and old not in text
    assert "--hub https://other.example" in text
