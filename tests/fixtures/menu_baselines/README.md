# 真实菜单图片基准

- 输入：`../manual_menu.json`，来自 2026-10-14 的实际菜单。
- `public.webp`：普通用户版；`admin.webp`：管理员完整版。
- 通过真正的 `HelpTypst.build_menu` 生成，固定时间为 `2026-10-14 12:00:00`。
- 使用仓库字体和默认配置（180 PPI），包含实际 WebP 转换结果。
- Emoji 使用 `Custom Help Emoji` 模板图标子集；2026-10-14 精简字体后经人工检查更新两版图片基准。
- 基准测试显式禁用系统字体，防止不同机器的字体回退影响排版；生产渲染仍允许使用系统字体。
- 渲染依赖固定为 `typst==0.15.0`、`Pillow==12.3.0`，见 `../../render-constraints.txt`。发布工作流自动应用这些约束。

本地测试先准备相同的渲染依赖：

```shell
python -m pip install -c tests/render-constraints.txt -r requirements.txt
```

运行 `python -m pytest tests/test_menu_fixture_build.py -q` 会重新构建两版，
完整解码图片，检查尺寸，并逐像素比较 RGB 图片，各通道 RMS 误差上限为 1
（像素值范围 0..255，允许渲染器或编码器的微小舍入差异）。
当前图片保存在 pytest 临时目录的 `public.webp` 和 `admin.webp`；
像素比较失败时还会保存 `public-diff.png` 或 `admin-diff.png`。
可用 `--basetemp=<新的输出目录>` 指定位置。pytest 会清理指定目录，勿指向基准目录或其他有用文件。
GitHub Actions 会保存两版实际输出和像素差异图为 `menu-render-results-<tag>` artifact，保留 14 天，即使测试失败也可下载检查。

修改布局后，先人工检查实际输出，再明确更新基准。PowerShell：

```powershell
$env:UPDATE_MENU_BASELINES = "1"
python -m pytest tests/test_menu_fixture_build.py -q
Remove-Item Env:UPDATE_MENU_BASELINES
```

普通测试不会自动覆盖基准；更新后的两张图片需要一起提交。
更换 Typst、Pillow 或字体后也应检查输出，避免把渲染环境差异误认为产品变更。
这些测试验证本地构建和消息图片选择，不验证 NAS 插件加载或 QQ 实际发送。
