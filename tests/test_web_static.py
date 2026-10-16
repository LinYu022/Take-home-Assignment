from app.web import INDEX_HTML
from pathlib import Path


def test_browser_uses_server_viewstate_and_exposes_m3_controls():
    assert "atob(" not in INDEX_HTML
    script = (Path(__file__).parents[1] / "app/assets/player.js").read_text(encoding="utf-8")
    assert "/api/view/normalize" in script and "atob(" not in script
    for element_id in ("panelVideo", "panelSeries", "panelMetadata", "series", "left", "right", "startNs", "endNs"):
        assert f'id="{element_id}"' in INDEX_HTML
    assert "createImageBitmap" in script and "bad_image" in script

