import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_custom_help.core import analyzer
from astrbot_plugin_custom_help.core.menu import (
    MenuDocument,
    MenuNode,
    MenuPlugin,
    MenuStore,
    atomic_json,
    export_document,
    search_published,
)
from astrbot_plugin_custom_help.domain import PluginMetadata, RenderNode
from astrbot_plugin_custom_help.main import HelpTypst
from PIL import Image
from pydantic import ValidationError


def document():
    return MenuDocument(
        plugins=[
            MenuPlugin(
                id="images",
                name="images",
                nodes=[
                    MenuNode(id="search", name="搜图", desc="查找图片"),
                    MenuNode(
                        id="secret", name="隐藏命令", desc="secret", visible=False
                    ),
                    MenuNode(
                        id="group",
                        name="admin",
                        is_group=True,
                        visible=False,
                        children=[MenuNode(id="child", name="danger", desc="secret")],
                    ),
                ],
            )
        ]
    )


def published(tmp_path, doc=None):
    store = MenuStore(tmp_path)
    doc = doc or document()
    generation = store.new_generation()
    image = generation / "menu.webp"
    Image.new("RGB", (30, 30)).save(image)
    store.publish(generation, [str(image)], doc, ["/"])
    return store, image


def test_visibility_applies_to_parent_and_search():
    doc = document()
    assert [n.name for n in doc.visible_plugins()[0].nodes] == ["搜图"]
    assert search_published(doc, "secret", ["/"]) == []
    assert "搜图" in search_published(doc, "images", ["/"])[0]


def test_export_includes_hidden_admin_and_system_and_preserves_edits():
    plugins = [
        PluginMetadata(name="hidden", nodes=[RenderNode(name="x")]),
        PluginMetadata(name="system", reserved=True, nodes=[RenderNode(name="help")]),
        PluginMetadata(
            name="normal",
            nodes=[
                RenderNode(
                    name="admin",
                    is_group=True,
                    tag="admin",
                    children=[RenderNode(name="child")],
                ),
                RenderNode(name="public"),
            ],
        ),
    ]
    first = export_document(plugins, {"hidden"}, [{"kind": "tool"}])
    assert len(first.plugins) == 3
    assert not first.plugins[0].visible
    assert not first.plugins[1].visible
    assert not first.plugins[2].nodes[0].children[0].visible
    assert first.plugins[2].nodes[0].children[0].tag == "admin"
    first.title = "自定义菜单"
    first.plugins[2].nodes[1].desc = "自己写的说明"
    first.plugins[2].nodes.reverse()
    plugins[2].nodes.append(RenderNode(name="new"))
    second = export_document(plugins, set(), [], first)
    assert second.title == "自定义菜单"
    assert [n.name for n in second.plugins[2].nodes] == ["public", "admin", "new"]
    assert second.plugins[2].nodes[0].desc == "自己写的说明"
    assert not second.plugins[2].nodes[-1].visible
    assert not second.plugins[0].visible  # Removing blacklist does not override edits.


def test_removed_commands_remain_in_source_but_unavailable():
    old = export_document(
        [PluginMetadata(name="p", nodes=[RenderNode(name="a")])], set(), []
    )
    new = export_document(
        [PluginMetadata(name="p", nodes=[RenderNode(name="b")])], set(), [], old
    )
    assert not new.plugins[0].nodes[0].available
    assert new.visible_plugins() == []


@pytest.mark.parametrize("bad", ["false", 0, None])
def test_invalid_visibility_does_not_silently_publish(tmp_path, bad):
    payload = document().model_dump()
    payload["plugins"][0]["visible"] = bad
    atomic_json(tmp_path / "menu.json", payload)
    with pytest.raises(ValidationError):
        MenuDocument.read(tmp_path / "menu.json")


def test_duplicate_ids_are_rejected(tmp_path):
    doc = document()
    doc.plugins[0].nodes[1].id = "search"
    atomic_json(tmp_path / "menu.json", doc.model_dump())
    with pytest.raises(ValueError, match="重复"):
        MenuDocument.read(tmp_path / "menu.json")


