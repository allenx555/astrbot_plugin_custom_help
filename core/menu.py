"""Editable menu source and immutable published generations."""

import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..domain import PluginMetadata, RenderNode
from ..utils.image import verify_image_header


class MenuNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    desc: str = ""
    visible: StrictBool = True
    available: StrictBool = True
    is_group: StrictBool = False
    tag: str = "normal"
    aliases: list[str] = Field(default_factory=list)
    children: list["MenuNode"] = Field(default_factory=list)


class MenuPlugin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    display_name: str | None = None
    desc: str = ""
    version: str = ""
    visible: StrictBool = True
    available: StrictBool = True
    reserved: StrictBool = False
    nodes: list[MenuNode] = Field(default_factory=list)


class MenuDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    title: str = "AstrBot 指令菜单"
    plugins: list[MenuPlugin]
    inventory: list[dict] = Field(default_factory=list)

    @classmethod
    def read(cls, path: Path):
        document = cls.model_validate_json(path.read_text(encoding="utf-8-sig"))
        if document.schema_version != 1:
            raise ValueError("不支持的菜单 schema_version")
        ids = set()

        def check(items):
            for item in items:
                if not item.id or item.id in ids:
                    raise ValueError(f"菜单 ID 为空或重复: {item.id}")
                ids.add(item.id)
                check(item.nodes if isinstance(item, MenuPlugin) else item.children)

        check(document.plugins)
        return document

    def visible_plugins(self, include_hidden=False) -> list[PluginMetadata]:
        def nodes(items, inherited_hidden=False):
            result = []
            for item in items:
                if not item.available or (not include_hidden and not item.visible):
                    continue
                hidden = inherited_hidden or not item.visible
                children = nodes(item.children, hidden)
                if item.is_group and not children:
                    continue
                result.append(
                    RenderNode(
                        hidden=hidden,
                        name=item.name,
                        desc=item.desc,
                        is_group=item.is_group,
                        tag=item.tag,
                        aliases=item.aliases,
                        children=children,
                    )
                )
            return result

        result = []
        for plugin in self.plugins:
            if not plugin.available or (not include_hidden and not plugin.visible):
                continue
            visible = nodes(plugin.nodes, not plugin.visible)
            if visible:
                result.append(
                    PluginMetadata(
                        hidden=not plugin.visible,
                        name=plugin.name,
                        display_name=plugin.display_name,
                        desc=plugin.desc,
                        version=plugin.version,
                        nodes=visible,
                    )
                )
        return result


def export_document(plugins, ignored_plugins, inventory, existing=None):
    """IDs identify source commands; edits/order survive subsequent exports."""

    def node_list(items, plugin_id, path=(), inherited_admin=False):
        result = []
        for node in items:
            parts = (*path, node.name)
            admin = inherited_admin or node.tag == "admin"
            result.append(
                MenuNode(
                    id=json.dumps([plugin_id, *parts], ensure_ascii=False),
                    name=node.name,
                    desc=node.desc,
                    is_group=node.is_group,
                    tag="admin" if admin else node.tag,
                    aliases=node.aliases,
                    visible=not admin and node.enabled,
                    available=node.enabled,
                    children=node_list(node.children, plugin_id, parts, admin),
                )
            )
        return result

    fresh = MenuDocument(
        plugins=[
            MenuPlugin(
                id=p.name,
                name=p.name,
                display_name=p.display_name,
                desc=p.desc,
                version=p.version,
                reserved=p.reserved,
                available=p.activated,
                visible=p.name not in ignored_plugins
                and not p.reserved
                and p.activated,
                nodes=node_list(p.nodes, p.name),
            )
            for p in plugins
        ],
        inventory=inventory,
    )
    if existing is None:
        return fresh

    def merge(old_items, new_items):
        new_by_id = {item.id: item for item in new_items}
        merged = []
        for old in old_items:
            item = old.model_copy(deep=True)
            new = new_by_id.pop(item.id, None)
            if new is None:
                if not item.id.startswith("custom:"):
                    item.available = False
            else:
                item.available = new.available
                if isinstance(item, MenuPlugin):
                    item.version = new.version
                    item.reserved = new.reserved
                    item.nodes = merge(item.nodes, new.nodes)
                else:
                    item.tag = new.tag
                    item.aliases = new.aliases
                    item.children = merge(item.children, new.children)
            merged.append(item)
        for item in new_by_id.values():
            item.visible = False  # Newly discovered commands require review.
            merged.append(item)
        return merged

    fresh.title = existing.title
    fresh.plugins = merge(existing.plugins, fresh.plugins)
    return fresh


