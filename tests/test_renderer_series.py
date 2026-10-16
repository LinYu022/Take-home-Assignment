import numpy as np
import pyarrow as pa

from app.renderer import _series_values


def test_whole_vectors_exclude_embedded_timestamp_but_components_keep_raw_index():
    table = pa.table({"base_0_camera/position": [[100000000, 1., 2., 3.], [200000000, 2., 4., 6.]],
                      "action.left_hand_tracks": [[1., 2., 3.], [2., 4., 6.]]})
    np.testing.assert_array_equal(_series_values(table, "base_0_camera/position", 0, 2), [2., 4.])
    np.testing.assert_array_equal(_series_values(table, "base_0_camera/position[0]", 0, 2), [100000000, 200000000])
    np.testing.assert_array_equal(_series_values(table, "action.left_hand_tracks", 0, 2), [2., 4.])
