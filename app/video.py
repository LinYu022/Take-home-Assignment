from pathlib import Path
import threading
import av
import numpy as np
from .data.errors import DataContractError, DataError

class StaleGeneration(DataError):
    pass


class IndexedDecoder:
    """One owner/lock per decoder; seek identity is checked against actual PTS."""
    def __init__(self, path: Path, expected_rows: int, index=None, expected_dimensions=None):
        self.path = path
        if index is None:
            with av.open(str(path)) as container:
                stream = container.streams.video[0]; stream.thread_type = "SLICE"; stream.codec_context.thread_count = 1
                if expected_dimensions is not None:
                    if (stream.width, stream.height) != tuple(expected_dimensions):
                        raise DataContractError("video dimensions differ from reserved metadata")
                    if stream.codec_context.name != "h264" or stream.codec_context.pix_fmt not in {"yuv420p", "yuvj420p"}:
                        raise DataContractError("cannot reserve codec pixels for this video format; expected 8-bit H264 4:2:0")
                frames = [(int(frame.pts), bool(frame.key_frame)) for frame in container.decode(video=0)]
                self.time_base = stream.time_base
                self.width, self.height = stream.width, stream.height
            if len(frames) != expected_rows or not frames or any(a[0] >= b[0] for a, b in zip(frames, frames[1:])):
                raise DataContractError("Video PTS/count cannot be mapped one-to-one to episode rows")
            self.pts = np.asarray([x[0] for x in frames], dtype=np.int64); self.pts.setflags(write=False)
            self.pts_to_frame = {int(pts): i for i, pts in enumerate(self.pts)}
            self.keyframes = [i for i, entry in enumerate(frames) if entry[1]]
        else:
            self.pts, self.pts_to_frame, self.keyframes, self.time_base, self.width, self.height = index
        self.index = (self.pts, self.pts_to_frame, self.keyframes, self.time_base, self.width, self.height)
        self.container = av.open(str(path))
        self.stream = self.container.streams.video[0]
        self.stream.thread_type = "SLICE"; self.stream.codec_context.thread_count = 1
        self.iterator = iter(self.container.decode(video=0))
        self.last = -1
        self.lock = threading.Lock()
        self.closed = False

    def get(self, frame_index, valid=lambda: True):
        with self.lock:
            if self.closed or not valid():
                raise StaleGeneration("decoder closed")
            if not 0 <= frame_index < len(self.pts):
                raise ValueError("frame is out of range")
            if not self.last < frame_index <= self.last + 8:
                self.container.seek(int(self.pts[frame_index]), stream=self.stream, backward=True, any_frame=False)
                self.iterator = iter(self.container.decode(video=0))
            for frame in self.iterator:
                index = self.pts_to_frame.get(int(frame.pts))
                if index is None:
                    raise DataContractError("Decoded PTS is absent from the index")
                self.last = index
                if index == frame_index:
                    return frame.to_image().convert("RGB"), int(frame.pts)
                if index > frame_index:
                    raise DataContractError("Seek skipped the requested frame")
            raise DataContractError("Requested video frame is unavailable")

    def close(self):
        # Active get() owns this same lock; its resource cannot be closed underneath it.
        with self.lock:
            if not self.closed:
                self.container.close()
                self.closed = True