def test_publish_failure_keeps_old_generation_and_restart_reads_it(
    tmp_path, monkeypatch
):
    store, image = published(tmp_path)
    before = store.manifest.read_bytes()
    with pytest.raises(ValueError):
        store.publish(
            store.new_generation(), [str(tmp_path / "missing.webp")], document(), ["/"]
        )
    assert store.manifest.read_bytes() == before
    restarted = MenuStore(tmp_path)
    assert restarted.read_published()[1] == [image.resolve()]
    # A failed manifest switch also preserves the old publication.
    import astrbot_plugin_custom_help.core.menu as menu

    original_replace = menu.os.replace

    def fail_manifest(src, dst):
        if dst == store.manifest:
            raise OSError("disk error")
        original_replace(src, dst)

    monkeypatch.setattr(menu.os, "replace", fail_manifest)
    gen = store.new_generation()
    new_image = gen / "new.webp"
    Image.new("RGB", (20, 20)).save(new_image)
    with pytest.raises(OSError):
        store.publish(gen, [str(new_image)], document(), ["/"])
    assert store.manifest.read_bytes() == before


def test_export_backup_and_editing_source_does_not_change_published_search(tmp_path):
    store, _ = published(tmp_path)
    doc = document()
    store.save_source(doc)
    doc.plugins[0].nodes[1].visible = True
    store.save_source(doc)
    backup = MenuDocument.read(store.source.with_suffix(".json.bak"))
    assert not backup.plugins[0].nodes[1].visible
    _, _, committed = store.read_published()
    assert search_published(committed, "secret", ["/"]) == []


def test_reusing_generation_cannot_mutate_old_snapshot_on_manifest_failure(
    tmp_path, monkeypatch
):
    store, image = published(tmp_path)
    import astrbot_plugin_custom_help.core.menu as module

    replace = module.os.replace

    def fail_manifest(source, destination):
        if destination == store.manifest:
            raise OSError("disk error")
        replace(source, destination)

    monkeypatch.setattr(module.os, "replace", fail_manifest)
    edited = document()
    edited.plugins[0].nodes[1].visible = True
    with pytest.raises(OSError):
        store.publish(image.parent, [str(image)], edited, ["/"])
    assert not search_published(store.read_published()[2], "secret", ["/"])
    store.discard_generation(image.parent)
    assert image.exists()


def test_generation_cleanup_preserves_current_recent_and_unrelated_files(tmp_path):
    store, image = published(tmp_path)
    import os
    import time

    old = [store.new_generation() for _ in range(5)]
    for folder in old:
        os.utime(folder, (time.time() - 10 * 86400,) * 2)
    unrelated = store.root / "notes"
    unrelated.mkdir()
    store.collect_garbage()
    assert image.exists() and unrelated.exists()
    assert len([p for p in old if p.exists()]) == 2
    with pytest.raises(ValueError):
        store.discard_generation(tmp_path)


def test_cancel_during_publication_does_not_delete_committed_images(
    tmp_path, monkeypatch
):
    import threading

    from astrbot_plugin_custom_help.core.renderer import RenderResult
    from astrbot_plugin_custom_help.domain import TypstPluginConfig
    from astrbot_plugin_custom_help.utils import TypstLayout

    store, _ = published(tmp_path)
    store.save_source(document())
    bot = object.__new__(HelpTypst)
    bot.menu_store = store
    bot._menu_lock = asyncio.Lock()
    bot.context = SimpleNamespace(get_config=lambda: {"wake_prefix": ["/"]})
    bot.plugin_config = TypstPluginConfig.load({})
    bot.font_manager = SimpleNamespace(get_render_font_list=lambda _: [])
    bot.layout = TypstLayout(bot.plugin_config)

    async def render(pipeline, mode, *, output_dir):
        output_dir.mkdir(parents=True)
        image = output_dir / "menu.webp"
        Image.new("RGB", (20, 20)).save(image)
        return RenderResult([str(image)], []), ""

    bot.renderer = SimpleNamespace(render=render)
    started, finish = threading.Event(), threading.Event()
    publish = store.publish

    def delayed_publish(*args):
        started.set()
        assert finish.wait(5)
        return publish(*args)

    monkeypatch.setattr(store, "publish", delayed_publish)

    async def consume():
        return [item async for item in bot.build_menu(FakeEvent())]

    async def run():
        job = asyncio.create_task(consume())
        assert await asyncio.to_thread(started.wait, 5)
        job.cancel()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert all(path.exists() for path in store.read_published()[1])
        assert all(path.exists() for path in store.read_published(admin=True)[1])

    asyncio.run(run())


