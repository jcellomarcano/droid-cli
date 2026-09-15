"""droid.db.list_databases contra un `ls -lR` real anonimizado de una app debuggable."""
from tests.helpers import fixture_text

from droid import db


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
