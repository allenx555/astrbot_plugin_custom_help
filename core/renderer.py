"""Bounded render jobs; published menus are cached by MenuStore, not KV."""

import asyncio
import multiprocessing
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from astrbot.api import logger

from ..domain import TypstPluginConfig
from ..utils.image import verify_image_header
from .worker import RenderTask, render_to_pipe


class RenderResult:
    def __init__(self, images: list[str], temp_files: list[Path]):
        self.images = images
        self.temp_files = temp_files


class TypstRenderer:
    def __init__(
        self,
        data_dir: Path,
        template_path: Path,
        font_dirs: list[Path],
        config: TypstPluginConfig,
    ):
        self.data_dir = data_dir
        self.template_path = template_path
        self.font_dirs = font_dirs
        self.cfg = config
        self._slots = asyncio.Semaphore(config.rendering.max_concurrent_tasks)
        self._processes = set()
        self._jobs = set()
        self._closed = False

    async def close(self):
        self._closed = True
        jobs = list(self._jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    @staticmethod
    async def _stop(process):
        if process.is_alive():
            process.terminate()
        await asyncio.to_thread(process.join, 2)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join)

    async def _compile(self, task):
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(target=render_to_pipe, args=(writer, task))
        deadline = (
            asyncio.get_running_loop().time() + self.cfg.rendering.timeout_compile
        )
        try:
            launch = asyncio.create_task(asyncio.to_thread(process.start))
            try:
                await asyncio.shield(launch)
            except asyncio.CancelledError:
                await launch
                raise
            self._processes.add(process)
            writer.close()

            async def receive():
                while not reader.poll():
                    if not process.is_alive():
                        raise RuntimeError("渲染进程退出且未返回结果")
                    await asyncio.sleep(0.05)
                return reader.recv()

            remaining = max(0, deadline - asyncio.get_running_loop().time())
            return await asyncio.wait_for(receive(), remaining)
        finally:
            writer.close()
            reader.close()
            if process.pid is not None:
                await self._stop(process)
                self._processes.discard(process)
                process.close()

    async def render(
        self,
        data_provider: Callable[[Path], int],
        mode: str,
        query: str | None = None,
        *,
        output_dir: Path | None = None,
    ):
        if mode not in {"command", "event", "filter"}:
            return None, "不支持的菜单类型"
        if self._closed:
            return None, "渲染器已关闭"
        temporary = output_dir is None
        directory = output_dir or self.data_dir / f"temp_{uuid.uuid4().hex}"
        directory = directory.resolve()
        directory.relative_to(self.data_dir.resolve())
        if directory == self.data_dir.resolve() or directory.exists():
            return None, "渲染目录必须是新的独立目录"
        success = False
        analysis = None
        job = asyncio.current_task()
        self._jobs.add(job)
        try:
            async with self._slots:
                if self._closed:
                    return None, "渲染器已关闭"
                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
                json_path = directory / "layout.json"
                png_path = directory / "menu.png"
                analysis = asyncio.create_task(
                    asyncio.to_thread(data_provider, json_path)
                )
                count = await asyncio.wait_for(
                    asyncio.shield(analysis), self.cfg.rendering.timeout_analysis
                )
                if count == 0:
                    return None, "empty"
                task = RenderTask(
                    template_path=str(self.template_path),
                    font_paths=[str(p) for p in self.font_dirs],
                    json_str=await asyncio.to_thread(
                        json_path.read_text, encoding="utf-8"
                    ),
                    output_png_path=str(png_path),
                    output_dir=str(directory),
                    timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                    query=query,
                    is_temp=False,
                    req_id=directory.name,
                    webp_limit=self.cfg.rendering.webp_limit,
                    split_height=self.cfg.rendering.split_height,
                    ppi=self.cfg.rendering.ppi,
                )
                images = await self._compile(task)
                if images and images[0].startswith("ERROR:"):
                    raise RuntimeError(images[0])
                if not images or any(not verify_image_header(Path(p)) for p in images):
                    raise RuntimeError("渲染图片缺失或损坏")
                await asyncio.to_thread(json_path.unlink, missing_ok=True)
                await asyncio.to_thread(png_path.unlink, missing_ok=True)
                success = True
                return RenderResult(images, [directory] if temporary else []), ""
        except asyncio.TimeoutError:
            return None, "菜单生成超时，已停止编译；旧菜单保持可用"
        except Exception as exc:
            logger.error(f"[HelpTypst] 渲染失败: {exc}", exc_info=True)
            return None, "菜单生成失败，请管理员检查日志"
        finally:
            self._jobs.discard(job)
            if not success:
                # Timed-out threads cannot be killed; clean after their last write.
                if analysis is not None and not analysis.done():

                    def cleanup(future):
                        try:
                            future.result()
                        except BaseException:
                            pass
                        shutil.rmtree(directory, ignore_errors=True)

                    analysis.add_done_callback(cleanup)
                else:
                    await asyncio.to_thread(
                        shutil.rmtree, directory, ignore_errors=True
                    )
