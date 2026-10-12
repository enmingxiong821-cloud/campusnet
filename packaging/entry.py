"""PyInstaller 单文件入口。

不直接用 campusnet/__main__.py：打包器要的是一个脚本文件，而包内模块在冻结
场景下的相对导入不可靠。这里只做一件事——把命令行入口暴露出来。
"""

from __future__ import annotations

from campusnet.cli import main

if __name__ == "__main__":  # pragma: no cover - 只在打包产物里执行
    raise SystemExit(main())
