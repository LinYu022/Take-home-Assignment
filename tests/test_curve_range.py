import pyarrow as pa
import pytest

from app.renderer import _curve_range
from app.viewstate import normalize_view


@pytest.mark.parametrize("kind,page,expected", [
    ("series_query", (1514, 1524), (1514, 1524, "query_page", False)),
    ("series_query", (1524, 1534), (1524, 1534, "query_page", False)),
    ("series_query", (1534, 1544), (1534, 1544, "query_page", False)),
    ("series_query", (1543, 1544), (1543, 1544, "query_page", False)),
    ("aggregate_series", (1514, 1544), (1514, 1544, "aggregate_input", False)),
])
def test_evidence_window_is_exact_and_precedes_time_range(kind, page, expected):
    table = pa.table({"timestamp_ns": list(range(3029))})
    state = normalize_view({"time_range": {"start_ns": 100, "end_ns": 200}, "series": ["value"],
                            "evidence": {"kind": kind, "field": "value", "component": None,
                                         "requested_frame_range": {"start": 1514, "end": 1544},
                                         "returned_frame_range": {"start": page[0], "end": page[1]},
                                         "page_offset": page[0] - 1514}},
                           episode_count=1, episode_lengths={0: 3029}, fields=["value"],
                           episode_time_ranges={0: (0, 3029)})
    assert _curve_range(table, state, 100) == expected


def test_without_evidence_retains_time_and_default_cursor_windows():
    table = pa.table({"timestamp_ns": list(range(3029))})
    context = dict(episode_count=1, episode_lengths={0: 3029}, fields=["value"],
                   episode_time_ranges={0: (0, 3029)})
    state = normalize_view({"time_range": {"start_ns": 1514, "end_ns": 1544}}, **context)
    assert _curve_range(table, state, 1514) == (1514, 1544, "time_range", False)
    state = normalize_view({"cursor": {"frame": 1514}}, **context)
    assert _curve_range(table, state, 1514) == (1274, 1755, "cursor_window", False)
