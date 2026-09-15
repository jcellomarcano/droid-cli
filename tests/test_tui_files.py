"""Prueba de humo de la pestaña Archivos (F) de la TUI: navegacion, preview y borrado con confirmacion."""
import droid.adb as adbmod
import droid.files as filesmod
from droid.adb import Device
from droid.files import FileEntry
from droid.tui import Confirm, DroidApp


async def _wait_until(pilot, predicate, attempts=100, step=0.02):
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause(step)
    return False


def _dev():
    return Device(serial="EMU", state="device", transport="emu", model="X")


def _entries():
    return [
        FileEntry(name="databases", path="databases", kind="dir", size=0, mtime="2024-01-01 00:00",
                  perm="drwxrwx--x", owner="u0_a1", link_target=None, message=None),
        FileEntry(name="a.txt", path="a.txt", kind="file", size=12, mtime="2024-01-01 00:00",
                  perm="-rw-rw----", owner="u0_a1", link_target=None, message=None),
    ]


def _mount(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [])
    monkeypatch.setattr(adbmod, "adb_path", lambda: "/bin/echo")
    monkeypatch.setattr(DroidApp, "refresh_devices", lambda self: None)
    monkeypatch.setattr(DroidApp, "_tick_periodic", lambda self: None)


async def test_tab_f_switches_to_files(monkeypatch):
    _mount(monkeypatch)
    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("F")
        assert await _wait_until(pilot, lambda: app.active_tab == "files")
        assert app.active_tab == "files"


async def test_load_and_navigate_into_dir(monkeypatch):
    _mount(monkeypatch)
    calls = []

    def fake_list_dir(serial, root, rel="", package=None, pid=None):
        calls.append((root, rel, package, pid))
        return _entries()

    monkeypatch.setattr(filesmod, "list_dir", fake_list_dir)
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))

    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current = _dev()
        pane = app.query_one("#files_pane")
        pane.query_one("#f_pkg").value = "com.example.app"
        pane.action_reload()
        table = pane.query_one("#f_list")
        assert await _wait_until(pilot, lambda: table.row_count == 2)
        assert table.row_count == 2

        pane._enter("databases")
        assert await _wait_until(pilot, lambda: pane.rel_segments == ["databases"])
        assert await _wait_until(pilot, lambda: any(c[1] == "databases" for c in calls))
        breadcrumb = pane.query_one("#f_breadcrumb").render().plain
        assert "databases" in breadcrumb
        # once inside a subdirectory the ".." row is shown alongside the two entries
        assert await _wait_until(pilot, lambda: table.row_count == 3)


async def test_delete_file_needs_confirmation_before_calling_delete_path(monkeypatch):
    _mount(monkeypatch)
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))

    delete_calls = []

    def fake_delete_path(serial, root, rel, package, is_dir, *, confirmed=False, pid=None, device_key=None):
        delete_calls.append((root, rel, package, is_dir, confirmed))
        return True, ""

    monkeypatch.setattr(filesmod, "delete_path", fake_delete_path)

    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current = _dev()
        pane = app.query_one("#files_pane")
        pane.query_one("#f_pkg").value = "com.example.app"
        pane.action_reload()
        table = pane.query_one("#f_list")
        assert await _wait_until(pilot, lambda: table.row_count == 2)
        table.move_cursor(row=1)  # "a.txt", a plain file

        pushed = []

        def _capture_push(screen, callback=None):
            pushed.append(callback)

        monkeypatch.setattr(app, "push_screen", _capture_push)

        pane.action_delete()
        assert len(pushed) == 1
        # decline the confirmation -> delete_path must NOT be called
        pushed[0](False)
        assert delete_calls == []

        pane.action_delete()
        assert len(pushed) == 2
        # confirm this time -> delete_path is called once, with confirmed=True
        pushed[1](True)
        assert await _wait_until(pilot, lambda: len(delete_calls) == 1)
        assert delete_calls[0][3] is False  # is_dir
        assert delete_calls[0][4] is True   # confirmed


