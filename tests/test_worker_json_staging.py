from __future__ import annotations

import json
from pathlib import Path

from astrbot_plugin_custom_help.core import worker


def _render_task(template: Path, output_dir: Path, payload: str) -> worker.RenderTask:
    return worker.RenderTask(
        template_path=str(template),
        font_paths=[],
        json_str=payload,
        output_png_path=str(output_dir / "menu.png"),
        output_dir=str(output_dir),
        timestamp="now",
        query=None,
        is_temp=True,
        req_id="request",
    )


def test_render_stages_json_as_a_file_and_cleans_it(monkeypatch, tmp_path):
    template = tmp_path / "base.typ"
    template.write_text("#let data = json(sys.inputs.json_path)", encoding="utf-8")
    payload = json.dumps({"plugins": ["x" * 10_000]})
    observed = {}

    def fake_compile(template_path, **kwargs):
        observed["inputs"] = kwargs["sys_inputs"]
        staged = Path(template_path).parent / kwargs["sys_inputs"]["json_path"]
        observed["path"] = staged
        assert staged.read_text(encoding="utf-8") == payload

    monkeypatch.setattr(worker.typst, "compile", fake_compile)
    monkeypatch.setattr(worker, "process_image_to_webp", lambda **_kwargs: ["ok.webp"])

    result = worker.execute_render_task(_render_task(template, tmp_path, payload))

    assert result == ["ok.webp"]
    assert "json_string" not in observed["inputs"]
    assert not observed["path"].exists()
    assert observed["path"].parent != template.parent
    assert not list(tmp_path.glob(".render_*"))


def test_staged_json_is_cleaned_when_writing_fails(monkeypatch, tmp_path):
    template = tmp_path / "base.typ"
    template.write_text("#let data = json(sys.inputs.json_path)", encoding="utf-8")
    original_named_tempfile = worker.tempfile.NamedTemporaryFile

    class _FailingWriteFile:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.name = wrapped.name

        def __enter__(self):
            self._wrapped.__enter__()
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self._wrapped.__exit__(exc_type, exc, traceback)

        def write(self, _value):
            raise OSError("simulated disk-full write failure")

    def failing_named_tempfile(*args, **kwargs):
        return _FailingWriteFile(original_named_tempfile(*args, **kwargs))

    monkeypatch.setattr(worker.tempfile, "NamedTemporaryFile", failing_named_tempfile)

    result = worker.execute_render_task(_render_task(template, tmp_path, "{}"))

    assert result[0].startswith("ERROR:")
    assert list(tmp_path.glob(".help_typst_*.json")) == []
