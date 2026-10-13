import asyncio
import shutil
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools

from .core import CommandAnalyzer, EventAnalyzer, FilterAnalyzer, TypstRenderer
from .core.menu import MenuDocument, MenuStore, export_document, search_published
from .domain import InternalCFG, TypstPluginConfig
from .utils import FontManager, HelpHint, MsgRecall, TypstLayout


class HelpTypst(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        # 1. 静态资源路径
        self.plugin_dir = Path(__file__).parent
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.template_path = self.plugin_dir / "templates" / InternalCFG.NAME_TEMPLATE
        self.schema_path = self.plugin_dir / "_conf_schema.json"

        # 2. 配置加载
        self.config = config
        self.plugin_config = TypstPluginConfig.load(config)

        # 3. 获取字体
        raw_path = self.plugin_config.custom_font_path
        if raw_path and raw_path.strip():
            self.user_font_dir = Path(raw_path)  # 自定义字体目录
        else:
            self.user_font_dir = self.data_dir / "fonts"  # 缺省值

        try:
            self.user_font_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            if not raw_path:
                logger.warning(f"[HelpTypst] 无法创建默认字体目录: {e}")

        self.builtin_font_dir = (
            self.plugin_dir / "resources" / InternalCFG.NAME_FONT_DIR
        )  # 内置
        self.font_dirs = [self.builtin_font_dir, self.user_font_dir]  # 汇总

        # 3. 初始化组件
        self.font_manager = FontManager(self.font_dirs)
        self.layout = TypstLayout(self.plugin_config)
        self.hint = HelpHint()
        self.msg = MsgRecall()

        # 4. 渲染器
        self.renderer = TypstRenderer(
            data_dir=self.data_dir,
            template_path=self.template_path,
            font_dirs=self.font_dirs,
            config=self.plugin_config,
        )

        # 5. 分析器
        self.cmd_analyzer = CommandAnalyzer(context, self.plugin_config)
        self.evt_analyzer = EventAnalyzer(context, self.plugin_config)
        self.flt_analyzer = FilterAnalyzer(context, self.plugin_config)

        self.prefixes: list[str] = []
        self.menu_store = MenuStore(self.data_dir)
        self._menu_lock = asyncio.Lock()
        self._background_tasks = set()

    async def initialize(self):
        """异步初始化"""
        self._init_prefixes(self.context)
        await asyncio.to_thread(self._refresh_resources)
        logger.info("[HelpTypst] 初始化完成")

    def _refresh_resources(self):
        try:
            # 1. 扫描
            self.font_manager.scan_fonts()

            # 2. 更新 Schema
            self.font_manager.update_json_schema(self.schema_path)

        except Exception as e:
            logger.warning(f"[HelpTypst] 资源重载失败: {e}")

    async def terminate(self):
        """周期hook"""
        await self.renderer.close()
        tasks = [
            task
            for task in self._background_tasks
            if task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 1. 清理临时文件
        await self._perform_cleanup()

    def _start_background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _perform_cleanup(self):
        try:
            # glob 匹配
            temp_files = list(self.data_dir.glob("temp_*"))
            if not temp_files:
                return

            logger.debug(f"[HelpTypst] 清理 {len(temp_files)} 个缓存文件...")

            for f in temp_files:
                try:
                    if f.exists():  # 双重检查
                        if f.is_dir():
                            await asyncio.to_thread(shutil.rmtree, f)
                        else:
                            await asyncio.to_thread(f.unlink)
                except OSError:
                    pass

        except Exception as e:
            logger.warning(f"[HelpTypst] 清理失败: {e}")

    @filter.command_group("typst")  # 该指令组留待扩展更多调试功能
    @filter.permission_type(filter.PermissionType.ADMIN)
    def typst(self):
        pass

    @typst.command("font")
    async def cmd_scan_fonts(self, event: AstrMessageEvent):
        """扫描字体并重载插件"""
        # 1. 扫描与更新
        await asyncio.to_thread(self._refresh_resources)
        count = len(self.font_manager.available_families)

        # 2. 尝试自我重载
        try:
            pm = getattr(
                self.context, "_star_manager", None
            )  # hack: 获取 PluginManager 实例
            if pm:
                plugin_name = getattr(self, "name", "astrbot_plugin_custom_help")
                yield event.plain_result(
                    f"✅ 扫描完成 ({count} fonts)。正在重载以刷新面板..."
                )
                self._start_background(self._safe_reload(pm, plugin_name))
            else:
                yield event.plain_result(f"✅ 扫描完成 ({count} fonts)。请手动重载插件")
        except Exception as e:
            yield event.plain_result(f"❌ 自动重载失败: {e}")

    async def _safe_reload(self, pm, plugin_name):
        """延迟重载"""
        await asyncio.sleep(InternalCFG.DELAY_SEND)
        try:
            logger.info(f"[HelpTypst] 正在执行自我重载: {plugin_name}")
            await pm.reload(plugin_name)
        except Exception as e:
            logger.error(f"[HelpTypst] 自我重载异常: {e}")

    @typst.command("export")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def export_menu(self, event: AstrMessageEvent):
        """导出完整命令清单，保留已有菜单编辑。"""
        try:
            async with self._menu_lock:
                # Runtime registry access stays on the event loop.
                plugins = self.cmd_analyzer.export_all()
                inventory = self.cmd_analyzer.export_inventory()
                existing = (
                    await asyncio.to_thread(MenuDocument.read, self.menu_store.source)
                    if self.menu_store.source.exists()
                    else None
                )
                document = export_document(
                    plugins, self.plugin_config.ignored_plugins, inventory, existing
                )
                await asyncio.to_thread(self.menu_store.save_source, document)
            yield event.plain_result(
                f"已导出 {len(document.plugins)} 个命令分类（含隐藏、系统、管理员及仍注册的停用项）。\n"
                f"文件：{self.menu_store.source}\n"
                "修改插件或节点的 visible，再执行 /typst build。重新导出会保留编辑；新增项默认隐藏。"
            )
        except Exception as e:
            logger.error(f"[HelpTypst] 菜单导出失败: {e}", exc_info=True)
            yield event.plain_result(f"菜单导出失败，原文件保留：{e}")

    @typst.command("build")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def build_menu(self, event: AstrMessageEvent):
        """从 menu.json 手动生成并发布固定菜单。"""
        generation = None
        committed = False
        try:
            async with self._menu_lock:
                document = await asyncio.to_thread(
                    MenuDocument.read, self.menu_store.source
                )
                plugins = document.visible_plugins()
                admin_plugins = document.visible_plugins(include_hidden=True)
                if not admin_plugins:
                    raise ValueError("没有可用命令，请检查插件及节点的 available")
                generation = await asyncio.to_thread(self.menu_store.new_generation)
                self._init_prefixes(self.context)
                prefixes = list(self.prefixes)
                fonts = self.font_manager.get_render_font_list(
                    self.plugin_config.appearance.get_active_font_order()
                )

                async def render_variant(items, name, title):
                    directory = generation / name

                    def pipeline(path):
                        self.layout.dump_layout_json(
                            items,
                            path,
                            title,
                            "command",
                            prefixes,
                            fonts,
                            admin_view=name == "admin",
                        )
                        # An entirely hidden menu still has a public title/legend.
                        return max(1, len(items))

                    result, error = await self.renderer.render(
                        pipeline, "command", output_dir=directory
                    )
                    if not result:
                        raise RuntimeError(f"{name} 菜单生成失败：{error}")
                    return result

                result = await render_variant(plugins, "public", document.title)
                admin_result = await render_variant(
                    admin_plugins, "admin", f"{document.title}（管理员完整版）"
                )
                publication = asyncio.create_task(
                    asyncio.to_thread(
                        self.menu_store.publish,
                        generation,
                        result.images,
                        document,
                        prefixes,
                        admin_result.images,
                    )
                )
                try:
                    published = await asyncio.shield(publication)
                    committed = True
                except asyncio.CancelledError:
                    # Atomic publication cannot be interrupted halfway through
                    # by reload; finish before deciding whether to discard files.
                    await publication
                    committed = True
                    raise
                try:
                    await asyncio.to_thread(self.menu_store.collect_garbage)
                except Exception:
                    logger.warning("[HelpTypst] 旧菜单目录清理失败", exc_info=True)
            yield event.plain_result(
                f"菜单已生成：{len(result.images)} 张图片，{len(plugins)} 个可见分类。\n"
                f"管理员完整版：{len(admin_result.images)} 张图片，{len(admin_plugins)} 个分类（浅色为隐藏功能）。\n"
                f"生成时间：{published['generated_at']}\n使用 /helps 查看。"
            )
        except FileNotFoundError:
            yield event.plain_result(
                "菜单文件不存在，请先执行 /typst export。旧的已发布菜单保持可用。"
            )
        except Exception as e:
            logger.error(f"[HelpTypst] 手动生成失败: {e}", exc_info=True)
            yield event.plain_result(f"菜单生成失败，旧的已发布菜单保持可用：{e}")
        finally:
            if generation is not None and not committed:
                try:
                    await asyncio.to_thread(
                        self.menu_store.discard_generation, generation
                    )
                except Exception:
                    logger.warning("[HelpTypst] 未发布目录清理失败", exc_info=True)

    @typst.command("status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def menu_status(self, event: AstrMessageEvent):
        """查看菜单源文件及已发布图片状态。"""
        try:
            payload, images, _ = await asyncio.to_thread(self.menu_store.read_published)
            yield event.plain_result(
                f"菜单文件：{self.menu_store.source}\n"
                f"生成时间：{payload['generated_at']}\n图片数量：{len(images)}\n"
                "编辑菜单后需执行 /typst build，重载不会自动生成。"
            )
        except Exception as e:
            yield event.plain_result(
                f"菜单尚未发布或不可用：{e}\n菜单文件：{self.menu_store.source}"
            )

    async def _handle_request(
        self,
        event: AstrMessageEvent,
        analyzer,
        title: str,
        mode: str,
        query: str | None,
    ):
        """通用请求处理逻辑"""
        wait_msg_id = None

        try:
            plugins = analyzer.get_plugins(query)
        except Exception:
            logger.warning("[HelpTypst] 诊断数据读取失败", exc_info=True)
            yield event.plain_result("诊断数据读取失败，请检查插件日志。")
            return

        if self.plugin_config.enable_waiting_message:
            # 1. 发送提示
            hint_text = (
                self.hint.msg_searching(query)
                if query
                else self.hint.msg_rendering(mode)
            )
            wait_msg_id = await self.msg.send_wait(event, hint_text)

        def data_pipeline(save_path: Path) -> int:
            """数据流转"""
            # 数据层：获取对象
            if not plugins:
                return 0

            # 视图层：决定标题 & 计算布局 & 写入JSON
            display_title = f'搜索结果: "{query}"' if query else title
            user_fonts = (
                self.plugin_config.appearance.get_active_font_order()
            )  # 预设字体配置
            final_font_list = self.font_manager.get_render_font_list(user_fonts)  # 校验

            self.layout.dump_layout_json(
                plugins=plugins,
                save_path=save_path,
                title=display_title,
                mode=mode,
                prefixes=self.prefixes,
                font_list=final_font_list,
            )

            return len(plugins)

        # 2. 执行渲染
        try:
            result, error = await self.renderer.render(data_pipeline, mode, query)
        finally:
            if wait_msg_id:
                await self.msg.recall(event, wait_msg_id)

        # 4. 处理结果
        if result:
            try:
                yield event.chain_result(
                    [Image.fromFileSystem(p) for p in result.images]
                )
            finally:
                # 后台任务清理文件列表
                if result.temp_files:
                    self._start_background(self._cleanup_task(result.temp_files))
        else:
            # 错误处理
            if error == "empty":
                yield event.plain_result(self.hint.msg_empty_result(mode, query))
            else:
                yield event.plain_result(error)

    async def _cleanup_task(self, files: list[Path]):
        """异步清理任务"""
        await asyncio.sleep(InternalCFG.DELAY_SEND)
        for p in files:
            try:
                if p.exists():
                    if p.is_dir():
                        await asyncio.to_thread(shutil.rmtree, p)
                    else:
                        await asyncio.to_thread(p.unlink)
            except Exception as e:
                logger.warning(f"[HelpTypst] 临时文件清理失败 {p}: {e}")

    def _init_prefixes(self, context: Context):
        """唤醒词"""
        try:
            global_config = context.get_config()
            raw = global_config.get("wake_prefix", ["/"])
            self.prefixes = [raw] if isinstance(raw, str) else list(raw)
        except Exception as e:
            logger.warning(f"[HelpTypst] 获取唤醒词失败，使用默认值 '/': {e}")
            self.prefixes = ["/"]

    @filter.command("helps")
    async def show_menu(self, event: AstrMessageEvent, query: str = ""):
        """按管理员身份发送固定菜单及搜索结果。"""
        try:
            admin = event.is_admin()
            payload, images, document = await asyncio.to_thread(
                self.menu_store.read_published, admin=admin
            )
            if admin and "admin_images" not in payload:
                yield event.plain_result(
                    "管理员完整版尚未生成，暂用普通版；请执行 /typst build。"
                )
            if query.strip():
                chunks = search_published(
                    document,
                    query,
                    payload.get("prefixes", ["/"]),
                    include_hidden=admin and "admin_images" in payload,
                )
                if not chunks:
                    yield event.plain_result("已发布菜单中没有匹配的可见功能。")
                for chunk in chunks:
                    yield event.plain_result(chunk)
            else:
                yield event.chain_result([Image.fromFileSystem(str(p)) for p in images])
        except Exception:
            logger.warning("[HelpTypst] 已发布菜单不可用", exc_info=True)
            yield event.plain_result(
                "菜单尚未生成或图片不可用，请管理员执行 /typst export 和 /typst build。"
            )

    @filter.command("events")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_events(self, event: AstrMessageEvent, query: str = ""):
        """显示事件监听列表"""
        async for r in self._handle_request(
            event, self.evt_analyzer, "AstrBot 事件监听", "event", query
        ):
            yield r

    @filter.command("filters")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def show_filters(self, event: AstrMessageEvent, query: str = ""):
        """显示过滤器详情"""
        async for r in self._handle_request(
            event, self.flt_analyzer, "AstrBot 过滤器分析", "filter", query
        ):
            yield r
