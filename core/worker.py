import re
import shutil
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path

import typst

from ..domain import DefaultCFG
from ..utils import process_image_to_webp


def render_to_pipe(connection, task):
    """Child process entry point; process exit releases compiler memory."""
    try:
        connection.send(execute_render_task(task))
    finally:
        connection.close()


@dataclass
class RenderTask:
    template_path: str
    font_paths: list[str]
    json_str: str
    output_png_path: str
    output_dir: str
    timestamp: str
    query: str | None
    is_temp: bool
    req_id: str
    webp_limit: int = DefaultCFG.LIMIT_WEBP
    split_height: int = DefaultCFG.LIMIT_SIDE
    ppi: float = DefaultCFG.LIMIT_PPI
    ignore_system_fonts: bool = False


def execute_render_task(task: RenderTask) -> list[str]:
    """渲染子进程"""
    staged_json_path: Path | None = None
    workspace = None
    try:
        # 1. 准备参数
        workspace = tempfile.TemporaryDirectory(prefix=".render_", dir=task.output_dir)
        template_dir = Path(workspace.name) / "templates"
        shutil.copytree(
            Path(task.template_path).parent,
            template_dir,
            ignore=shutil.ignore_patterns(".help_typst_*.json", ".render_*"),
        )
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=template_dir,
            prefix=".help_typst_",
            suffix=".json",
            delete=False,
        ) as staged_json:
            staged_json_path = Path(staged_json.name)
            staged_json.write(task.json_str)

        sys_inputs = {
            "json_path": staged_json_path.name,
            "timestamp": task.timestamp,
        }
        if task.query:
            sys_inputs["query_regex"] = re.escape(task.query)

        # 2. 执行 Typst 编译
        typst.compile(
            str(template_dir / Path(task.template_path).name),
            output=task.output_png_path,
            font_paths=task.font_paths,
            ignore_system_fonts=task.ignore_system_fonts,
            format="png",
            ppi=task.ppi,
            sys_inputs=sys_inputs,
        )

        # 3. 调用图片处理
        # 计算文件名 stem
        src_path = Path(task.output_png_path)
        final_stem = f"temp_{task.req_id}" if task.is_temp else src_path.stem

        return process_image_to_webp(
            source_path=task.output_png_path,
            output_dir=task.output_dir,
            stem_name=final_stem,
            webp_limit=task.webp_limit,
            split_height=task.split_height,
        )

    except Exception:
        return [f"ERROR: {traceback.format_exc()}"]

    finally:
        if staged_json_path:
            try:
                staged_json_path.unlink(missing_ok=True)
            except OSError:
                pass

        if workspace is not None:
            workspace.cleanup()
