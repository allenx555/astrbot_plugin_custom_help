import asyncio
import multiprocessing
import time
from pathlib import Path

import pytest
from astrbot_plugin_custom_help.core import renderer as module
from astrbot_plugin_custom_help.core.renderer import TypstRenderer
from astrbot_plugin_custom_help.core.worker import RenderTask
from astrbot_plugin_custom_help.domain import TypstPluginConfig
from PIL import Image


def slow_child(connection, task):
    time.sleep(30)


def renderer(tmp_path):
    return TypstRenderer(
        tmp_path, tmp_path / "template.typ", [], TypstPluginConfig.load({})
    )


def task(tmp_path):
    return RenderTask(
        "template.typ",
        [],
        "{}",
        str(tmp_path / "menu.png"),
        str(tmp_path),
        "test",
        None,
        False,
        "test",
    )


def test_compile_timeout_terminates_real_process_and_keeps_event_loop_responsive(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(module, "render_to_pipe", slow_child)
    service = renderer(tmp_path)
    service.cfg.rendering.timeout_compile = 0.2
    before = {child.pid for child in multiprocessing.active_children()}

    async def run():
        job = asyncio.create_task(service._compile(task(tmp_path)))
        ticks = 0
        while not job.done():
            ticks += 1
            await asyncio.sleep(0.02)
        with pytest.raises(asyncio.TimeoutError):
            await job
        assert ticks >= 3
        assert not service._processes

    asyncio.run(run())
    assert {child.pid for child in multiprocessing.active_children()} == before


def test_jobs_share_limit_and_cannot_overwrite_published_directory(
    tmp_path, monkeypatch
):
    service = renderer(tmp_path)
    service._slots = asyncio.Semaphore(1)
    running = maximum = 0

    async def compile(task):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        await asyncio.sleep(0.03)
        path = Path(task.output_dir) / "menu.webp"
        Image.new("RGB", (20, 20)).save(path)
        running -= 1
        return [str(path)]

    def pipeline(path):
        path.write_text("{}", encoding="utf-8")
        return 1

    monkeypatch.setattr(service, "_compile", compile)

    async def run():
        outputs = [tmp_path / "public", tmp_path / "admin"]
        results = await asyncio.gather(
            *(service.render(pipeline, "command", output_dir=path) for path in outputs)
        )
        assert maximum == 1 and all(result for result, _ in results)
        assert results[0][0].images != results[1][0].images
        assert not list(tmp_path.rglob("*.json"))
        assert not (await service.render(pipeline, "command", output_dir=outputs[0]))[0]
        assert (outputs[0] / "menu.webp").exists()
        await service.close()
        assert not (await service.render(pipeline, "command"))[0]

    asyncio.run(run())


def test_close_cancels_active_compile_and_cleans_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "render_to_pipe", slow_child)
    service = renderer(tmp_path)

    def pipeline(path):
        path.write_text("{}", encoding="utf-8")
        return 1

    async def run():
        job = asyncio.create_task(service.render(pipeline, "event"))
        while not service._processes:
            await asyncio.sleep(0.01)
        await service.close()
        assert job.cancelled()
        assert not service._processes
        assert not list(tmp_path.glob("temp_*"))

    asyncio.run(run())


def test_analysis_timeout_cleans_late_thread_writes(tmp_path):
    service = renderer(tmp_path)
    service.cfg.rendering.timeout_analysis = 0.02

    def slow_pipeline(path):
        time.sleep(0.08)
        path.write_text("hidden data", encoding="utf-8")
        return 1

    async def run():
        result, error = await service.render(slow_pipeline, "event")
        assert result is None and "超时" in error
        await asyncio.sleep(0.15)
        assert not list(tmp_path.glob("temp_*"))

    asyncio.run(run())


def test_font_self_reload_does_not_cancel_or_await_its_own_task(tmp_path):
    from astrbot_plugin_custom_help.main import HelpTypst

    bot = object.__new__(HelpTypst)
    bot.data_dir = tmp_path
    bot.renderer = renderer(tmp_path)
    bot._background_tasks = set()

    async def reload_like_plugin_manager():
        bot._background_tasks.add(asyncio.current_task())
        await bot.terminate()

    asyncio.run(reload_like_plugin_manager())


@pytest.mark.parametrize(
    "settings",
    [
        {"max_concurrent_tasks": 0},
        {"max_concurrent_tasks": 1.5},
        {"timeout_compile": float("nan")},
        {"ppi": float("inf")},
        {"split_height": 0},
        {"split_height": 20000},
    ],
)
def test_invalid_render_settings_rejected(settings):
    with pytest.raises(ValueError):
        TypstPluginConfig.load({"rendering": settings})