class FakeEvent:
    def is_admin(self):
        return False

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


def test_helps_sends_published_images_without_analyzer_or_renderer(tmp_path):
    store, image = published(tmp_path)
    bot = object.__new__(HelpTypst)
    bot.menu_store = store

    # No analyzer, renderer, fonts or context on the instance.
    async def run():
        results = [r async for r in bot.show_menu(FakeEvent())]
        assert len(results[0]) == 1
        assert image.name in str(results[0][0])
        results = [r async for r in bot.show_menu(FakeEvent(), "secret")]
        assert results == ["已发布菜单中没有匹配的可见功能。"]

    asyncio.run(run())


def test_missing_publication_never_renders(tmp_path):
    bot = object.__new__(HelpTypst)
    bot.menu_store = MenuStore(tmp_path)

    async def run():
        results = [r async for r in bot.show_menu(FakeEvent())]
        assert "尚未生成" in results[0]

    asyncio.run(run())


def handler(module, name, filters):
    async def callback(self, event):
        pass

    return analyzer.StarHandlerMetadata(
        event_type=analyzer.EventType.AdapterMessageEvent,
        handler_full_name=module + "_" + name,
        handler_name=name,
        handler_module_path=module,
        handler=callback,
        event_filters=filters,
        desc=name,
    )


def test_registry_export_includes_blacklist_inactive_orphan_and_admin(monkeypatch):
    normal = handler(
        "plugins.hidden.main", "ordinary", [analyzer.CommandFilter("ordinary")]
    )
    admin = handler(
        "plugins.hidden.main",
        "admin",
        [
            analyzer.CommandFilter("admin"),
            analyzer.PermissionTypeFilter(analyzer.PermissionType.ADMIN),
        ],
    )
    member = handler(
        "plugins.hidden.main",
        "member",
        [
            analyzer.CommandFilter("member"),
            analyzer.PermissionTypeFilter(analyzer.PermissionType.MEMBER),
        ],
    )
    disabled = handler(
        "plugins.disabled.main", "disabled", [analyzer.CommandFilter("disabled")]
    )
    orphan = handler("astrbot.core.system", "help", [analyzer.CommandFilter("help")])
    stars = [
        SimpleNamespace(
            name="hidden", module_path="plugins.hidden.main", activated=True
        ),
        SimpleNamespace(
            name="disabled", module_path="plugins.disabled.main", activated=False
        ),
    ]
    monkeypatch.setattr(
        analyzer, "star_handlers_registry", [normal, admin, member, disabled, orphan]
    )
    context = SimpleNamespace(get_all_stars=lambda: stars)
    cfg = SimpleNamespace(ignored_plugins={"hidden", "disabled"})
    exported = analyzer.CommandAnalyzer(context, cfg).export_all()
    by_name = {p.name: p for p in exported}
    assert set(by_name) == {"hidden", "disabled", "astrbot.core.system"}
    assert not by_name["disabled"].activated
    assert by_name["astrbot.core.system"].reserved
    assert {n.name: n.tag for n in by_name["hidden"].nodes} == {
        "admin": "admin",
        "ordinary": "normal",
        "member": "normal",
    }


