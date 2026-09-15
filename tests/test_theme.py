"""Identidad de color: mismo valor de entrada -> mismo color siempre."""
from droid import theme


def test_color_for_stable_and_in_palette():
    assert theme.color_for("ActivityManager", theme.TAGS) == theme.color_for("ActivityManager", theme.TAGS)
    assert theme.color_for("ActivityManager", theme.TAGS) in theme.TAGS


def test_color_for_empty_string_is_first_of_palette():
    assert theme.color_for("", theme.IDENTITY) == theme.IDENTITY[0]


def test_pid_color_distinguishes_consecutive_pids():
    assert theme.pid_color(10362) != theme.pid_color(10363)
    assert theme.pid_color(10362) in theme.IDENTITY


def test_thread_color_uses_t_prefix_namespace():
    assert theme.thread_color("RenderThread") == theme.color_for("t:RenderThread")


def test_spark_maps_values_to_bar_glyphs():
    assert theme.spark([1, 2, 3, 4, 5, 10]) == "▁▁▂▃▄█"


def test_spark_empty_input_returns_empty_string():
    assert theme.spark([]) == ""
    assert theme.spark([None, None]) == ""


def test_kb_formats_megabytes_and_none():
    assert theme.kb(2048) == "2.0 MB"
    assert theme.kb(512) == "0.5 MB"
    assert theme.kb(None) == "—"


def test_rate_formats_bytes_kilobytes_megabytes():
    assert theme.rate(500) == "500 B/s"
    assert theme.rate(2048) == "2 KB/s"
    assert theme.rate(5 * 1024 * 1024) == "5.0 MB/s"
