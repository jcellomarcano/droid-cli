"""droid.inspector: parseo de /proc/stat, dumpsys meminfo/gfxinfo, exit-info y el sampler rapido."""
import pytest
from tests.helpers import fixture_text

from droid import inspector as insp


def test_parse_stat_line_from_proc_pid_stat():
    line = "10362 (igital.app) S 24285 24285 0 0 -1 4194624 48727 0 148 0 148 74 0 0 -2 0 26 0 31262609 7377362944 62111 18446744073709551615 1 1 0 0 0 0 4612 1 1073775864 0 0 0 17 2 1 1 0 0 0 0 0 0 0 0 0 0 0"
    tid, comm, state, ticks = insp.parse_stat_line(line)
    assert (tid, comm, state, ticks) == (10362, "igital.app", "S", 222)


def test_parse_stat_line_task_path_form():
    line = "/proc/10362/task/10363/stat:10363 (Signal Catcher) S 24285 24285 0 0 -1 4194368 54 0 0 0 0 0 0 0 20 0 26 0 31262613 7377362944 62111 18446744073709551615 1 1 0 0 0 0 20480 1 1073775864 0 0 0 -1 1 0 0 0 0 0 0 0 0 0 0 0 0 0"
    tid, comm, state, ticks = insp.parse_stat_line(line)
    assert (tid, comm, state) == (10363, "Signal Catcher", "S")


def test_parse_stat_line_garbage_returns_none():
    assert insp.parse_stat_line("not a stat line") is None
    assert insp.parse_stat_line("") is None


def test_parse_meminfo_real_fixture():
    mi = insp.parse_meminfo(fixture_text("meminfo_app"))
    assert mi.pss_total_kb == 128272
    assert mi.rss_total_kb == 255416
    assert mi.java_heap_kb == 23048
    assert mi.native_heap_kb == 4516
    assert mi.views == 18
    assert mi.view_roots == 1
    assert mi.error == ""


def test_parse_meminfo_timeout_sets_error():
    mi = insp.parse_meminfo("IOException while dumping meminfo\nTimeout waiting for meminfo")
    assert mi.error == "timeout de dumpsys meminfo"
    assert mi.pss_total_kb == 0


def test_parse_meminfo_empty_text_returns_zeroed():
    mi = insp.parse_meminfo("")
    assert mi.pss_total_kb == 0
    assert mi.error == ""


def test_parse_gfxinfo_real_fixture():
    out = insp.parse_gfxinfo(fixture_text("gfxinfo_app"))
    assert out["total_frames"] == 29
    assert out["janky"] == 2
    assert out["p50"] == 17
    assert out["p99"] == 150
    assert out["missed_vsync"] == 0
    assert out["slow_ui"] == 1


def test_parse_gfxinfo_no_process_found_returns_empty_dict():
    assert insp.parse_gfxinfo(fixture_text("gfxinfo_systemui")) == {}


def test_parse_exit_info_real_fixture_reports_crash():
    records = insp.parse_exit_info(fixture_text("exit_info_app_after_crash"))
    assert len(records) == 1
    rec = records[0]
    assert rec.pid == 10362
    assert rec.description == "crash"
    assert rec.timestamp == "2026-09-15 15:03:13.455"
    assert rec.importance == 100
    assert rec.rss == "0.00"


@pytest.mark.xfail(strict=True, reason=(
    "droid/inspector.py:234 reason=(\\d+) \\((\\w+)\\) subreason=\\d+ \\((\\w+)\\) status=(\\d+) requires "
    "single-word parenthesized reason/subreason text; real dumpsys output like "
    "'reason=4 (APP CRASH(EXCEPTION)) subreason=0 (UNKNOWN) status=0' has a space and a nested "
    "paren, so re.search finds no match at all and reason/subreason/status silently default to "
    "0/''/0 instead of the real values (reason should be 4/CRASH, not 0/UNKNOWN)."
))
def test_parse_exit_info_multiword_reason_is_dropped_bug():
    records = insp.parse_exit_info(fixture_text("exit_info_app_after_crash"))
    rec = records[0]
    assert rec.reason == 4
    assert rec.reason_name == "CRASH"


def test_parse_exit_info_garbage_returns_empty_list():
    assert insp.parse_exit_info("nothing relevant here\njust noise") == []
    assert insp.parse_exit_info("") == []


def test_fast_sample_script_includes_pss_when_debuggable():
    script = insp.fast_sample_script(1234, "com.example.app", debuggable=True)
    assert "/proc/1234/stat" in script
    assert "run-as com.example.app cat /proc/1234/smaps_rollup" in script
    assert "##PSS" in script


def test_fast_sample_script_omits_pss_when_not_debuggable():
    script = insp.fast_sample_script(1234, "com.example.app", debuggable=False)
    assert "##PSS" not in script
    assert "smaps_rollup" not in script


def test_parse_fast_sample_real_fixture():
    total_ticks, ncpu, proc, threads, status, oom, pss, gfx = insp.parse_fast_sample(fixture_text("fast_sample_app"))
    assert ncpu == 4
    assert proc[0] == 10362
    assert proc[2] == "S"
    assert len(threads) == 26
    assert status["VmRSS"] == 248948
    assert status["Threads"] == 26
    assert oom == 0
    assert pss["Pss"] == 121747
    assert gfx["total_frames"] == 29


def test_parse_fast_sample_empty_text_returns_defaults():
    total_ticks, ncpu, proc, threads, status, oom, pss, gfx = insp.parse_fast_sample("")
    assert total_ticks == 0
    assert ncpu == 1
    assert proc is None
    assert threads == []
    assert status == {}
    assert oom is None
    assert pss == {}
    assert gfx == {}


def test_oom_label_known_and_none():
    assert insp.oom_label(None) == "?"
    assert insp.oom_label(0) == "primer plano"
    assert insp.oom_label(1000) == "en caché"
    assert insp.oom_label(99999) == "en caché"
