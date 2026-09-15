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

    def fake_delete_path(serial, root, rel, package, is_dir, *, confirmed=False):
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

    def fake_delete_path(serial, root, rel, package, is_dir, *, confirmed=False):
        delete_calls.append((root, rel, package, is_dir, confirmed))
        return True, ""

    monkeypatch.setattr(filesmod, "delete_path", fake_delete_path)

    rc = cli.main(["files", "-p", "com.example.app", "EMU", "a.txt", "--rm", "-y"])
    assert rc == 0
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == "a.txt"
    assert delete_calls[0][3] is False  # is_dir: "a.txt" is a file in _entries()
    assert delete_calls[0][4] is True   # confirmed


def test_files_path_after_options_is_accepted(monkeypatch, capsys):
    from droid import cli, files
    from droid.adb import Device
    monkeypatch.setattr(cli.adbmod, "list_devices", lambda details=True: [_dev()])
    seen = {}
    monkeypatch.setattr(files, "check_access", lambda *a, **k: (True, ""))
    monkeypatch.setattr(files, "list_dir", lambda serial, root, rel="", package=None, pid=None: seen.update(root=root, rel=rel) or [])
    assert cli.main(["files", _dev().serial, "--root", "tmp", "droid-test", "--json"]) == 0
    assert seen == {"root": "tmp", "rel": "droid-test"}
