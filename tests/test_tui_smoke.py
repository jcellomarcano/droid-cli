"""Prueba de humo de la TUI: arranca sin dispositivos, abre ayuda, cambia de pestaña."""
import droid.adb as adbmod
from droid.tui import DroidApp, HelpScreen


async def _wait_until(pilot, predicate, attempts=100, step=0.02):
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause(step)
    return False


async def test_app_mounts_help_and_tab_switch(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [])
    monkeypatch.setattr(adbmod, "adb_path", lambda: "/bin/echo")
    monkeypatch.setattr(DroidApp, "refresh_devices", lambda self: None)
    monkeypatch.setattr(DroidApp, "_tick_periodic", lambda self: None)
    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.active_tab == "devices"

        await pilot.press("question_mark")
        assert await _wait_until(pilot, lambda: isinstance(app.screen, HelpScreen))
        assert isinstance(app.screen, HelpScreen)

        await pilot.press("escape")
        assert await _wait_until(pilot, lambda: not isinstance(app.screen, HelpScreen))
        assert not isinstance(app.screen, HelpScreen)

        await pilot.press("2")
        assert await _wait_until(pilot, lambda: app.active_tab == "logs")
        assert app.active_tab == "logs"