def atomic_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".menu_",
            suffix=".json",
            delete=False,
        ) as f:
            temporary = Path(f.name)
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class MenuStore:
    def __init__(self, data_dir):
        self.source = data_dir / "menu.json"
        self.root = data_dir / "published_menu"
        self.manifest = self.root / "current.json"

    def save_source(self, document):
        if self.source.exists():
            # Preserve the previous editable source before replacing it.
            atomic_json(
                self.source.with_suffix(".json.bak"),
                json.loads(self.source.read_text(encoding="utf-8-sig")),
            )
        atomic_json(self.source, document.model_dump())

    def new_generation(self):
        directory = self.root / uuid.uuid4().hex
        directory.mkdir(parents=True)
        return directory

    def _generation_path(self, path):
        path = Path(path)
        if path.is_symlink():
            raise ValueError("发布目录不能是符号链接")
        path = path.resolve()
        if path.parent != self.root.resolve() or not re.fullmatch(
            r"[0-9a-f]{32}", path.name
        ):
            raise ValueError("无效的发布目录")
        return path

    def discard_generation(self, generation):
        generation = self._generation_path(generation)
        if self.manifest.exists():
            current = json.loads(self.manifest.read_text(encoding="utf-8"))
            if Path(current["menu"]).parts[0] == generation.name:
                return
        shutil.rmtree(generation, ignore_errors=True)

    def collect_garbage(self, retain=3, min_age_days=7):
        """Keep current/recent generations; allow in-flight sends ample time."""
        current = json.loads(self.manifest.read_text(encoding="utf-8"))
        candidates = [
            p
            for p in self.root.iterdir()
            if p.is_dir()
            and not p.is_symlink()
            and re.fullmatch(r"[0-9a-f]{32}", p.name)
        ]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        cutoff = time.time() - min_age_days * 86400
        protected = {p.name for p in candidates[:retain]}
        protected.add(Path(current["menu"]).parts[0])
        for generation in candidates:
            if generation.name not in protected and generation.stat().st_mtime < cutoff:
                self.discard_generation(generation)

    def publish(self, generation, images, document, prefixes, admin_images=None):
        generation = self._generation_path(generation)
        for image in [*images, *(admin_images or [])]:
            Path(image).resolve().relative_to(generation)
        if not images or any(not verify_image_header(Path(p)) for p in images):
            raise ValueError("生成的菜单图片缺失或损坏")
        relative = [
            str(Path(p).resolve().relative_to(self.root.resolve())) for p in images
        ]
        admin_relative = None
        if admin_images is not None:
            if not admin_images or any(
                not verify_image_header(Path(p)) for p in admin_images
            ):
                raise ValueError("生成的管理员菜单图片缺失或损坏")
            admin_relative = [
                str(Path(p).resolve().relative_to(self.root.resolve()))
                for p in admin_images
            ]
        snapshot = generation / f"menu_{uuid.uuid4().hex}.json"
        atomic_json(snapshot, document.model_dump())
        payload = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "images": relative,
            "menu": str(snapshot.relative_to(self.root.resolve())),
            "prefixes": prefixes,
        }
        if admin_relative is not None:
            payload["admin_images"] = admin_relative
        # Commit only after all images and the published source are ready.
        atomic_json(self.manifest, payload)
        return payload

    def read_published(self, admin=False):
        payload = json.loads(self.manifest.read_text(encoding="utf-8"))

        def resolve(relative):
            path = (self.root / relative).resolve()
            path.relative_to(self.root.resolve())
            return path

        images = (
            [resolve(p) for p in payload.get("admin_images", payload["images"])]
            if admin
            else [resolve(p) for p in payload["images"]]
        )
        if not images or any(not verify_image_header(p) for p in images):
            raise ValueError("已发布菜单图片缺失或损坏，请管理员重新生成")
        document = MenuDocument.read(resolve(payload["menu"]))
        generation = self._generation_path(resolve(payload["menu"]).parent)
        for image in images:
            image.relative_to(generation)
        return payload, images, document


def search_published(document, query, prefixes, include_hidden=False):
    """Search the published source for the selected audience."""
    q = query.casefold().strip()
    lines = []
    prefix = prefixes[0] if prefixes else "/"
    for plugin in document.visible_plugins(include_hidden=include_hidden):
        label = plugin.display_name or plugin.name
        plugin_match = q in " ".join([label, plugin.name, plugin.desc]).casefold()

        def walk(nodes, parents=(), inherited_match=False):
            for node in nodes:
                path = (*parents, node.name)
                match = (
                    plugin_match
                    or inherited_match
                    or q in " ".join([node.name, node.desc, *node.aliases]).casefold()
                )
                if node.is_group:
                    walk(node.children, path, match)
                elif match:
                    marker = "（隐藏）" if node.hidden else ""
                    lines.append(
                        f"{label} · {prefix}{' '.join(path)}{marker}：{node.desc}"
                    )

        walk(plugin.nodes)
    # Bound message size; do not silently truncate the result set.
    chunks = []
    current = ""
    for line in lines:
        for start in range(0, len(line), 1800):
            part = line[start : start + 1800]
            if len(current) + len(part) + 1 > 1800:
                chunks.append(current)
                current = ""
            current += part + "\n"
    if current:
        chunks.append(current.rstrip())
    return chunks
