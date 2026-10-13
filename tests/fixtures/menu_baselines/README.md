# 真实菜单图片基准

- 输入：`../manual_menu.json`，来自 2026-10-14 的实际菜单。
- `public.webp`：普通用户版；`admin.webp`：管理员完整版。
- 通过真正的 `HelpTypst.build_menu` 生成，固定时间为 `2026-10-14 12:00:00`。
- 使用仓库字体和默认配置（180 PPI），包含实际 WebP 转换结果。

运行 `python -m pytest tests/test_menu_fixture_build.py -q` 会重新构建两版，
完整解码图片，检查尺寸，并逐像素比较 RGB 图片，各通道 RMS 误差上限为 1
（像素值范围 0..255，允许渲染器或编码器的微小舍入差异）。
当前图片保存在 pytest 临时目录的 `public.webp` 和 `admin.webp`；
像素比较失败时还会保存 `public-diff.png` 或 `admin-diff.png`。
可用 `--basetemp=<新的输出目录>` 指定位置。pytest 会清理指定目录，勿指向基准目录或其他有用文件。

修改布局后，先人工检查实际输出，再明确更新基准。PowerShell：

```powershell
$env:UPDATE_MENU_BASELINES = "1"
python -m pytest tests/test_menu_fixture_build.py -q
Remove-Item Env:UPDATE_MENU_BASELINES
```

普通测试不会自动覆盖基准；更新后的两张图片需要一起提交。
更换 Typst、Pillow 或字体后也应检查输出，避免把渲染环境差异误认为产品变更。
这些测试验证本地构建和消息图片选择，不验证 NAS 插件加载或 QQ 实际发送。
