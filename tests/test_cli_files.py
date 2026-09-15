"""`droid files`: listado JSON y borrado (--rm) con y sin confirmacion (-y)."""
import json

import droid.adb as adbmod
import droid.files as filesmod
from droid import cli, ui
from droid.adb import Device
from droid.files import FileEntry


def _dev():
    return Device(serial="EMU", state="device", transport="emu", model="X")


def _entries():
    return [
        FileEntry(name="databases", path="databases", kind="dir", size=0, mtime="2024-01-01 00:00",
                  perm="drwxrwx--x", owner="u0_a1", link_target=None, message=None),
        FileEntry(name="a.txt", path="a.txt", kind="file", size=12, mtime="2024-01-01 00:00",
                  perm="-rw-rw----", owner="u0_a1", link_target=None, message=None),
    ]


def test_files_json_lists_entries(monkeypatch, capsys):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())

    rc = cli.main(["files", "-p", "com.example.app", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    names = {e["name"] for e in data}
    assert names == {"databases", "a.txt"}


def test_files_rm_without_yes_asks_and_skips_when_declined(monkeypatch, capsys):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())
    monkeypatch.setattr(ui, "confirm", lambda *a, **kw: False)

    delete_calls = []

    def fake_delete_path(serial, root, rel, package, is_dir, *, confirmed=False, pid=None, device_key=None):
        delete_calls.append((root, rel, package, is_dir, confirmed))
        return True, ""

    monkeypatch.setattr(filesmod, "delete_path", fake_delete_path)

    rc = cli.main(["files", "-p", "com.example.app", "EMU", "a.txt", "--rm"])
    assert rc == 0
    assert delete_calls == []


def test_files_rm_with_yes_deletes_confirmed(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())

    def _confirm_should_not_be_called(*a, **kw):
        raise AssertionError("ui.confirm no debe llamarse con -y")

    monkeypatch.setattr(ui, "confirm", _confirm_should_not_be_called)

    delete_calls = []

    def fake_delete_path(serial, root, rel, package, is_dir, *, confirmed=False, pid=None, device_key=None):
        delete_calls.append((root, rel, package, is_dir, confirmed))
        return True, ""

    monkeypatch.setattr(filesmod, "delete_path", fake_delete_path)

    rc = cli.main(["files", "-p", "com.example.app", "EMU", "a.txt", "--rm", "-y"])
    assert rc == 0
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == "a.txt"
    assert delete_calls[0][3] is False  # is_dir: "a.txt" is a file in _entries()
    assert delete_calls[0][4] is True   # confirmed


def test_files_rm_directory_without_recursive_is_rejected(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())

    def _delete_should_not_be_called(*a, **kw):
        raise AssertionError("delete_path no debe llamarse sin --recursive sobre un directorio")

    monkeypatch.setattr(filesmod, "delete_path", _delete_should_not_be_called)

    rc = cli.main(["files", "-p", "com.example.app", "EMU", "databases", "--rm", "-y"])
    assert rc == 1


def test_files_rm_directory_with_recursive_and_yes_deletes_confirmed(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())

    delete_calls = []

    def fake_delete_path(serial, root, rel, package, is_dir, *, confirmed=False, pid=None, device_key=None):
        delete_calls.append((root, rel, package, is_dir, confirmed))
        return True, ""

    monkeypatch.setattr(filesmod, "delete_path", fake_delete_path)

    rc = cli.main(["files", "-p", "com.example.app", "EMU", "databases", "--rm", "--recursive", "-y"])
    assert rc == 0
    assert len(delete_calls) == 1
    assert delete_calls[0][3] is True  # is_dir


def test_files_rm_on_proc_root_is_rejected(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))

    def _delete_should_not_be_called(*a, **kw):
        raise AssertionError("delete_path no debe llamarse bajo /proc")

    monkeypatch.setattr(filesmod, "delete_path", _delete_should_not_be_called)

    rc = cli.main(["files", "EMU", "--root", "proc", "--pid", "1234", "cmdline", "--rm", "-y"])
    assert rc == 1


def test_files_json_keeps_error_rows_and_exits_1_when_all_errors(monkeypatch, capsys):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))
    error_entry = FileEntry(name="", path="", kind="error", size=0, mtime="", perm="",
                             owner="", link_target=None, message="ls: Permission denied")
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: [error_entry])

    rc = cli.main(["files", "-p", "com.example.app", "--json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert data[0]["kind"] == "error"


def test_files_json_mixed_entries_keeps_errors_but_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))
    error_entry = FileEntry(name="", path="", kind="error", size=0, mtime="", perm="",
                             owner="", link_target=None, message="ls: Permission denied")
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries() + [error_entry])

    rc = cli.main(["files", "-p", "com.example.app", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    kinds = {e["kind"] for e in data}
    assert "error" in kinds
    assert len(data) == 3


def test_files_path_after_options_is_accepted(monkeypatch, capsys):
    from droid import cli, files
    from droid.adb import Device
    monkeypatch.setattr(cli.adbmod, "list_devices", lambda details=True: [_dev()])
    seen = {}
    monkeypatch.setattr(files, "check_access", lambda *a, **k: (True, ""))
    monkeypatch.setattr(files, "list_dir", lambda serial, root, rel="", package=None, pid=None: seen.update(root=root, rel=rel) or [])
    assert cli.main(["files", _dev().serial, "--root", "tmp", "droid-test", "--json"]) == 0
    assert seen == {"root": "tmp", "rel": "droid-test"}


def test_files_pull_and_rm_refuse_the_root_and_directories_without_recursive(monkeypatch, tmp_path):
    from droid import cli, files as filesmod
    import droid.adb as adbmod
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [_dev()])
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())
    calls = []
    monkeypatch.setattr(filesmod, "pull_path", lambda *a, **kw: calls.append(("pull", a)) or [])
    monkeypatch.setattr(filesmod, "delete_path", lambda *a, **kw: calls.append(("rm", a)) or (True, ""))
    assert cli.main(["files", _dev().serial, "--root", "tmp", "--pull", str(tmp_path)]) == 1
    assert cli.main(["files", _dev().serial, "--root", "tmp", "--rm", "-y", "--recursive"]) == 1
    assert calls == []
