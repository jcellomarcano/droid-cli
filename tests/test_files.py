"""droid.files: parseo de `ls -la`, navegacion de sandbox/dispositivo y operaciones (pull/delete/preview)."""
from pathlib import Path

import pytest

from tests.helpers import fixture_text
from droid import files


def test_parse_ls_root_section_app_has_five_dirs():
    root_text = fixture_text("run_as_ls_la_app").split("\n==", 1)[0]
    entries = files.parse_ls(root_text)
    dirs = [e for e in entries if e.kind == "dir"]
    assert {d.name for d in dirs} == {"cache", "code_cache", "databases", "files", "shared_prefs"}


def test_parse_ls_mixed_fixture():
    entries = files.parse_ls(fixture_text("ls_la_mixed"))
    by_name = {e.name: e for e in entries if e.kind != "error"}

    assert by_name["mydir"].kind == "dir"

    f = by_name["myfile.txt"]
    assert f.kind == "file"
    assert f.size == 123

    link = by_name["lib"]
    assert link.kind == "link"
    assert link.link_target == "/system/lib"

    sock = by_name["s"]
    assert sock.kind == "other"

    spaced = by_name["file with spaces.txt"]
    assert spaced.kind == "file"
    assert spaced.size == 456

    errors = [e for e in entries if e.kind == "error"]
    assert len(errors) == 1
    assert "Permission denied" in errors[0].message

    assert not any(e.name in (".", "..") for e in entries)


def test_parse_ls_sdcard_fixture_files_and_dirs():
    entries = files.parse_ls(fixture_text("ls_la_sdcard"))
    names = {e.name for e in entries}
    assert "cat.png" in names
    assert "Download" in names
    cat_png = next(e for e in entries if e.name == "cat.png")
    assert cat_png.kind == "file"
    assert cat_png.size == 32316
    download = next(e for e in entries if e.name == "Download")
    assert download.kind == "dir"


def test_list_dir_sandbox_builds_run_as_command(fake_shell):
    seen = {}

    def record(cmd):
        seen["cmd"] = cmd
        return ""

    fake_shell(record)
    files.list_dir("S", "sandbox", "databases", package="com.example.app")
    assert "run-as com.example.app" in seen["cmd"]
    assert "databases" in seen["cmd"]


def test_list_dir_device_builds_ls_command(fake_shell):
    seen = {}

    def record(cmd):
        seen["cmd"] = cmd
        return ""

    fake_shell(record)
    files.list_dir("S", "sdcard", "")
    assert "/sdcard" in seen["cmd"]
    assert "run-as" not in seen["cmd"]


def test_pull_path_sandbox_uses_exec_out(monkeypatch, tmp_path):
    calls = []

    def fake_exec_out(serial, args, dest, timeout=120):
        calls.append((serial, args, dest))
        Path(dest).write_bytes(b"hola")

    monkeypatch.setattr("droid.files.adbmod.exec_out", fake_exec_out)
    dest_dir = tmp_path / "out"
    pulled = files.pull_path("S", "sandbox", "databases/leaks.db", "com.example.app", dest_dir)
    assert len(pulled) == 1
    assert calls[0][1][:2] == ["run-as", "com.example.app"]
    assert pulled[0].read_bytes() == b"hola"


def test_pull_path_device_uses_adb_pull(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, serial=None, timeout=20, input=None):
        calls.append(args)

    monkeypatch.setattr("droid.files.adbmod.run", fake_run)
    dest_dir = tmp_path / "out"
    files.pull_path("S", "sdcard", "Download/a.txt", None, dest_dir)
    assert calls[0][0] == "pull"


def test_delete_path_without_confirmed_raises_and_never_calls_shell(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no deberia llamarse adb shell sin confirmed=True")

    monkeypatch.setattr("droid.files.adbmod.shell", boom)
    with pytest.raises(ValueError):
        files.delete_path("S", "sandbox", "databases/leaks.db", "com.example.app", False)


def test_delete_path_confirmed_calls_rm_f(fake_shell):
    seen = {}

    def record(cmd):
        seen["cmd"] = cmd
        return ""

    fake_shell(record)
    ok, out = files.delete_path("S", "sandbox", "databases/leaks.db", "com.example.app", False, confirmed=True)
    assert ok is True
    assert "rm -f" in seen["cmd"]


def test_delete_path_confirmed_dir_calls_rm_rf(fake_shell):
    seen = {}

    def record(cmd):
        seen["cmd"] = cmd
        return ""

    fake_shell(record)
    files.delete_path("S", "sdcard", "Download", None, True, confirmed=True)
    assert "rm -rf" in seen["cmd"]


def test_preview_not_truncated(fake_shell):
    fake_shell("contenido corto")
    text, truncated = files.preview("S", "sandbox", "files/x.txt", "com.example.app", max_bytes=1000)
    assert text == "contenido corto"
    assert truncated is False


def test_preview_truncated(fake_shell):
    fake_shell("x" * 50)
    text, truncated = files.preview("S", "sandbox", "files/x.txt", "com.example.app", max_bytes=10)
    assert truncated is True
    assert len(text) == 10


@pytest.mark.parametrize("name,expected", [
    ("prefs.xml", True),
    ("a.txt", True),
    ("data.json", True),
    ("app.log", True),
    ("settings.prefs", True),
    ("store.pb", False),
    ("leaks.db", False),
    ("noext", False),
])
def test_is_text_name(name, expected):
    assert files.is_text_name(name) is expected


def test_list_dir_device_root_lists_the_directory_behind_a_symlink(fake_shell):
    seen = []
    fake_shell(lambda cmd: seen.append(cmd) or "total 0\n")
    files.list_dir("S", "sdcard", "")
    assert "ls -la /sdcard/ " in seen[-1]
