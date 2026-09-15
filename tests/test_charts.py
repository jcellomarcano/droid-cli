"""Renderables de gráficos: funciones puras, sin I/O, deterministas."""
import pytest
from droid import theme


def test_hbar_zero_value_is_all_empty():
    assert theme.hbar(0, 100, width=10) == "░" * 10


def test_hbar_value_equals_max_is_all_filled():
    assert theme.hbar(100, 100, width=10) == "█" * 10


def test_hbar_value_over_max_clamps_to_full():
    assert theme.hbar(150, 100, width=10) == "█" * 10


def test_hbar_max_value_zero_is_all_empty():
    assert theme.hbar(5, 0, width=10) == "░" * 10


def test_hbar_negative_max_value_is_all_empty():
    assert theme.hbar(5, -10, width=10) == "░" * 10


def test_hbar_width_one_proportional():
    assert theme.hbar(49, 100, width=1) == "░"
    assert theme.hbar(51, 100, width=1) == "█"


def test_hbar_custom_fill_and_empty_chars():
    assert theme.hbar(50, 100, width=4, fill="#", empty="-") == "##--"


def test_spark_labeled_trailer_matches_values():
    text = theme.spark_labeled([1, 2, 3])
    plain = text.plain
    assert plain.startswith(theme.spark([1, 2, 3]))
    assert "mín 1.0" in plain
    assert "máx 3.0" in plain
    assert "media 2.0" in plain


def test_spark_labeled_empty_values_says_sin_datos():
    text = theme.spark_labeled([])
    assert text.plain == "sin datos"


def test_spark_labeled_unit_appended_to_each_number():
    text = theme.spark_labeled([10, 20], unit=" ms")
    assert "mín 10.0 ms" in text.plain
    assert "máx 20.0 ms" in text.plain
    assert "media 15.0 ms" in text.plain


def test_dual_spark_shares_hi_so_b_uses_middle_glyph():
    a_spark, b_spark = theme.dual_spark([0, 10], [0, 5])
    assert a_spark[-1] == theme.BARS[-1]
    assert b_spark[-1] == theme.BARS[3]
    assert b_spark[-1] != theme.BARS[-1]


def test_dual_spark_both_empty_returns_empty_strings():
    assert theme.dual_spark([], []) == ("", "")


def test_histogram_row_count_matches_buckets():
    buckets = [("a", 1.0), ("b", 5.0), ("c", 3.0)]
    table = theme.histogram(buckets, width=10)
    assert table.row_count == 3


def test_histogram_widest_bar_belongs_to_max_bucket():
    buckets = [("a", 1.0), ("b", 5.0), ("c", 3.0)]
    bars = [theme.hbar(v, 5.0, width=10) for _, v in buckets]
    assert bars[1].count("█") == 10
    assert bars[1].count("█") > bars[0].count("█")
    assert bars[1].count("█") > bars[2].count("█")


def test_frame_buckets_boundaries_are_half_open_lt16_16to32_32to48_ge48():
    result = theme.frame_buckets([5, 16, 31.9, 32, 48, 49])
    assert result == [("<16 ms", 1), ("16-32 ms", 2), ("32-48 ms", 1), (">48 ms", 2)]


def test_frame_buckets_empty_input_all_zero():
    assert theme.frame_buckets([]) == [("<16 ms", 0), ("16-32 ms", 0), ("32-48 ms", 0), (">48 ms", 0)]


def test_timeline_strip_priority_crash_wins_over_gc_in_same_slot():
    events = [(0.5, "gc"), (0.5, "crash")]
    text = theme.timeline_strip(events, 0.0, 1.0, width=10)
    assert "×" in text.plain
    assert text.plain.count("×") == 1


def test_timeline_strip_clamps_out_of_range_events_to_edges():
    events = [(-100.0, "gc"), (100.0, "anr")]
    text = theme.timeline_strip(events, 0.0, 1.0, width=5)
    assert text.plain[0] == "·"
    assert text.plain[-1] == "!"


def test_timeline_strip_zero_length_range_is_all_spaces():
    text = theme.timeline_strip([(1.0, "crash")], 5.0, 5.0, width=6)
    assert text.plain == " " * 6


def test_timeline_strip_no_events_is_all_spaces():
    text = theme.timeline_strip([], 0.0, 1.0, width=6)
    assert text.plain == " " * 6


def test_hbar_negative_width_raises_or_returns_empty():
    assert theme.hbar(5, 10, width=0) == ""


def test_frame_buckets_rejects_non_numeric_entries():
    with pytest.raises(TypeError):
        theme.frame_buckets([None])


def test_histogram_empty_buckets_returns_zero_rows():
    table = theme.histogram([], width=10)
    assert table.row_count == 0


def test_dual_spark_rejects_non_iterable_input():
    with pytest.raises(TypeError):
        theme.dual_spark(None, [1, 2])
