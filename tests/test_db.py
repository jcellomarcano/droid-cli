"""droid.db.list_databases contra un `ls -lR` real anonimizado de una app debuggable."""
import pytest

from droid import adb as adbmod
from droid import db

from tests.helpers import fixture_text


def test_list_databases_finds_sqlite_file(fake_shell):
    fake_shell(fixture_text("run_as_ls_lR_app"))
    dbs = db.list_databases("S", "com.example.app")
    assert len(dbs) == 1
    d = dbs[0]
    assert d.path == "databases/leaks.db"
    assert d.size == 36864
    assert d.mtime == "2026-09-15 15:03"
    assert d.wal is False
    assert d.shm is False
    assert d.name == "leaks.db"


def test_list_databases_excludes_non_db_files_in_other_dirs(fake_shell):
    fake_shell(fixture_text("run_as_ls_lR_app"))
    dbs = db.list_databases("S", "com.example.app")
    assert all("shared_prefs" not in d.path for d in dbs)
    assert all("profileInstalled" not in d.path for d in dbs)


def test_list_databases_permission_denied_returns_empty(fake_shell):
    fake_shell(fixture_text("run_as_ls_la_denied"))
    assert db.list_databases("S", "com.example.app") == []


def test_list_databases_garbage_output_returns_empty(fake_shell):
    fake_shell("garbage that is not an ls -lR listing at all\n")
    assert db.list_databases("S", "com.example.app") == []


def test_pull_database_pulls_base_wal_and_shm_via_exec_out(monkeypatch, tmp_path):
    calls = []
    contents = {"": b"base-db-bytes", "-wal": b"wal-bytes", "-shm": b"shm-bytes"}

    def fake_exec_out(serial, args, dest, timeout=120):
        suffix = args[-1].replace("databases/leaks.db", "")
        calls.append((serial, tuple(args), timeout))
        dest.write_bytes(contents[suffix])

    monkeypatch.setattr(adbmod, "exec_out", fake_exec_out)

    local = db.pull_database("S", "com.example.app", "databases/leaks.db", tmp_path)

    assert len(calls) == 3
    assert all(c[0] == "S" for c in calls)
    assert all(c[1][:3] == ("run-as", "com.example.app", "cat") for c in calls)
    remotes = [c[1][3] for c in calls]
    assert remotes == ["databases/leaks.db", "databases/leaks.db-wal", "databases/leaks.db-shm"]

    assert local == tmp_path / "leaks.db"
    assert local.read_bytes() == b"base-db-bytes"
    assert (tmp_path / "leaks.db-wal").read_bytes() == b"wal-bytes"
    assert (tmp_path / "leaks.db-shm").read_bytes() == b"shm-bytes"


def test_pull_database_raises_when_base_file_fails(monkeypatch, tmp_path):
    def fake_exec_out(serial, args, dest, timeout=120):
        raise adbmod.AdbError("run-as: package not debuggable")

    monkeypatch.setattr(adbmod, "exec_out", fake_exec_out)

    with pytest.raises(RuntimeError):
        db.pull_database("S", "com.example.app", "databases/leaks.db", tmp_path)


def test_pull_database_ignores_missing_wal_and_shm(monkeypatch, tmp_path):
    def fake_exec_out(serial, args, dest, timeout=120):
        remote = args[-1]
        if remote.endswith(("-wal", "-shm")):
            raise adbmod.AdbError("cat: No such file or directory")
        dest.write_bytes(b"base-db-bytes")

    monkeypatch.setattr(adbmod, "exec_out", fake_exec_out)

    local = db.pull_database("S", "com.example.app", "databases/leaks.db", tmp_path)
    assert local.read_bytes() == b"base-db-bytes"
    assert not (tmp_path / "leaks.db-wal").exists()
    assert not (tmp_path / "leaks.db-shm").exists()
