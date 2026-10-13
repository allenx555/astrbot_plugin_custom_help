"""Regression coverage for the user's real editable menu (2026-10-14)."""

import asyncio
import io
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import typst
from astrbot_plugin_custom_help.core.menu import MenuDocument, MenuStore
from astrbot_plugin_custom_help.core.renderer import TypstRenderer
from astrbot_plugin_custom_help.domain import TypstPluginConfig
from astrbot_plugin_custom_help.main import HelpTypst
from astrbot_plugin_custom_help.utils import FontManager, TypstLayout
from PIL import Image, ImageChops, ImageStat
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/manual_menu.json"
BASELINES = ROOT / "tests/fixtures/menu_baselines"


def check_image_baseline(actual, audience, output_dir):
    """Keep current output even on failure; updates require an explicit opt-in."""
    output = output_dir / f"{audience}.webp"
    output.write_bytes(actual.read_bytes())
    baseline = BASELINES / output.name
    if os.environ.get("UPDATE_MENU_BASELINES") == "1":
        BASELINES.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(output.read_bytes())
    assert baseline.exists(), f"Missing baseline: {baseline}"
    with Image.open(output) as image, Image.open(baseline) as expected:
        image, expected = image.convert("RGB"), expected.convert("RGB")
        assert image.size == expected.size, f"Image dimensions changed: {output}"
        diff = ImageChops.difference(image, expected)
        rms = max(ImageStat.Stat(diff).rms)
        # Allow tiny rasterizer/codec rounding differences between platforms.
        if rms > 1:
            diff.save(output_dir / f"{audience}-diff.png")
        assert rms <= 1, f"Visual baseline changed (RMS={rms:.3f}): {output}"


class Event:
    def __init__(self, admin=False):
        self.admin = admin

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


def build_bot(directory):
    bot = object.__new__(HelpTypst)
    bot.menu_store = MenuStore(directory)
    bot._menu_lock = asyncio.Lock()
    bot.context = SimpleNamespace(get_config=lambda: {"wake_prefix": ["/"]})
    bot.plugin_config = TypstPluginConfig.load({})
    bot.template_path = ROOT / "templates/base.typ"
    bot.font_dirs = [ROOT / "resources/fonts"]
    bot.font_manager = FontManager(bot.font_dirs)
    bot.font_manager.scan_fonts()
    bot.layout = TypstLayout(bot.plugin_config)
    bot.renderer = TypstRenderer(
        directory,
        bot.template_path,
        bot.font_dirs,
        bot.plugin_config,
        ignore_system_fonts=True,
    )
    return bot


def test_real_fixture_build_publishes_both_images_and_routes_help(
    tmp_path, monkeypatch
):
    from astrbot_plugin_custom_help.core import renderer

    monkeypatch.setattr(
        renderer, "time", SimpleNamespace(strftime=lambda _: "2026-10-14 12:00:00")
    )
    bot = build_bot(tmp_path)
    bot.menu_store.source.write_bytes(FIXTURE.read_bytes())
    original = FIXTURE.read_bytes()

    async def run():
        try:
            messages = [result async for result in bot.build_menu(Event(admin=True))]
            assert "菜单已生成" in messages[0], messages
            assert "4 个可见分类" in messages[0]
            assert "5 个分类" in messages[0]
            public_meta, public_images, public_source = bot.menu_store.read_published()
            admin_meta, admin_images, admin_source = bot.menu_store.read_published(
                admin=True
            )
            assert public_meta == admin_meta
            assert public_source == admin_source == MenuDocument.read(FIXTURE)
            assert public_meta["prefixes"] == ["/"]
            assert set(public_images).isdisjoint(admin_images)
            assert len(public_images) == len(admin_images) == 1
            # Preserve both audiences even if the first comparison fails.
            (tmp_path / "public.webp").write_bytes(public_images[0].read_bytes())
            (tmp_path / "admin.webp").write_bytes(admin_images[0].read_bytes())
            check_image_baseline(public_images[0], "public", tmp_path)
            check_image_baseline(admin_images[0], "admin", tmp_path)
            # Both lists must decode fully, rather than merely existing on disk.
            for path in [*public_images, *admin_images]:
                with Image.open(path) as image:
                    image.load()
                    assert image.format == "WEBP"
                    assert 100 < image.width <= 16383
                    assert 0 < image.height <= 16383
            for path in [public_images[0].parent, admin_images[0].parent]:
                assert all(item.suffix == ".webp" for item in path.iterdir())
            assert not list(bot.menu_store.root.rglob(".render_*"))
            for admin, images in [(False, public_images), (True, admin_images)]:
                results = [result async for result in bot.show_menu(Event(admin))]
                assert len(results) == 1 and len(results[0]) == len(images)
                assert all(
                    Path(component.path).resolve() == path.resolve()
                    for path, component in zip(images, results[0])
                )
            public_search = [
                result async for result in bot.show_menu(Event(), "pixiv_novel")
            ]
            admin_search = [
                result async for result in bot.show_menu(Event(True), "pixiv_novel")
            ]
            assert public_search == ["已发布菜单中没有匹配的可见功能。"]
            assert (
                admin_search
                and "pixiv_novel" in admin_search[0]
                and "（隐藏）" in admin_search[0]
            )
            assert (
                bot.menu_store.source.read_bytes() == original == FIXTURE.read_bytes()
            )
        finally:
            await bot.renderer.close()

    asyncio.run(run())