def test_nested_same_named_groups_are_not_dropped_and_inherit_admin(monkeypatch):
    root = analyzer.CommandGroupFilter("manage")
    root.add_custom_filter(analyzer.PermissionTypeFilter(analyzer.PermissionType.ADMIN))
    sub = analyzer.CommandGroupFilter("manage", parent_group=root)
    leaf_h = handler("p.main", "leaf", [])
    leaf = analyzer.CommandFilter("run", handler_md=leaf_h)
    root.add_sub_command_filter(sub)
    sub.add_sub_command_filter(leaf)
    root_h = handler("p.main", "root", [root])
    sub_h = handler("p.main", "sub", [sub])
    monkeypatch.setattr(analyzer, "star_handlers_registry", [root_h, sub_h, leaf_h])
    context = SimpleNamespace(
        get_all_stars=lambda: [
            SimpleNamespace(name="p", module_path="p.main", activated=True)
        ]
    )
    export = analyzer.CommandAnalyzer(
        context, SimpleNamespace(ignored_plugins=set())
    ).export_all()
    doc = export_document(export, set(), [])
    assert len(doc.plugins[0].nodes) == 1
    child = doc.plugins[0].nodes[0].children[0].children[0]
    assert child.name == "run" and child.tag == "admin" and not child.visible


def test_custom_nodes_survive_export():
    old = document()
    old.plugins[0].nodes.append(MenuNode(id="custom:extra", name="extra"))
    new = export_document(
        [PluginMetadata(name="images", nodes=[RenderNode(name="搜图")])], set(), [], old
    )
    assert new.plugins[0].nodes[-1].available


def test_management_handlers_have_admin_filters():
    # Inspect real decorators, including parent-group and leaf permissions.
    from astrbot.core.star.star_handler import star_handlers_registry

    for name in [
        "export_menu",
        "build_menu",
        "menu_status",
        "show_events",
        "show_filters",
    ]:
        h = next(
            h
            for h in star_handlers_registry
            if h.handler_module_path == HelpTypst.__module__ and h.handler_name == name
        )
        assert any(
            isinstance(f, analyzer.PermissionTypeFilter)
            and f.permission_type == analyzer.PermissionType.ADMIN
            for f in h.event_filters
        )


def test_inventory_includes_disabled_regex_handlers_and_tools(monkeypatch):
    h = handler("p.main", "hidden_regex", [analyzer.RegexFilter("^hidden$")])
    h.enabled = False
    monkeypatch.setattr(analyzer, "star_handlers_registry", [h])
    context = SimpleNamespace(
        get_llm_tool_manager=lambda: SimpleNamespace(
            func_list=[
                SimpleNamespace(
                    name="disabled_tool",
                    active=False,
                    description="tool",
                    handler_module_path="p.main",
                )
            ]
        )
    )
    items = analyzer.CommandAnalyzer(context, SimpleNamespace()).export_inventory()
    assert len(items) == 2
    assert items[0]["filters"][0]["type"] == "RegexFilter"
    assert "hidden" in items[0]["filters"][0]["pattern"]
    assert not items[0]["enabled"] and not items[1]["enabled"]


@pytest.mark.parametrize("hide_all", [False, True])
def test_build_command_with_real_typst_and_worker_process(tmp_path, hide_all):
    from pathlib import Path

    from astrbot_plugin_custom_help.domain import TypstPluginConfig
    from astrbot_plugin_custom_help.utils import FontManager, TypstLayout

    root = Path(__file__).resolve().parents[1]
    bot = object.__new__(HelpTypst)
    bot.menu_store = MenuStore(tmp_path)
    bot._menu_lock = asyncio.Lock()
    bot.plugin_config = TypstPluginConfig.load({})
    bot.template_path = root / "templates/base.typ"
    bot.font_dirs = [root / "resources/fonts"]
    bot.font_manager = FontManager(bot.font_dirs)
    bot.font_manager.scan_fonts()
    bot.layout = TypstLayout(bot.plugin_config)
    from astrbot_plugin_custom_help.core.renderer import TypstRenderer

    bot.renderer = TypstRenderer(
        tmp_path, bot.template_path, bot.font_dirs, bot.plugin_config
    )
    bot.context = SimpleNamespace(get_config=lambda: {"wake_prefix": ["/"]})
    kv = {}

    async def get_kv(key, default=None):
        return kv.get(key, default)

    async def put_kv(key, value):
        kv[key] = value

    async def delete_kv(key):
        kv.pop(key, None)

    bot.get_kv_data, bot.put_kv_data, bot.delete_kv_data = get_kv, put_kv, delete_kv
    source = document()
    source.plugins[0].visible = not hide_all
    bot.menu_store.save_source(source)

    async def run():
        messages = [r async for r in bot.build_menu(FakeEvent())]
        assert "菜单已生成" in messages[0], messages
        payload, images, committed = bot.menu_store.read_published()
        assert payload["prefixes"] == ["/"]
        assert images and committed.title == document().title
        assert all(Image.open(p).width > 100 for p in images)
        _, admin_images, _ = bot.menu_store.read_published(admin=True)
        assert admin_images and set(admin_images).isdisjoint(images)
        assert all(Image.open(p).width > 100 for p in admin_images)
        assert not list(admin_images[0].parent.glob("*.json"))
        assert not list(admin_images[0].parent.glob("*.png"))
        assert list((root / "templates").glob(".help_typst_*.json")) == []
        # Changing the editable source only takes effect on the next successful build.
        bot.menu_store.source.write_text("broken JSON", encoding="utf-8")
        manifest = bot.menu_store.manifest.read_bytes()
        messages = [r async for r in bot.build_menu(FakeEvent())]
        assert "生成失败" in messages[0]
        assert bot.menu_store.manifest.read_bytes() == manifest

    asyncio.run(run())


