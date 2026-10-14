"""Font discovery without silently rewriting user theme preferences."""

import json
import os
import tempfile
from pathlib import Path

import typst
from astrbot.api import logger


class FontManager:
    def __init__(self, font_dirs: list[Path]):
        self.font_dirs = font_dirs
        self.available_families: set[str] = set()

    def scan_fonts(self):
        search_paths = [str(p.resolve()) for p in self.font_dirs if p.exists()]
        try:
            families = set(typst.Fonts(font_paths=search_paths).families())
            self.available_families = families
            logger.info(f"[HelpTypst] 已识别 {len(families)} 个字体家族")
        except Exception:
            # A transient scan failure must not wipe user choices or last scan.
            logger.warning("[HelpTypst] 字体扫描失败，保留上次结果", exc_info=True)

    def update_json_schema(self, schema_path: Path):
        if not schema_path.exists() or not self.available_families:
            return
        temporary = None
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            target = schema["appearance"]["items"]["presets"]["templates"][
                "standard_theme"
            ]["items"]["font_order"]
            choices = sorted(self.available_families)
            if target.get("options") == choices:
                return
            target["options"] = choices
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=schema_path.parent, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(schema, stream, indent=2, ensure_ascii=False)
            os.replace(temporary, schema_path)
        except Exception:
            # Read-only plugin installations can still render using discovered fonts.
            logger.warning("[HelpTypst] 字体选项更新失败，渲染不受影响", exc_info=True)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def get_render_font_list(self, user_config_order: list[str]) -> list[str]:
        fonts = [font for font in user_config_order if font in self.available_families]
        return list(dict.fromkeys([*fonts, "Sarasa Gothic SC", "Custom Help Emoji"]))
