# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置。

在仓库根执行：

    python -m PyInstaller packaging/campusnet.spec --noconfirm

三个要点：

1. 前端静态页与建表脚本是**只读资源**，按原相对路径放进包里，
   这样冻结后 `paths.RESOURCE_ROOT` 依然能用同一套相对路径找到它们；
   数据库与日志则由 `paths.data_home()` 指到用户目录（%LOCALAPPDATA%\\campusnet），
   否则单文件每次运行解包到新目录，配置每次都会丢。
2. 不打 tests / doc / _tmp：体积与分发扫描都要求。
3. console=True：这是命令行工具，doctor / serve 的输出必须看得见。
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 - SPECPATH 是 spec 所在目录

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "project" / "backend")],
    binaries=[],
    datas=[
        (str(ROOT / "project" / "frontend"), "project/frontend"),
        # 门户规则是数据文件：漏了它程序不会报错，但会静默变成「0 条规则」。
        (str(ROOT / "project" / "backend" / "campusnet" / "profiles"), "campusnet/profiles"),
        (str(ROOT / "database" / "schema.sql"), "database"),
        (str(ROOT / "database" / "config.example.yml"), "database"),
    ],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "playwright"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="campusnet",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