@pytest.mark.parametrize("admin", [False, True])
def test_real_fixture_rendered_content_color_and_column_positions(tmp_path, admin):
    document = MenuDocument.read(FIXTURE)
    payload = tmp_path / "layout.json"
    TypstLayout(TypstPluginConfig.load({})).dump_layout_json(
        document.visible_plugins(include_hidden=admin),
        payload,
        document.title,
        "command",
        ["/"],
        ["Sarasa Gothic SC"],
        admin_view=admin,
    )
    template = tmp_path / "base.typ"
    template.write_bytes((ROOT / "templates/base.typ").read_bytes())
    pdf = typst.compile(
        str(template),
        format="pdf",
        font_paths=[str(ROOT / "resources/fonts")],
        ignore_system_fonts=True,
        sys_inputs={"json_path": payload.name, "timestamp": "fixture-test"},
    )
    colors, positions, stack = {}, {}, []
    fill = None
    spans = []
    previous_key = None
    targets = {"搜图", "dashboard_update", "helps"}
    targets.update(node.name for plugin in document.plugins for node in plugin.nodes)

    def operand(op, args, cm, tm):
        nonlocal fill
        if op == b"q":
            stack.append(fill)
        elif op == b"Q":
            fill = stack.pop()
        elif op in {b"rg", b"sc", b"scn"} and len(args) == 3:
            fill = tuple(float(value) for value in args)

    def visit(text, cm, tm, font, size):
        nonlocal previous_key
        name = text.replace("\u200b", "").strip()
        if not name:
            return
        position = (
            tm[4] * cm[0] + tm[5] * cm[2] + cm[4],
            tm[4] * cm[1] + tm[5] * cm[3] + cm[5],
        )
        # Typst splits names at zero-width breaks; pypdf reports these chunks
        # with the same text origin. Reassemble before matching full names.
        key = (tuple(cm), size, fill)
        if spans and key == previous_key:
            spans[-1] = (spans[-1][0] + name, spans[-1][1], fill)
        else:
            spans.append((name, position, fill))
        previous_key = key

    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    text = (
        reader.pages[0]
        .extract_text(visitor_text=visit, visitor_operand_before=operand)
        .replace("\u200b", "")
    )
    gray = pytest.approx((166 / 255, 174 / 255, 181 / 255), abs=0.001)
    for span, position, color in spans:
        for target in targets:
            if target not in positions and re.search(
                r"(?<![A-Za-z0-9_])" + re.escape(target) + r"(?![A-Za-z0-9_])", span
            ):
                colors[target] = color
                positions[target] = position
    assert "搜图" in text and "dashboard_update" in text and "helps" in text
    assert (
        positions["搜图"][0] < positions["dashboard_update"][0] < positions["helps"][0]
    )
    assert colors["helps"] != gray
    if admin:
        pixiv = next(
            plugin
            for plugin in document.plugins
            if plugin.name == "astrbot_plugin_pixiv_reborn"
        )
        for node in pixiv.nodes:
            if node.available and not node.is_group:
                assert node.name in text
                assert colors[node.name] == gray
    else:
        assert "pixiv_" not in text
        assert "pixiv" not in colors
