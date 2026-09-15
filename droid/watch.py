"""Hilo en segundo plano que sigue logcat (con reconexion) y despacha lineas parseadas filtradas por pid."""
import threading
from typing import Callable, Iterable, Optional

from . import logs as logsmod
from .logs import LogLine


class LogWatcher(threading.Thread):
    """Envuelve un LogcatStream: parsea cada linea y llama on_line(LogLine, raw) si coincide con el pid objetivo."""

    def __init__(self, serial: str, key: str, on_line: Callable[[LogLine, str], None], pid: Optional[int] = None,
                 buffers: Iterable[str] = ("main",), tail: int = 1, extra_tags: Iterable[str] = ()):
        super().__init__(daemon=True)
        self.serial = serial
        self.key = key
        self.on_line = on_line
        self.pid = pid
        self.buffers = list(buffers)
        self.tail = tail
        self.errors = 0
        self.extra_tags = set(extra_tags)
        self.stream = logsmod.LogcatStream(serial, key, buffers=self.buffers, tail=tail, reconnect=True)

    def set_pid(self, pid: Optional[int]) -> None:
        self.pid = pid

    def run(self) -> None:
        for kind, text in self.stream.stream():
            if kind != "line":
                continue
            ll = logsmod.parse(text)
            if ll is None:
                continue
            if self.pid is not None and ll.pid != self.pid and ll.tag not in self.extra_tags:
                continue
            try:
                self.on_line(ll, text)
            except Exception:
                self.errors += 1

    def stop(self) -> None:
        self.stream.close()
