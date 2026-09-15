"""droid.files: parseo de `ls -la`, navegacion de sandbox/dispositivo y operaciones (pull/delete/preview)."""
import shlex
import stat
from pathlib import Path

import pytest

from tests.helpers import fixture_text
from droid import config, files


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
    import subprocess
    calls = []

    def fake_run(args, serial=None, timeout=20, input=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("droid.files.adbmod.run", fake_run)
    dest_dir = tmp_path / "out"
    files.pull_path("S", "sdcard", "Download/a.txt", None, dest_dir)
    assert calls[0][0] == "pull"


def test_pull_path_device_raises_and_removes_partial_file_on_nonzero_rc(monkeypatch, tmp_path):
    import subprocess

    def fake_run(args, serial=None, timeout=20, input=None):
        # simula que adb pull alcanzo a escribir algo antes de fallar
        Path(args[2]).write_bytes(b"truncado")
        return subprocess.CompletedProcess(args, 1, "", "device offline")

    monkeypatch.setattr("droid.files.adbmod.run", fake_run)
    dest_dir = tmp_path / "out"
    with pytest.raises(files.adbmod.AdbError):
        files.pull_path("S", "sdcard", "Download/a.txt", None, dest_dir)
    assert not (dest_dir / "a.txt").exists()


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
    ("readme.md", True),
    ("data.csv", True),
    ("app.properties", True),
    ("hosts", True),
    ("store.pb", False),
    ("leaks.db", False),
    ("noext", False),
])
def test_is_text_name(name, expected):
    assert files.is_text_name(name) is expected


# --- F5-C2/C20: contencion de la raiz (posixpath.normpath, .. y rutas absolutas) ---

@pytest.mark.parametrize("rel", ["..", "/etc/passwd", "a/../..", "../../etc", "a/../../b"])
def test_normalize_rel_rejects_escapes(rel):
    with pytest.raises(ValueError, match="ruta fuera de la raíz"):
        files._normalize_rel(rel)


@pytest.mark.parametrize("rel,expected", [("", ""), (".", ""), ("a/./b", "a/b"), ("a//b", "a/b"), ("a/b/", "a/b")])
def test_normalize_rel_normalizes_safe_paths(rel, expected):
    assert files._normalize_rel(rel) == expected