def test_admin_render_failure_keeps_both_old_variants(tmp_path, monkeypatch):
    store, image = published(tmp_path)
    store.save_source(document())
    store.publish(image.parent, [str(image)], document(), ["/"], [str(image)])
    before = store.manifest.read_bytes()
    bot = object.__new__(HelpTypst)
    bot.menu_store = store
    bot._menu_lock = asyncio.Lock()
    bot.context = SimpleNamespace(get_config=lambda: {"wake_prefix": ["/"]})
    from astrbot_plugin_custom_help.core.renderer import RenderResult, TypstRenderer
    from astrbot_plugin_custom_help.domain import TypstPluginConfig
    from astrbot_plugin_custom_help.utils import TypstLayout

    bot.plugin_config = TypstPluginConfig.load({})
    bot.template_path = tmp_path / "template.typ"
    bot.font_dirs = []
    bot.font_manager = SimpleNamespace(get_render_font_list=lambda _: [])
    bot.layout = TypstLayout(bot.plugin_config)
    bot.renderer = TypstRenderer(
        tmp_path, bot.template_path, bot.font_dirs, bot.plugin_config
    )

    async def fail_admin(renderer, pipeline, mode, *, output_dir):
        if output_dir.name == "admin":
            return None, "test admin compile failure"
        output_dir.mkdir(parents=True)
        image = output_dir / "public.webp"
        Image.new("RGB", (30, 30)).save(image)
        return RenderResult([str(image)], []), ""

    monkeypatch.setattr(TypstRenderer, "render", fail_admin)

    async def run():
        results = [r async for r in bot.build_menu(FakeEvent())]
        assert "test admin compile failure" in results[0]
        assert store.manifest.read_bytes() == before
        assert store.read_published()[1] == [image.resolve()]
        assert store.read_published(admin=True)[1] == [image.resolve()]

    asyncio.run(run())


def test_admin_projection_marks_hidden_ancestors_and_excludes_unavailable():
    doc = document()
    doc.plugins.append(
        MenuPlugin(
            id="hidden-plugin",
            name="HiddenPlugin",
            visible=False,
            nodes=[MenuNode(id="inherited", name="Inherited")],
        )
    )
    doc.plugins[0].nodes.append(
        MenuNode(id="inactive", name="Inactive", available=False)
    )
    public = doc.visible_plugins()
    full = doc.visible_plugins(include_hidden=True)
    assert len(public) == 1 and len(public[0].nodes) == 1
    assert [node.hidden for node in full[0].nodes] == [False, True, True]
    assert full[0].nodes[2].children[0].hidden
    assert full[1].hidden and full[1].nodes[0].hidden
    assert "隐藏" in search_published(doc, "secret", ["/"], include_hidden=True)[0]
    assert not search_published(doc, "secret", ["/"])
    assert not search_published(doc, "Inactive", ["/"], include_hidden=True)