async def test_delete_directory_requires_second_confirmation(monkeypatch):
    _mount(monkeypatch)
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))

    delete_calls = []

    def fake_delete_path(serial, root, rel, package, is_dir, *, confirmed=False, pid=None, device_key=None):
        delete_calls.append((root, rel, package, is_dir, confirmed))
        return True, ""

    monkeypatch.setattr(filesmod, "delete_path", fake_delete_path)

    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current = _dev()
        pane = app.query_one("#files_pane")
        pane.query_one("#f_pkg").value = "com.example.app"
        pane.action_reload()
        table = pane.query_one("#f_list")
        assert await _wait_until(pilot, lambda: table.row_count == 2)
        table.move_cursor(row=0)  # "databases", a directory

        pushed = []

        def _capture_push(screen, callback=None):
            pushed.append(callback)

        monkeypatch.setattr(app, "push_screen", _capture_push)

        pane.action_delete()
        assert len(pushed) == 1
        pushed[0](True)  # confirm first prompt -> counts children in a worker, then pushes a
        # SECOND, distinct confirmation (with the count) via call_from_thread; still no delete.
        assert await _wait_until(pilot, lambda: len(pushed) == 2)
        assert delete_calls == []

        pushed[1](True)  # confirm second prompt -> now delete_path runs
        assert await _wait_until(pilot, lambda: len(delete_calls) == 1)
        assert delete_calls[0][3] is True  # is_dir
        assert delete_calls[0][4] is True  # confirmed


async def test_delete_directory_second_confirmation_message_mentions_entry_count(monkeypatch):
    _mount(monkeypatch)
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))

    def fake_list_dir(serial, root, rel="", package=None, pid=None):
        if rel == "databases":
            return [
                FileEntry(name="a.db", path="databases/a.db", kind="file", size=1, mtime="",
                          perm="", owner="", link_target=None, message=None),
                FileEntry(name="b.db", path="databases/b.db", kind="file", size=1, mtime="",
                          perm="", owner="", link_target=None, message=None),
            ]
        return _entries()

    monkeypatch.setattr(filesmod, "list_dir", fake_list_dir)
    monkeypatch.setattr(filesmod, "delete_path", lambda *a, **kw: (True, ""))

    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current = _dev()
        pane = app.query_one("#files_pane")
        pane.query_one("#f_pkg").value = "com.example.app"
        pane.action_reload()
        table = pane.query_one("#f_list")
        assert await _wait_until(pilot, lambda: table.row_count == 2)
        table.move_cursor(row=0)  # "databases"

        pushed_screens = []

        def _capture_push(screen, callback=None):
            pushed_screens.append((screen, callback))

        monkeypatch.setattr(app, "push_screen", _capture_push)

        pane.action_delete()
        assert len(pushed_screens) == 1
        first_screen, first_cb = pushed_screens[0]
        assert first_screen._text == "¿Borrar databases?"
        first_cb(True)

        assert await _wait_until(pilot, lambda: len(pushed_screens) == 2)
        second_screen, _second_cb = pushed_screens[1]
        assert second_screen._text != first_screen._text
        assert "2" in second_screen._text


async def test_delete_blocked_under_proc_root(monkeypatch):
    _mount(monkeypatch)
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))

    def _delete_should_not_be_called(*a, **kw):
        raise AssertionError("delete_path no debe llamarse bajo /proc")

    monkeypatch.setattr(filesmod, "delete_path", _delete_should_not_be_called)

    notifications = []
    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current = _dev()
        monkeypatch.setattr(app, "notify", lambda msg, **kw: notifications.append(msg))
        pane = app.query_one("#files_pane")
        pane.root = "proc"
        pane.entries = {"cmdline": FileEntry(name="cmdline", path="cmdline", kind="file", size=0,
                                              mtime="", perm="", owner="", link_target=None, message=None)}
        table = pane.query_one("#f_list")
        table.add_row("", "cmdline", "", "", "", key="cmdline")
        table.move_cursor(row=0)

        pane.action_delete()
        assert any("proc" in m for m in notifications)