def test_list_dir_rejects_dotdot_rel_without_touching_adb(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no deberia llamarse adb shell con una ruta fuera de la raiz")

    monkeypatch.setattr("droid.files.adbmod.shell", boom)
    with pytest.raises(ValueError):
        files.list_dir("S", "sandbox", "../..", package="com.example.app")


def test_delete_path_rejects_absolute_rel(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no deberia llamarse adb shell con una ruta fuera de la raiz")

    monkeypatch.setattr("droid.files.adbmod.shell", boom)
    with pytest.raises(ValueError):
        files.delete_path("S", "sandbox", "/etc/passwd", "com.example.app", False, confirmed=True)


def test_preview_rejects_dotdot_rel(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no deberia llamarse adb shell con una ruta fuera de la raiz")

    monkeypatch.setattr("droid.files.adbmod.shell", boom)
    with pytest.raises(ValueError):
        files.preview("S", "sandbox", "a/../..", "com.example.app")


# --- F5-C1/INV-03: sin `sh -c` anidado, comillas de shlex.quote aplicadas una sola vez ---

TRICKY_REL = "mi carpeta/$(id)'x"


def test_list_dir_sandbox_quotes_tricky_path_without_nested_sh_c(fake_shell):
    seen = {}
    fake_shell(lambda cmd: seen.setdefault("cmd", cmd) or "")
    files.list_dir("S", "sandbox", TRICKY_REL, package="com.example.app")
    cmd = seen["cmd"]
    assert "sh -c" not in cmd
    assert shlex.quote(TRICKY_REL) in cmd


def test_delete_path_sandbox_quotes_tricky_path_without_nested_sh_c(fake_shell):
    seen = {}
    fake_shell(lambda cmd: seen.setdefault("cmd", cmd) or "")
    files.delete_path("S", "sandbox", TRICKY_REL, "com.example.app", False, confirmed=True)
    cmd = seen["cmd"]
    assert "sh -c" not in cmd
    assert shlex.quote(TRICKY_REL) in cmd


def test_preview_sandbox_quotes_tricky_path_without_nested_sh_c(fake_shell):
    seen = {}
    fake_shell(lambda cmd: seen.setdefault("cmd", cmd) or "")
    files.preview("S", "sandbox", TRICKY_REL, "com.example.app")
    cmd = seen["cmd"]
    assert "sh -c" not in cmd
    assert shlex.quote(TRICKY_REL) in cmd


def test_delete_path_device_root_quotes_tricky_path_without_nested_sh_c(fake_shell):
    seen = {}
    fake_shell(lambda cmd: seen.setdefault("cmd", cmd) or "")
    files.delete_path("S", "tmp", TRICKY_REL, None, False, confirmed=True)
    cmd = seen["cmd"]
    assert "sh -c" not in cmd
    assert shlex.quote(f"/data/local/tmp/{TRICKY_REL}") in cmd


@pytest.mark.allow_adb
def test_list_dir_command_reaches_fake_adb_shell_as_one_argument(tmp_path, monkeypatch):
    import droid.adb as adbmod

    fake_adb = tmp_path / "adb"
    fake_adb.write_text(
        "#!/bin/bash\n"
        "dir=\"$(dirname \"$0\")\"\n"
        "for a in \"$@\"; do printf '%s\\n' \"$a\" >> \"$dir/argv.txt\"; done\n"
        "echo \"$4\"\n"
    )
    fake_adb.chmod(fake_adb.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))

    files.list_dir("S", "sandbox", TRICKY_REL, package="com.example.app")

    argv_lines = (tmp_path / "argv.txt").read_text().splitlines()
    assert argv_lines[:3] == ["-s", "S", "shell"]
    shell_arg = argv_lines[3]
    tokens = shlex.split(shell_arg)
    assert TRICKY_REL in tokens
    assert not any(tok.startswith("$(") for tok in tokens)


# --- F5-C10/B1: pid encaminado hasta preview/pull/delete; /proc no se borra ---

def test_preview_proc_uses_pid_in_path(fake_shell):
    seen = {}
    fake_shell(lambda cmd: seen.setdefault("cmd", cmd) or "")
    files.preview("S", "proc", "cmdline", None, pid="1234")
    assert "/proc/1234/cmdline" in seen["cmd"]
    assert "/proc/None" not in seen["cmd"]


def test_pull_path_proc_uses_pid_in_path(monkeypatch, tmp_path):
    import subprocess
    calls = []

    def fake_run(args, serial=None, timeout=20, input=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("droid.files.adbmod.run", fake_run)
    files.pull_path("S", "proc", "cmdline", None, tmp_path / "out", pid="1234")
    assert calls[0][1] == "/proc/1234/cmdline"


def test_delete_path_proc_raises_and_never_calls_shell(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no deberia llamarse adb shell bajo /proc")

    monkeypatch.setattr("droid.files.adbmod.shell", boom)
    with pytest.raises(ValueError, match="no se borra bajo /proc"):
        files.delete_path("S", "proc", "cmdline", None, False, confirmed=True, pid="1234")


# --- F5-C8/B8: exec_out no deja ficheros truncados y pull_path revisa el rc de `adb pull` ---

def test_exec_out_writes_part_file_then_renames_on_success(monkeypatch, tmp_path):
    import subprocess
    import droid.adb as adbmod

    def fake_subprocess_run(cmd, stdout=None, stderr=None, timeout=None):
        stdout.write(b"hola")
        return subprocess.CompletedProcess(cmd, 0, None, b"")

    monkeypatch.setattr(adbmod, "adb_path", lambda: "/bin/echo")
    monkeypatch.setattr(adbmod.subprocess, "run", fake_subprocess_run)
    dest = tmp_path / "out.db"
    adbmod.exec_out("S", ["cat", "x"], dest)
    assert dest.read_bytes() == b"hola"
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_exec_out_removes_partial_file_on_nonzero_rc(monkeypatch, tmp_path):
    import subprocess
    import droid.adb as adbmod

    def fake_subprocess_run(cmd, stdout=None, stderr=None, timeout=None):
        stdout.write(b"truncado a medias")
        return subprocess.CompletedProcess(cmd, 1, None, b"run-as: package not debuggable")

    monkeypatch.setattr(adbmod, "adb_path", lambda: "/bin/echo")
    monkeypatch.setattr(adbmod.subprocess, "run", fake_subprocess_run)
    dest = tmp_path / "out.db"
    with pytest.raises(adbmod.AdbError):
        adbmod.exec_out("S", ["cat", "x"], dest)
    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_pull_path_sandbox_never_leaves_a_truncated_file_on_exec_out_failure(monkeypatch, tmp_path):
    import droid.adb as adbmod

    def fake_exec_out(serial, args, dest, timeout=120):
        raise adbmod.AdbError("run-as: package not debuggable")

    monkeypatch.setattr("droid.files.adbmod.exec_out", fake_exec_out)
    dest_dir = tmp_path / "out"
    with pytest.raises(adbmod.AdbError):
        files.pull_path("S", "sandbox", "databases/leaks.db", "com.example.app", dest_dir)
    assert not (dest_dir / "leaks.db").exists()


# --- F5-C13/B12: preview binario via exec-out (head -c 512), bytes reales ---

def test_preview_binary_sandbox_uses_run_as_head(monkeypatch, tmp_path):
    calls = []

    def fake_exec_out(serial, args, dest, timeout=120):
        calls.append(args)
        Path(dest).write_bytes(b"\xff\x00\xfe")

    monkeypatch.setattr("droid.files.adbmod.exec_out", fake_exec_out)
    data = files.preview_binary("S", "sandbox", "files/x.bin", "com.example.app")
    assert data == b"\xff\x00\xfe"
    assert calls[0] == ["run-as", "com.example.app", "head", "-c", "512", "files/x.bin"]


def test_preview_binary_device_root_uses_head_without_run_as(monkeypatch, tmp_path):
    calls = []

    def fake_exec_out(serial, args, dest, timeout=120):
        calls.append(args)
        Path(dest).write_bytes(b"\x89PNG")

    monkeypatch.setattr("droid.files.adbmod.exec_out", fake_exec_out)
    data = files.preview_binary("S", "sdcard", "img.png", None)
    assert data == b"\x89PNG"
    assert calls[0] == ["head", "-c", "512", "/sdcard/img.png"]
    assert "run-as" not in calls[0]


# --- F5-B2/INV-04: deteccion de rutas de shared_prefs / *prefs*.xml ---

@pytest.mark.parametrize("rel,name,expected", [
    ("shared_prefs", "app_prefs.xml", True),
    ("", "user_prefs.xml", True),
    ("files", "notes.txt", False),
    ("", "prefsdata.bin", False),
])
def test_is_prefs_path(rel, name, expected):
    assert files.is_prefs_path(rel, name) is expected


# --- F5-B3: auditabilidad del borrado ---

def test_delete_path_appends_audit_log_line(fake_shell, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DROID_HOME", tmp_path / ".droid")
    fake_shell("")
    ok, _ = files.delete_path("S", "sandbox", "databases/leaks.db", "com.example.app", False,
                               confirmed=True, device_key="dev-key-1")
    assert ok is True
    log = (tmp_path / ".droid" / files.AUDIT_LOG_NAME).read_text()
    line = log.strip().splitlines()[-1]
    ts, dev_key, root, path, ok_str = line.split("\t")
    assert dev_key == "dev-key-1"
    assert root == "sandbox"
    assert path == "databases/leaks.db"
    assert ok_str == "True"


def test_list_dir_device_root_lists_the_directory_behind_a_symlink(fake_shell):
    seen = []
    fake_shell(lambda cmd: seen.append(cmd) or "total 0\n")
    files.list_dir("S", "sdcard", "")
    assert "ls -la /sdcard/ " in seen[-1]


def test_delete_path_refuses_the_root_itself(fake_shell):
    called = []
    fake_shell(lambda cmd: called.append(cmd) or "")
    for rel in ("", ".", "x/..", "droid-test/./.."):
        with pytest.raises(ValueError):
            files.delete_path("S", "tmp", rel, None, True, confirmed=True)
    assert called == []


def test_parse_ls_turns_transport_errors_into_error_rows():
    rows = files.parse_ls("error: device offline", "x")
    assert len(rows) == 1 and rows[0].kind == "error" and "offline" in rows[0].message
    rows = files.parse_ls("adb: device 'EMU' not found", "x")
    assert rows and rows[0].kind == "error"


def test_check_access_explains_a_non_debuggable_package(monkeypatch):
    from droid import db
    monkeypatch.setattr(db, "check_run_as", lambda serial, pkg: (False, "run-as: package not debuggable: com.example.other"))
    ok, msg = files.check_access("S", "sandbox", "com.example.other")
    assert ok is False and "no es debuggable" in msg
