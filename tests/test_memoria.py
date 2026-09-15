"""droid.memoria: parseo de eventos GC de ART y deteccion de fugas de memoria por pendiente."""
from datetime import datetime

from tests.helpers import fixture_text

from droid.inspector import MemInfo
from droid.memoria import GC_RE, GcEvent, LeakDetector, parse_gc_line, parse_gc_message


def test_all_fixture_gc_lines_parse():
    lines = [l for l in fixture_text("logcat_gc").splitlines() if l.strip()]
    events = [parse_gc_line(l) for l in lines]
    assert len(events) == len(lines)
    assert all(e is not None for e in events)


def test_fixture_first_line_values():
    lines = [l for l in fixture_text("logcat_gc").splitlines() if l.strip()]
    e = parse_gc_line(lines[0])
    assert e.freed_kb == 10130
    assert round(e.pause_ms, 3) == 0.881
    assert e.total_ms == 19.749
    assert e.heap_total_kb == 196608
    assert e.kind == "Explicit concurrent mark compact GC"
    assert e.los_kb == 704
    assert e.free_pct == 95
    assert e.heap_used_kb == 9010


def test_parse_gc_line_timestamp_from_date_time():
    lines = [l for l in fixture_text("logcat_gc").splitlines() if l.strip()]
    e = parse_gc_line(lines[0])
    expected = datetime.strptime("2026-09-15 15:09:12.773", "%Y-%m-%d %H:%M:%S.%f").timestamp()
    assert e.t == expected


def test_parse_gc_line_without_year_assumes_current_year():
    line = "09-15 15:09:12.773 13999 14001 I igital.app: Explicit concurrent mark compact GC freed 10130KB AllocSpace bytes, 22(704KB) LOS objects, 95% free, 9010KB/192MB, paused 274us,607us total 19.749ms"
    e = parse_gc_line(line)
    assert e is not None
    assert datetime.fromtimestamp(e.t).year == datetime.now().year


def test_parse_gc_message_bytes_shape_ms_paused():
    msg = "Background concurrent copying GC freed 1234KB AllocSpace bytes, 0(0B) LOS objects, 12% free, 3456KB/4567KB, paused 1.2ms total 5.6ms"
    e = parse_gc_message(msg)
    assert e is not None
    assert e.kind == "Background concurrent copying GC"
    assert e.freed_kb == 1234
    assert e.los_kb == 0
    assert e.free_pct == 12
    assert e.heap_used_kb == 3456
    assert e.heap_total_kb == 4567
    assert e.pause_ms == 1.2
    assert e.total_ms == 5.6


def test_parse_gc_message_objects_shape_mb_heap():
    msg = "Background young concurrent copying GC freed 213(45KB) AllocSpace objects, 0(0B) LOS objects, 40% free, 2MB/4MB, paused 0.5ms,1.1ms total 12ms"
    e = parse_gc_message(msg)
    assert e is not None
    assert e.kind == "Background young concurrent copying GC"
    assert e.freed_kb == 45
    assert e.heap_used_kb == 2048
    assert e.heap_total_kb == 4096
    assert round(e.pause_ms, 3) == 1.6
    assert e.total_ms == 12


def test_parse_gc_message_mb_freed_bytes_shape():
    msg = "Explicit concurrent copying GC freed 1.5MB AllocSpace bytes, 0(0B) LOS objects, 50% free, 1MB/2MB, paused 100us total 200us"
    e = parse_gc_message(msg)
    assert e is not None
    assert e.freed_kb == 1536
    assert e.heap_used_kb == 1024
    assert e.heap_total_kb == 2048
    assert round(e.pause_ms, 3) == 0.1
    assert round(e.total_ms, 3) == 0.2


def test_parse_gc_message_negative_control_non_gc_line():
    assert parse_gc_message("just a regular log message about something else") is None


def test_parse_gc_message_negative_control_garbage():
    assert parse_gc_message("!!! not even close to a gc line 1234 !!!") is None


def test_parse_gc_line_negative_control_non_logcat_line():
    assert parse_gc_line("not a logcat line at all") is None


def test_parse_gc_line_negative_control_non_gc_logcat_line():
    line = "2026-09-15 15:03:13.450 10362 10362 E AndroidRuntime: FATAL EXCEPTION: main"
    assert parse_gc_line(line) is None