async def test_link_entry_shows_target_and_enter_does_nothing(monkeypatch):
    _mount(monkeypatch)
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))

    def fake_list_dir(serial, root, rel="", package=None, pid=None):
        return [
            FileEntry(name="lib", path="lib", kind="link", size=0, mtime="2024-01-01 00:00",
                      perm="lrwxrwxrwx", owner="root", link_target="/system/lib", message=None),
        ]

    monkeypatch.setattr(filesmod, "list_dir", fake_list_dir)

    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current = _dev()
        pane = app.query_one("#files_pane")
        pane.query_one("#f_pkg").value = "com.example.app"
        pane.action_reload()
        table = pane.query_one("#f_list")
        assert await _wait_until(pilot, lambda: table.row_count == 1)
        assert table.get_cell_at((0, 1)) == "lib -> /system/lib"

        # Enter on a link must not navigate into it (rel_segments stays empty)
        pane._enter("lib")
        await pilot.pause()
        assert pane.rel_segments == []


async def test_open_db_blocked_when_root_is_not_sandbox(monkeypatch):
    _mount(monkeypatch)
    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current = _dev()
        pane = app.query_one("#files_pane")
        pane.root = "sdcard"
        pane.entries = {"a.db": FileEntry(name="a.db", path="a.db", kind="file", size=1, mtime="",
                                           perm="", owner="", link_target=None, message=None)}
        table = pane.query_one("#f_list")
        table.add_row("", "a.db", "", "", "", key="a.db")
        table.move_cursor(row=0)

        notifications = []
        monkeypatch.setattr(app, "notify", lambda msg, **kw: notifications.append(msg))
        pane.action_open_db()
        assert any("sandbox" in m for m in notifications)


async def test_x_on_file_row_drives_the_real_confirm_modal(monkeypatch):
    """F5-C16: pulsa 'x' con el foco en #f_list y contesta el Confirm REAL (no monkeypatcheado),
    usando sus propios bindings (escape/y) en lugar de interceptar push_screen."""
    _mount(monkeypatch)
    monkeypatch.setattr(filesmod, "list_dir", lambda *a, **kw: _entries())
    monkeypatch.setattr(filesmod, "check_access", lambda *a, **kw: (True, "ok"))

    delete_calls = []

    def fake_delete_path(serial, root, rel, package, is_dir, *, confirmed=False, pid=None, device_key=None):
        delete_calls.append((root, rel, package, is_dir, confirmed))
        return True, ""

    monkeypatch.setattr(filesmod, "delete_path", fake_delete_path)

    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.current = _dev()
        pane = app.query_one("#files_pane")
        pane.query_one("#f_pkg").value = "com.example.app"
        pane.action_reload()
        table = pane.query_one("#f_list")
        assert await _wait_until(pilot, lambda: table.row_count == 2)
        table.move_cursor(row=1)  # "a.txt", a plain file
        table.focus()
        await pilot.pause()

        await pilot.press("x")
        assert await _wait_until(pilot, lambda: isinstance(app.screen, Confirm))

        await pilot.press("escape")  # Confirm.BINDINGS: escape -> action_no -> dismiss(False)
        assert await _wait_until(pilot, lambda: not isinstance(app.screen, Confirm))
        assert delete_calls == []

        await pilot.press("x")
        assert await _wait_until(pilot, lambda: isinstance(app.screen, Confirm))

        await pilot.press("y")  # Confirm.BINDINGS: y -> action_yes -> dismiss(True)
        assert await _wait_until(pilot, lambda: len(delete_calls) == 1)
        assert delete_calls[0][3] is False  # is_dir
        assert delete_calls[0][4] is True   # confirmed
