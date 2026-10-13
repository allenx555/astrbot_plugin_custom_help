import json

from astrbot_plugin_custom_help.utils import font as module
from astrbot_plugin_custom_help.utils.font import FontManager


def test_schema_update_is_atomic_and_preserves_source_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps(
            {
                "appearance": {
                    "items": {
                        "presets": {
                            "templates": {
                                "standard_theme": {
                                    "items": {"font_order": {"options": ["Original"]}}
                                }
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    fonts = FontManager([])
    fonts.available_families = {"NewFont"}

    def fail(*args):
        raise OSError("read-only plugin")

    monkeypatch.setattr(module.os, "replace", fail)
    fonts.update_json_schema(path)
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_failed_font_scan_preserves_previous_discovery(monkeypatch):
    fonts = FontManager([])
    fonts.available_families = {"CustomFont"}

    def fail(**kwargs):
        raise RuntimeError("font scan failure")

    monkeypatch.setattr(module.typst, "Fonts", fail)
    fonts.scan_fonts()
    assert fonts.available_families == {"CustomFont"}
    preferences = ["TemporarilyMissing", "CustomFont", "CustomFont"]
    assert fonts.get_render_font_list(preferences) == [
        "CustomFont",
        "Sarasa Gothic SC",
        "Noto Color Emoji",
    ]
    assert preferences == ["TemporarilyMissing", "CustomFont", "CustomFont"]
