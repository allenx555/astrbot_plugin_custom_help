import io
from pathlib import Path

import typst
from astrbot_plugin_custom_help.core.menu import MenuDocument, MenuNode, MenuPlugin
from astrbot_plugin_custom_help.domain import TypstPluginConfig
from astrbot_plugin_custom_help.utils import TypstLayout
from pypdf import PdfReader


def test_rendered_command_order_is_json_order(tmp_path):
    document = MenuDocument(
        plugins=[
            MenuPlugin(
                id="z",
                name="CardZ",
                nodes=[
                    MenuNode(id="z1", name="PlainFirst"),
                    MenuNode(id="hidden", name="Hidden", visible=False),
                    MenuNode(id="z2", name="RichSecond", desc="description"),
                    MenuNode(
                        id="group",
                        name="GroupThird",
                        is_group=True,
                        children=[
                            MenuNode(id="child1", name="ChildPlain"),
                            MenuNode(id="child2", name="ChildRich", desc="description"),
                        ],
                    ),
                    MenuNode(id="z3", name="PlainLast"),
                ],
            ),
            MenuPlugin(
                id="a",
                name="CardA",
                nodes=[
                    MenuNode(id=f"a{i}", name=f"Command{i}", desc="description")
                    for i in range(8)
                ],
            ),
            MenuPlugin(id="m", name="CardM", nodes=[MenuNode(id="m1", name="OnlyOne")]),
            MenuPlugin(
                id="b",
                name="CardB",
                nodes=[MenuNode(id="b1", name="ToolTag", tag="tool")],
            ),
        ]
    )
    layout = TypstLayout(
        TypstPluginConfig.load({"rendering": {"column_height_pt": 100}})
    )
    payload_path = tmp_path / "layout.json"
    layout.dump_layout_json(
        document.visible_plugins(),
        payload_path,
        "Order test",
        "command",
        ["/"],
        ["Sarasa Gothic SC"],
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
        sys_inputs={"json_path": payload_path.name, "timestamp": "test"},
    )
    positions = {}

    def visit(text, cm, tm, font, size):
        cleaned = text.replace("\u200b", "").strip()
        if cleaned:
            positions[cleaned] = (
                tm[4] * cm[0] + tm[5] * cm[2] + cm[4],
                tm[4] * cm[1] + tm[5] * cm[3] + cm[5],
            )

    reader = PdfReader(io.BytesIO(pdf))
    text = reader.pages[0].extract_text(visitor_text=visit).replace("\u200b", "")
    names = ["CardZ", "CardA", "CardM", "CardB"]
    assert all(name in text for name in names)
    assert positions["CardZ"][0] < positions["CardA"][0] < positions["CardM"][0]
    assert abs(positions["CardZ"][1] - positions["CardA"][1]) < 1
    assert positions["CardB"][1] < positions["CardZ"][1]
    nodes = [
        "PlainFirst",
        "RichSecond",
        "GroupThird",
        "ChildPlain",
        "ChildRich",
        "PlainLast",
    ]
    assert [text.index(name) for name in nodes] == sorted(
        text.index(name) for name in nodes
    )
    assert "Hidden" not in text


def test_event_layout_keeps_existing_tool_section():
    from astrbot_plugin_custom_help.domain import PluginMetadata, RenderNode

    layout = TypstLayout(TypstPluginConfig.load({}))
    payload = layout._generate_balanced_payload(
        [PluginMetadata(name="tools", nodes=[RenderNode(name="tool", tag="tool")])],
        "Events",
        "event",
        ["/"],
        [],
    )
    assert payload["singles"][0]["name"] == "tools"


def test_whole_cards_fill_columns_without_splitting(tmp_path):
    names = [
        "SmallA",
        "SmallB",
        "TallC",
        "SmallD",
        "SmallE",
        "TallF",
        "SmallG",
        "SmallH",
    ]
    document = MenuDocument(
        plugins=[
            MenuPlugin(
                id=name,
                name=name,
                nodes=[
                    MenuNode(id=f"{name}{i}", name=f"{name}Item{i:02d}")
                    for i in range(18 if name.startswith("Tall") else 1)
                ],
            )
            for name in names
        ]
    )
    layout = TypstLayout(
        TypstPluginConfig.load({"rendering": {"column_height_pt": 240}})
    )
    payload = tmp_path / "layout.json"
    layout.dump_layout_json(
        document.visible_plugins(),
        payload,
        "Whole cards",
        "command",
        ["/"],
        ["Sarasa Gothic SC"],
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
    positions = {}

    def visit(text, cm, tm, font, size):
        text = text.replace("\u200b", "").strip()
        if text:
            positions[text] = (
                tm[4] * cm[0] + tm[5] * cm[2] + cm[4],
                tm[4] * cm[1] + tm[5] * cm[3] + cm[5],
            )

    text = (
        PdfReader(io.BytesIO(pdf))
        .pages[0]
        .extract_text(visitor_text=visit)
        .replace("\u200b", "")
    )
    assert all(name in text for name in names)
    assert positions["SmallA"][0] < positions["SmallB"][0] < positions["TallC"][0]
    assert abs(positions["SmallA"][1] - positions["SmallB"][1]) < 1
    assert abs(positions["SmallA"][1] - positions["TallC"][1]) < 1
    assert abs(positions["SmallD"][0] - positions["SmallA"][0]) < 1
    assert abs(positions["SmallE"][0] - positions["SmallB"][0]) < 1
    assert positions["SmallD"][1] < positions["SmallA"][1]
    assert positions["SmallE"][1] < positions["SmallB"][1]
    assert positions["SmallD"][1] > positions["TallCItem17"][1]
    assert positions["SmallE"][1] > positions["TallCItem17"][1]
    assert abs(positions["TallF"][0] - positions["SmallA"][0]) < 1
    assert positions["TallF"][1] < positions["SmallD"][1]
    for name in ["TallC", "TallF"]:
        for i in range(18):
            x, _ = positions[f"{name}Item{i:02d}"]
            assert abs(x - positions[f"{name}Item00"][0]) < 1
        assert text.count(name + "Item") == 18