def test_gc_re_matches_first_fixture_line_message():
    msg = ("Explicit concurrent mark compact GC freed 10130KB AllocSpace bytes, 22(704KB) LOS objects, "
           "95% free, 9010KB/192MB, paused 274us,607us total 19.749ms")
    assert GC_RE.match(msg) is not None


def _rising_points(n=8, step=20.0, start=1000.0, growth=1000.0):
    return [(i * step, start + i * growth) for i in range(n)]


def test_leak_detector_flags_rising_series():
    detector = LeakDetector()
    points = _rising_points()
    flag = detector.check(points, metric="java_heap_kb")
    assert flag is not None
    assert flag.slope_kb_per_min > 0
    assert flag.samples == 8
    assert flag.gc_backed is False


def test_leak_detector_does_not_flag_flat_series():
    detector = LeakDetector()
    points = [(i * 20.0, 1000.0) for i in range(8)]
    assert detector.check(points) is None


def test_leak_detector_does_not_flag_short_series():
    detector = LeakDetector()
    points = _rising_points(n=5)
    assert detector.check(points) is None


def test_leak_detector_does_not_flag_growth_under_thresholds():
    detector = LeakDetector()
    points = [(i * 20.0, 100000.0 + i * 100.0) for i in range(8)]
    assert detector.check(points) is None


def test_leak_detector_gc_backed_baseline_return_does_not_flag():
    detector = LeakDetector()
    points = [
        (0.0, 1000.0), (20.0, 1000.0), (40.0, 1000.0), (60.0, 1000.0),
        (80.0, 1000.0), (100.0, 8000.0), (120.0, 1020.0), (140.0, 9000.0),
    ]
    gcs = [GcEvent(t=100.0, kind="Explicit concurrent mark compact GC", freed_kb=7000.0, los_kb=0.0,
                    free_pct=90, heap_used_kb=1000.0, heap_total_kb=200000.0, pause_ms=1.0, total_ms=2.0)]
    assert detector.check(points, gcs) is None


def test_leak_detector_gc_backed_flags_when_growth_persists():
    detector = LeakDetector()
    points = [
        (0.0, 1000.0), (20.0, 1000.0), (40.0, 1000.0), (60.0, 1000.0),
        (80.0, 1000.0), (100.0, 8000.0), (120.0, 8200.0), (140.0, 9000.0),
    ]
    gcs = [GcEvent(t=100.0, kind="Explicit concurrent mark compact GC", freed_kb=100.0, los_kb=0.0,
                    free_pct=90, heap_used_kb=8000.0, heap_total_kb=200000.0, pause_ms=1.0, total_ms=2.0)]
    flag = detector.check(points, gcs)
    assert flag is not None
    assert flag.gc_backed is True


def test_parse_gc_line_sets_pid_from_logline():
    lines = [l for l in fixture_text("logcat_gc").splitlines() if l.strip()]
    e = parse_gc_line(lines[0])
    assert e.pid == 13999


def test_leak_detector_window_ignores_old_growth_flat_recently():
    detector = LeakDetector()
    # crecio hace 20 min (t=0..1200s) y luego quedo plana los ultimos 3 min (t=1200..1380s, ventana 180s)
    growth = [(i * 60.0, 1000.0 + i * 1000.0) for i in range(21)]  # 0..1200s
    flat = [(1200.0 + i * 30.0, growth[-1][1]) for i in range(1, 7)]  # 1230..1380s, plano
    points = growth + flat
    flag = detector.check(points, window_s=180.0)
    assert flag is None


def test_leak_detector_window_flags_recent_growth():
    detector = LeakDetector()
    old_flat = [(i * 60.0, 1000.0) for i in range(15)]  # 0..840s plano
    recent_growth = [(900.0 + i * 20.0, 1000.0 + i * 1000.0) for i in range(7)]  # 900..1020s (dentro de 180s de la ultima)
    points = old_flat + recent_growth
    flag = detector.check(points, window_s=180.0)
    assert flag is not None
    assert flag.window_s == 180.0
    assert flag.since_t >= points[-1][0] - 180.0


def test_detect_all_flags_java_heap_not_native():
    detector = LeakDetector()
    meminfos = [
        MemInfo(t=i * 20.0, java_heap_kb=1000 + i * 1000, native_heap_kb=5000, pss_total_kb=5000)
        for i in range(8)
    ]
    flags = detector.detect_all(meminfos)
    metrics = {f.metric for f in flags}
    assert "java_heap_kb" in metrics
    assert "native_heap_kb" not in metrics
