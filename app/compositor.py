"""Deterministic layouts and translated labels shared by every image entrance."""
import math
import numpy as np
from PIL import Image, ImageDraw

from .data.errors import QueryLimitError
from .renderer import _curve_range, _font, _quat_matrix, _series_values
from .viewstate import resolve_frame

LABELS = {
    "en": dict(frame="Frame", task="Task", unknown="Unavailable", points="Hand points", time="Time", mean="Mean", scalar="Scalar", component="Raw component", unit="Unknown unit", episode="Episode"),
    "zh": dict(frame="帧", task="任务", unknown="未提供", points="手部点", time="时间", mean="均值", scalar="标量", component="原始分量", unit="单位未知", episode="片段"),
}


def wrapped(draw, value, x, y, width, font, color, lines=2):
    line = ""; count = 0
    for char in str(value):
        if draw.textlength(line + char, font=font) > width and line:
            draw.text((x, y), line, font=font, fill=color); y += font.size + 3
            count += 1; line = ""
            if count >= lines:
                return y
        line += char
    draw.text((x, y), line, font=font, fill=color)
    return y + font.size + 4


def compose(adapter, state, video, *, info_bar=None, series_cache=None):
    width, height = state.render["width"], state.render["height"]
    display = state.display or {"locale": "en", "info_bar": True}
    labels = LABELS[display["locale"]]
    show_info = display["info_bar"] if info_bar is None else info_bar
    frame = resolve_frame(state, adapter); table = adapter.table(state.episode); row = adapter.row(state.episode, frame)
    start, end, source, expanded = _curve_range(table, state, frame)
    canvas = Image.new("RGB", (width, height), (20, 23, 29)); draw = ImageDraw.Draw(canvas)
    pad, gap = (24, 24) if width >= 1400 else (16, 16)
    count = len(state.series) if "series" in state.panels else 0
    columns = 2 if count > 1 and width >= 600 else 1
    rows = math.ceil(count / columns) if count else 0
    top = "video" in state.panels or "metadata" in state.panels
    top_h = (480 if width >= 1400 and height >= 1100 else int(height * .60)) if top else 0
    plot_y = pad + top_h + (gap if top and count else 0)
    plot_h = (height - pad - plot_y - gap * max(0, rows - 1)) // max(1, rows)
    if count and plot_h < 92:
        raise QueryLimitError("LayoutTooSmall: use the 1600x1200 multi-field preset")
    video_box_w = int((width - pad * 2 - gap) * .62) if width < 1400 else (width - pad * 2 - gap) // 2
    scale = min(video_box_w / video.width, max(1, top_h) / video.height)
    vw, vh = max(1, round(video.width * scale)), max(1, round(video.height * scale))
    vx, vy = pad, pad
    manifest = dict(episode=state.episode, frame=frame, cursor_frame=frame, timestamp_ns=int(row["timestamp_ns"]),
                    time_range=dict(state.time_range) if state.time_range else None,
                    range_start_frame=start, range_end_frame=end, range_source=source, range_expanded=expanded,
                    width=width, height=height, panels=list(state.panels), video_rendered="video" in state.panels,
                    metadata_rendered="metadata" in state.panels, series_rendered="series" in state.panels,
                    overlay={"projected_points": 0, "outside_canvas": 0,"total_points":0,"nonfinite_points":0,"behind_camera_points":0,"outside_image_points":0,"drawn_points":0,
                             "classification":"mutually exclusive: nonfinite -> z<=0 -> outside calibrated image center -> drawn",
                             "legacy_semantics":"projected_points=total_points; outside_canvas=nonfinite+behind_camera+outside_image, NOT whole PNG boundary",
                             "source_frame":frame,"source_fields":[],"visibility":"unknown; drawn does not establish visibility or pixel alignment"}, series=[], display=display,
                    layout="multi_field" if width >= 1400 else "standard", renderer_version="png-player-v2",
                    video_rect=[vx, vy, vw, vh], task=adapter.episodes[state.episode].tasks[0] if adapter.episodes[state.episode].tasks else None)
    manifest.update(decoded_video_size=[video.width,video.height],
                    calibration_image_size=adapter.info.get("features",{}).get("base_0_camera/resolution"),
                    overlay_transform=None)
    if "video" in state.panels:
        canvas.paste(video.resize((vw, vh), Image.Resampling.LANCZOS), (vx, vy))
        features = adapter.info.get("features", {})
        try:
            k = np.asarray(features["base_0_camera/intrinsics"], dtype=np.float64)
            iw, ih = features["base_0_camera/resolution"]
            manifest.update(calibration_image_size=[iw,ih],decoded_video_size=[video.width,video.height],
                            overlay_transform={"scale_x":vw/iw,"scale_y":vh/ih,"translate_x":vx,"translate_y":vy,"crop":None,
                                               "domain":"calibrated image pixel centers, not composed PNG"})
            position = np.asarray(row["base_0_camera/position"], dtype=np.float64)[1:]
            rotation = _quat_matrix(row["base_0_camera/quaternion_xyzw"][1:])
            sides = {s for o in state.overlays for s in o["sides"]}
            for side in sorted(sides):
                manifest["overlay"]["source_fields"].append(f"{side}_hand/tracks")
                points = np.asarray(row[f"{side}_hand/tracks"], dtype=np.float64)[1:].reshape(21, 3)
                camera = (points - position) @ rotation; projected = camera @ k.T
                with np.errstate(divide="ignore", invalid="ignore"):
                    pixels = projected[:, :2] / projected[:, 2:3]
                for index, (x, y) in enumerate(pixels):
                    manifest["overlay"]["projected_points"] += 1
                    manifest["overlay"]["total_points"] += 1
                    category = ("nonfinite_points" if not np.isfinite(points[index]).all() or not np.isfinite(camera[index]).all() else
                                "behind_camera_points" if camera[index,2]<=0 else
                                "nonfinite_points" if not np.isfinite([x,y]).all() else
                                "outside_image_points" if not (0<=x<iw and 0<=y<ih) else "drawn_points")
                    manifest["overlay"][category]+=1
                    if category!="drawn_points":
                        manifest["overlay"]["outside_canvas"]+=1; continue
                    xx, yy = vx + x / iw * vw, vy + y / ih * vh
                    draw.ellipse((xx - 3, yy - 3, xx + 3, yy + 3), fill=(70, 255, 120) if side == "left" else (255, 130, 60), outline="white")
        except KeyError:
            pass
        if show_info:
            # Crop-sized alpha bar avoids several full-canvas RGBA temporaries.
            bh = min(58, vh); bar = Image.new("RGBA", (vw, bh), (10, 15, 22, 190)); bd = ImageDraw.Draw(bar)
            wrapped(bd, f"{labels['task']}: {manifest['task'] or labels['unknown']}", 10, 3, vw - 20, _font(13), "white", 1)
            bd.text((10, 28), f"{labels['frame']} {frame}   timestamp_ns {row['timestamp_ns']}", font=_font(12), fill="white")
            crop = canvas.crop((vx, vy + vh - bh, vx + vw, vy + vh)).convert("RGBA")
            canvas.paste(Image.alpha_composite(crop, bar).convert("RGB"), (vx, vy + vh - bh))
        draw.rectangle((vx, vy, vx + vw, vy + vh), outline=(75, 89, 108))
    if "metadata" in state.panels:
        mx = pad + video_box_w + gap if "video" in state.panels else pad
        draw.text((mx, pad), "LeRobot Visualizer", font=_font(20), fill=(235, 242, 255))
        y = pad + 38
        for value in (f"{labels['episode']}: {state.episode}", f"{labels['frame']}: {frame}", f"timestamp_ns: {row['timestamp_ns']}",
                      f"{labels['task']}: {manifest['task'] or labels['unknown']}",
                      f"{labels['points']}: {manifest['overlay']['projected_points'] - manifest['overlay']['outside_canvas']}/{manifest['overlay']['projected_points']}"):
            y = wrapped(draw, value, mx, y, width - pad - mx, _font(13), (193, 207, 225)) + 5
    catalog = {f.name: f for f in adapter.fields()} if count else {}
    colors = ((96, 170, 255), (255, 175, 80), (170, 135, 255), (90, 220, 180), (255, 130, 160), (150, 220, 80), (80, 215, 230), (215, 200, 140))
    for no, field in enumerate(state.series if count else ()):
        values = series_cache[field][start:end] if series_cache is not None and field in series_cache else _series_values(table, field, start, end)
        finite = np.isfinite(values)
        if not finite.any(): continue
        lo, hi = float(np.min(values[finite])), float(np.max(values[finite]))
        slo, shi = (lo - .5, hi + .5) if math.isclose(lo, hi) else (lo, hi)
        pw = (width - 2 * pad - gap * (columns - 1)) // columns
        x = pad + no % columns * (pw + gap); y = plot_y + no // columns * (plot_h + gap)
        color = colors[no]
        draw.rounded_rectangle((x, y, x + pw, y + plot_h), radius=5, fill=(24, 30, 39), outline=(63, 76, 95))
        wrapped(draw, field, x + 9, y + 3, pw - 18, _font(12), color, 1)
        base = field.split("[")[0]; descriptor = catalog.get(base)
        reduction = labels["component"] if "[" in field else labels["mean"] if descriptor and descriptor.observed_length else labels["scalar"]
        unit = descriptor.units if descriptor and descriptor.units else {"timestamp_ns": "ns", "timestamp": "s"}.get(base, labels["unit"])
        if field.endswith("[0]") and base in {"base_0_camera/position", "base_0_camera/quaternion_xyzw", "left_hand/tracks", "right_hand/tracks"}:
            unit = "ns"
        current = float(values[min(max(frame - start, 0), len(values) - 1)])
        draw.text((x + 9, y + 22), f"{reduction} | {unit} | {current:.5g}", font=_font(11), fill=(190, 203, 220))
        ax, ay, aw, ah = x + 62, y + 43, pw - 75, plot_h - 65
        for fraction in (0., .5, 1.):
            yy = ay + fraction * ah
            draw.line((ax, yy, ax + aw, yy), fill=(48, 58, 72))
            draw.text((x + 5, yy - 6), f"{shi - fraction * (shi - slo):.3g}", font=_font(10), fill=(165, 181, 202))
        points = [(ax + aw / 2 if len(values) == 1 else ax + i / (len(values) - 1) * aw,
                   ay + (shi - float(v)) / (shi - slo) * ah) for i, v in enumerate(values)]
        if len(points) == 1:
            xx, yy = points[0]; draw.ellipse((xx - 3, yy - 3, xx + 3, yy + 3), fill=color)
        else: draw.line(points, fill=color, width=2)
        if start <= frame < end:
            xx = ax + aw / 2 if end - start == 1 else ax + (frame - start) / (end - start - 1) * aw
            draw.line((xx, ay, xx, ay + ah), fill=(240, 245, 255))
        for index, anchor in ((start, ax), (end - 1, ax + aw - 50)):
            draw.text((anchor, y + plot_h - 18), f"{int(table['timestamp_ns'][index].as_py()) / 1e9:.3f}s", font=_font(10), fill=(165, 181, 202))
        manifest["series"].append(dict(field=field, start_frame=start, end_frame=end, min=lo, max=hi, current=current, unit=unit, reduction=reduction, rect=[x, y, pw, plot_h]))
    return canvas, manifest
