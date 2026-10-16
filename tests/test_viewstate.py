import unittest

from app.viewstate import ViewStateError, decode_view, encode_view, normalize_view


class ViewStateTests(unittest.TestCase):
    fields = ["base_0_camera/position", "action.left_hand_tracks"]
    kwargs = {"episode_count": 1, "episode_lengths": {0: 3029}, "fields": fields}

    def test_canonical_round_trip(self):
        state = normalize_view({"episode": 0, "cursor": {"frame": 1514}, "series": self.fields}, **self.kwargs)
        url = encode_view(state, "http://127.0.0.1:8000")
        decoded = decode_view(url, **self.kwargs)
        self.assertEqual(state.to_dict(), decoded.to_dict())

    def test_rejects_mutually_exclusive_cursor_and_range(self):
        with self.assertRaises(ViewStateError):
            normalize_view({"cursor": {"frame": 1}, "time_range": {"start_ns": 0, "end_ns": 1}}, **self.kwargs)

    def test_rejects_bad_field_and_frame(self):
        with self.assertRaises(ViewStateError):
            normalize_view({"cursor": {"frame": 4000}, "series": ["missing"]}, **self.kwargs)

    def test_time_range_is_strictly_bounded(self):
        kwargs = {**self.kwargs, "episode_time_ranges": {0: (1000, 9000)}}
        state = normalize_view({"cursor": None, "time_range": {"start_ns": 2000, "end_ns": 3000}}, **kwargs)
        self.assertEqual(state.time_range, {"start_ns": 2000, "end_ns": 3000})
        with self.assertRaises(ViewStateError):
            normalize_view({"cursor": None, "time_range": {"start_ns": 9000, "end_ns": 9001}}, **kwargs)

    def test_overlay_and_url_limits(self):
        with self.assertRaises(ViewStateError):
            normalize_view({"overlays": [{"type": "hand_tracks", "sides": ["left", "right"]}] * 9}, **self.kwargs)
        state = normalize_view({"cursor": {"frame": 1}, "series": [self.fields[0] + "[0]"], "evidence": {"kind": "series_query", "field": self.fields[0], "component": 0, "requested_frame_range": {"start": 1, "end": 3}, "returned_frame_range": {"start": 1, "end": 2}, "page_offset": 0, "page_limit": 1}}, **self.kwargs)
        with self.assertRaises(ViewStateError):
            encode_view(state, "http://127.0.0.1:8000", max_url_chars=10)

    def test_aggregate_evidence_requires_the_complete_input_range(self):
        raw = {"series": [self.fields[0] + "[0]"], "evidence": {
            "kind": "aggregate_series", "field": self.fields[0], "component": 0,
            "requested_frame_range": {"start": 1, "end": 4},
            "returned_frame_range": {"start": 1, "end": 3}, "page_offset": 0}}
        with self.assertRaisesRegex(ViewStateError, "aggregate evidence requested and returned ranges must match"):
            normalize_view(raw, **self.kwargs)
        raw["evidence"]["returned_frame_range"]["end"] = 4
        state = normalize_view(raw, **self.kwargs)
        self.assertEqual(decode_view(encode_view(state, "http://localhost"), **self.kwargs), state)


if __name__ == "__main__":
    unittest.main()

