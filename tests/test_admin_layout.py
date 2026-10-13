import io
from pathlib import Path

import pytest
import typst
from astrbot_plugin_custom_help.core.menu import MenuDocument, MenuNode, MenuPlugin
from astrbot_plugin_custom_help.domain import TypstPluginConfig
from astrbot_plugin_custom_help.utils import TypstLayout
from pypdf import PdfReader


def test_admin_pdf_marks_hidden_text_and_keeps_visible_text_distinct(tmp_path):
    document = MenuDocument(
        plugins=[
            MenuPlugin(
                id="plugin",
                name="Plugin",
                nodes=[
                    MenuNode(
                        id="public", name="PublicCommand", desc="PublicDescription"
                    ),
                    MenuNode(
                        id="secret",
                        name="HiddenCommand",
                        desc="HiddenDescription",
                        visible=False,
                    ),
                    MenuNode(
                        id="group",
                        name="HiddenGroup",
                        visible=False,
                        is_group=True,
                        children=[MenuNode(id="child", name="HiddenChild")],
                    ),
                ],
            ),
            MenuPlugin(
                id="hidden-plugin",
                name="HiddenPlugin",
                visible=False,
                nodes=[MenuNode(id="inherited", name="InheritedCommand")],
            ),
        ]
    )
    payload = tmp_path / "layout.json"
    TypstLayout(TypstPluginConfig.load({})).dump_layout_json(
        document.visible_plugins(include_hidden=True),
        payload,
        "Admin",
        "command",
        ["/"],
        ["Sarasa Gothic SC"],
        admin_view=True,
    )
    root = Path(__file__).resolve().parents[1]
    template = tmp_path / "base.typ"
    template.write_text(
        (root / "templates/base.typ").read_text(encoding="utf-8"), encoding="utf-8"
    )
    pdf = typst.compile(
        str(template),
        format="pdf",
        font_paths=[str(root / "resources/fonts")],
        sys_inputs={"json_path": payload.name, "timestamp": "test"},
    )
    colors = {}
    fill = None
    stack = []

    def operand(op, args, cm, tm):
        nonlocal fill
        if op == b"q":
            stack.append(fill)
        elif op == b"Q":
            fill = stack.pop()
        elif op in {b"rg", b"sc", b"scn"} and len(args) == 3:
            fill = tuple(float(value) for value in args)

    def visit(text, cm, tm, font, size):
        name = text.replace("\u200b", "").strip()
        if name:
            colors[name] = fill

    PdfReader(io.BytesIO(pdf)).pages[0].extract_text(
        visitor_text=visit, visitor_operand_before=operand
    )
    gray = (166 / 255, 174 / 255, 181 / 255)
    for name in [
        "HiddenCommand",
        "HiddenDescription",
        "HiddenGroup",
        "HiddenChild",
        "HiddenPlugin",
        "InheritedCommand",
    ]:
        assert colors[name] == pytest.approx(gray, abs=0.001)
    assert colors["PublicCommand"] != pytest.approx(gray, abs=0.001)
