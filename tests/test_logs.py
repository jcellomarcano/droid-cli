"""droid.logs: parseo de lineas logcat, filtros y render, contra una captura real anonimizada."""
from tests.helpers import fixture_text

from droid.logs import AM_DIED_RE, AM_KILL_RE, AM_START_RE, LINE_RE, Filter, LogLine, Renderer, parse


def test_parse_full_line_with_year():
    line = "2026-09-15 15:03:13.450 10362 10362 E AndroidRuntime: FATAL EXCEPTION: main"
    ll = parse(line)
    assert ll is not None
    assert ll.date == "2026-09-15"
    assert ll.time == "15:03:13.450"
    assert ll.pid == 10362
    assert ll.tid == 10362
    assert ll.level == "E"
    assert ll.tag == "AndroidRuntime"
    assert ll.msg == "FATAL EXCEPTION: main"


def test_parse_real_crash_fixture_line():
    text = fixture_text("logcat_crash")
    line = [l for l in text.splitlines() if "Process:" in l][0]
    ll = parse(line)
    assert ll is not None
    assert "com.example.app" in ll.msg
    assert "example" not in ll.msg


def test_parse_separator_line_returns_none():
    assert parse("--------- beginning of crash") is None


def test_parse_garbage_returns_none():
    assert parse("not a logcat line at all") is None


def test_line_re_requires_valid_level_letter():
    assert LINE_RE.match("09-15 15:03:13.450 1 1 Z tag: msg") is None


def test_parse_line_without_year_returns_md_only_date():
    line = "09-15 15:03:13.450 10362 10362 E AndroidRuntime: FATAL EXCEPTION: main"
    ll = parse(line)
    assert ll is not None
    assert ll.date == "09-15"
    assert ll.time == "15:03:13.450"


def test_am_start_re_matches_start_proc():
    m = AM_START_RE.match("Start proc 12345:com.example.app/u0a93 for activity")
    assert m.group(1) == "12345"
    assert m.group(2) == "com.example.app"


def test_am_died_re_matches_real_fixture_line():
    text = fixture_text("logcat_am_art")
    line = [l for l in text.splitlines() if "has died" in l][0]
    ll = parse(line)
    m = AM_DIED_RE.match(ll.msg)
    assert m.group(1) == "com.example.app"
    assert m.group(2) == "10362"


def test_am_kill_re_matches_killing_line():
    m = AM_KILL_RE.match("Killing 555:com.example.app/u0a10 (adj 900): empty for 30m")
    assert m.group(1) == "555"
    assert m.group(2) == "com.example.app"


def test_am_kill_re_no_match_on_unrelated_text():
    assert AM_KILL_RE.match("Killed by the user") is None


def test_filter_observe_start_tracks_pid():
    f = Filter(packages=["com.example.app"])
    ll = LogLine("raw", "09-15", "10:00:00.000", 1, 1, "I", "ActivityManager", "Start proc 555:com.example.app/u0a1 for service")
    evt = f.observe(ll)
    assert evt == "proceso com.example.app iniciado (pid 555)"
    assert 555 in f.all_pids()


def test_filter_observe_died_from_real_fixture_untracks_pid():
    f = Filter(packages=["com.example.app"])
    f.set_pids({10362: "com.example.app"})
    text = fixture_text("logcat_am_art")
    line = [l for l in text.splitlines() if "has died" in l][0]
    ll = parse(line)
    evt = f.observe(ll)
    assert evt == "proceso com.example.app murió (pid 10362)"
    assert 10362 not in f.all_pids()


def test_filter_observe_ignores_non_activitymanager_tag():
    f = Filter(packages=["com.example.app"])
    ll = LogLine("raw", "09-15", "10:00:00.000", 1, 1, "I", "OtherTag", "Start proc 555:com.example.app/u0a1 for service")
    assert f.observe(ll) is None


def test_filter_matches_by_package_pid_and_level():
    f = Filter(packages=["com.example.app"], min_level="W")
    f.set_pids({10362: "com.example.app"})
    warn = LogLine("raw", "09-15", "10:00:00.000", 10362, 10362, "W", "Tag", "warning message")
    info = LogLine("raw", "09-15", "10:00:00.000", 10362, 10362, "I", "Tag", "info message")
    other_pid = LogLine("raw", "09-15", "10:00:00.000", 1, 1, "E", "Tag", "boom")
    assert f.matches(warn) is True
    assert f.matches(info) is False
    assert f.matches(other_pid) is False


def test_filter_matches_grep_and_exclude():
    f = Filter(grep="boom", exclude="ignoreme")
    hit = LogLine("raw", "09-15", "10:00:00.000", 1, 1, "I", "Tag", "boom happened")
    excluded = LogLine("raw", "09-15", "10:00:00.000", 1, 1, "I", "Tag", "boom but ignoreme")
    miss = LogLine("raw", "09-15", "10:00:00.000", 1, 1, "I", "Tag", "nothing here")
    assert f.matches(hit) is True
    assert f.matches(excluded) is False
    assert f.matches(miss) is False


def test_filter_matches_garbage_empty_filter_accepts_everything():
    f = Filter()
    ll = LogLine("raw", "09-15", "10:00:00.000", 1, 1, "V", "", "")
    assert f.matches(ll) is True


def test_renderer_plain_returns_raw_untouched():
    r = Renderer(color=False)
    ll = LogLine("the raw text", "09-15", "10:00:00.000", 1, 1, "I", "Tag", "msg")
    assert r.plain(ll) == "the raw text"


def test_renderer_line_no_color_contains_message():
    r = Renderer(color=False, show_time=True, show_pid=True)
    r.set_width(200)
    ll = LogLine("raw", "09-15", "15:03:13.450", 10362, 10362, "E", "AndroidRuntime", "FATAL EXCEPTION: main")
    out = r.line(ll)
    assert "FATAL EXCEPTION: main" in out
    assert "10362" in out


def test_renderer_line_from_real_fixture_line():
    r = Renderer(color=False)
    r.set_width(300)
    text = fixture_text("logcat_crash")
    line = [l for l in text.splitlines() if "Process:" in l][0]
    ll = parse(line)
    out = r.line(ll)
    assert "com.example.app" in out