def test_dual_publication_routes_by_admin_and_preserves_old_on_failure(tmp_path):
    store, public_image = published(tmp_path)
    generation = public_image.parent
    admin_image = generation / "admin.webp"
    Image.new("RGB", (30, 30)).save(admin_image)
    store.publish(
        generation, [str(public_image)], document(), ["/"], [str(admin_image)]
    )
    before = store.manifest.read_bytes()
    with pytest.raises(ValueError):
        store.publish(
            store.new_generation(),
            [str(public_image)],
            document(),
            ["/"],
            [str(tmp_path / "missing.webp")],
        )
    assert store.manifest.read_bytes() == before
    bot = object.__new__(HelpTypst)
    bot.menu_store = store

    class AdminEvent(FakeEvent):
        def is_admin(self):
            return True

    async def run():
        normal = [r async for r in bot.show_menu(FakeEvent())]
        admin = [r async for r in bot.show_menu(AdminEvent())]
        assert public_image.name in str(normal[0][0])
        assert admin_image.name in str(admin[0][0])
        assert public_image.name not in str(admin[0][0])
        assert [r async for r in bot.show_menu(FakeEvent(), "secret")] == [
            "已发布菜单中没有匹配的可见功能。"
        ]
        assert "（隐藏）" in [r async for r in bot.show_menu(AdminEvent(), "secret")][0]
        # A broken administrator image must never affect public requests.
        admin_image.write_bytes(b"broken")
        assert public_image.name in str(
            [r async for r in bot.show_menu(FakeEvent())][0][0]
        )
        assert "尚未生成" in [r async for r in bot.show_menu(AdminEvent())][0]

    asyncio.run(run())


def test_legacy_publication_admin_falls_back_without_hidden_search(tmp_path):
    store, image = published(tmp_path)
    bot = object.__new__(HelpTypst)
    bot.menu_store = store

    class AdminEvent(FakeEvent):
        def is_admin(self):
            return True

    async def run():
        results = [r async for r in bot.show_menu(AdminEvent())]
        assert "完整版尚未生成" in results[0]
        assert image.name in str(results[1][0])
        results = [r async for r in bot.show_menu(AdminEvent(), "secret")]
        assert results[-1] == "已发布菜单中没有匹配的可见功能。"

    asyncio.run(run())


@pytest.mark.parametrize("is_admin", [False, True])
def test_public_preview_uses_public_images_and_search_even_if_admin_image_broken(
    tmp_path, is_admin
):
    store, public_image = published(tmp_path)
    admin_image = public_image.parent / "admin.webp"
    Image.new("RGB", (30, 30)).save(admin_image)
    store.publish(
        public_image.parent, [str(public_image)], document(), ["/"], [str(admin_image)]
    )
    admin_image.write_bytes(b"broken")
    bot = object.__new__(HelpTypst)
    bot.menu_store = store
    event = FakeEvent()
    event.is_admin = lambda: is_admin

    async def run():
        results = [r async for r in bot.show_menu(event, "--public")]
        assert len(results) == 1
        assert results[0][0].path == str(public_image.resolve())
        assert [r async for r in bot.show_menu(event, "--public", "secret")] == [
            "已发布菜单中没有匹配的可见功能。"
        ]
        visible = [r async for r in bot.show_menu(event, "--public", "搜图")]
        assert "搜图" in visible[0] and "（隐藏）" not in visible[0]
        # Public preview must not depend on the presence of an admin variant.
        store.publish(public_image.parent, [str(public_image)], document(), ["/"])
        legacy = [r async for r in bot.show_menu(event, "--public")]
        assert len(legacy) == 1 and legacy[0][0].path == str(public_image.resolve())

    asyncio.run(run())


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], {"query": "", "keyword": ""}),
        (["--public"], {"query": "--public", "keyword": ""}),
        (["--public", "搜图"], {"query": "--public", "keyword": "搜图"}),
        (["secret"], {"query": "secret", "keyword": ""}),
    ],
)
def test_helps_public_option_parses_through_astrbot_command_filter(arguments, expected):
    from astrbot.core.star.filter.command import CommandFilter

    command = CommandFilter(
        "helps", handler_md=SimpleNamespace(handler=HelpTypst.show_menu)
    )
    assert (
        command.validate_and_convert_params(arguments, command.handler_params)
        == expected
    )
