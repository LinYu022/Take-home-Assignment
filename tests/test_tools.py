import unittest

from app.tools import call_tool, tool_definitions
from app.data.errors import QueryLimitError


class Column:
    def __init__(self, values):
        self.values = values

    def slice(self, start, length):
        return Column(self.values[start:start + length])

    def to_pylist(self):
        return self.values


class Table:
    def __init__(self):
        self.data = {
            "timestamp_ns": [i * 100 for i in range(8)],
            "frame_index": list(range(8)),
            "base_0_camera/position": [[i, i + 1, i + 2] for i in range(8)],
            "action.left_hand_tracks": [[i, i + 1] for i in range(8)],
        }
        self.num_rows = 8

    def __getitem__(self, name):
        return Column(self.data[name])


class Episode:
    episode_index = 0
    length = 8
    tasks = ["fixture task"]
    start_ns = 0
    end_ns = 800
    data_start_ns = 0
    data_end_ns = 800
    video_start_ns = 0
    video_end_ns = 800
    frame_period_ns = 100

    def as_dict(self):
        return {"episode_index": 0, "length": 8, "tasks": ["fixture task"], "data_start_ns": 0, "data_end_ns": 800, "video_start_ns": 0, "video_end_ns": 800, "frame_period_ns": 100}


class Field:
    def __init__(self, name):
        self.name = name


class Adapter:
    episodes = [Episode()]
    info = {"features": {}}

    def table(self, episode):
        return Table()

    def fields(self):
        return [Field(x) for x in Table().data]

    def manifest(self):
        return {"episodes": [{"episode_index": 0, "length": 8}], "fields": [x.name for x in self.fields()]}


class Settings:
    public_base_url = "http://127.0.0.1:8000"
    max_render_width = 1600
    max_render_height = 1200
    max_query_rows = 3
    max_query_scalars = 10
    max_query_bytes = 10000
    max_view_url_chars = 16384
    max_aggregate_rows = 1000000


class ToolContractTests(unittest.TestCase):
    def test_discovery_contains_required_tools(self):
        names = {x["name"] for x in tool_definitions()}
        self.assertEqual(names, {"describe_dataset", "list_episodes", "query_series", "aggregate_series", "encode_view", "decode_view", "render_view", "compare_series", "audit_timing", "audit_integrity"})

    def test_query_is_bounded_and_continuable(self):
        result = call_tool("query_series", {"episode": 0, "field": "action.left_hand_tracks", "component": 0, "limit": 3}, Adapter(), Settings(), None)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_cursor"], 3)
        self.assertEqual(len(result["values"]), 3)
        self.assertIn("view_url", result)
        continued = call_tool("query_series", {"episode": 0, "field": "action.left_hand_tracks", "component": 0, "limit": 3, "cursor": result["next_cursor"]}, Adapter(), Settings(), None)
        self.assertEqual(continued["start_frame"], 3)
        self.assertEqual(continued["values"][0], 3)

    def test_query_view_url_preserves_request(self):
        settings = Settings()
        settings.max_query_rows = 10
        result = call_tool("query_series", {"episode": 0, "field": "action.left_hand_tracks", "component": 0, "start_frame": 2, "end_frame": 6, "limit": 10}, Adapter(), settings, None)
        from app.viewstate import decode_view
        state = decode_view(result["view_url"], episode_count=1, episode_lengths={0: 8}, fields=[x.name for x in Adapter().fields()], episode_time_ranges={0: (0, 800)})
        self.assertEqual(state.evidence["component"], 0)
        self.assertEqual(state.evidence["requested_frame_range"], {"start": 2, "end": 6})
        self.assertEqual(state.evidence["returned_frame_range"], {"start": 2, "end": 6})

    def test_aggregate_is_exact_and_bounded(self):
        result = call_tool("aggregate_series", {"episode": 0, "field": "action.left_hand_tracks", "component": 0}, Adapter(), Settings(), None)
        self.assertTrue(result["exact"])
        self.assertEqual(result["count"], 8)

    def test_complete_response_budget_includes_timestamps_and_url(self):
        settings = Settings()
        settings.max_query_rows = 10
        settings.max_query_bytes = 300
        with self.assertRaises(QueryLimitError):
            call_tool("query_series", {"episode": 0, "field": "action.left_hand_tracks", "component": 0, "start_frame": 2, "end_frame": 6, "limit": 10}, Adapter(), settings, None)


if __name__ == "__main__":
    unittest.main()

