"""版本号必须三处一致：后端常量、扩展清单、CHANGELOG 最新已发布段。

这三处靠手改，历史上已经漂过一次——扩展清单停在 1.x 而后端早就是 2.0.0，
用户装上插件看到的版本号与命令行 `--version` 对不上。发布时最容易漏的就是
「改了后端忘了改清单」，所以在这里钉死。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from campusnet import __version__

ROOT = Path(__file__).resolve().parents[3]
CHANGELOG = ROOT / "CHANGELOG.md"
MANIFEST = ROOT / "project/frontend/extension/manifest.json"

RELEASED_SECTION = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def test_extension_manifest_tracks_backend_version() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["version"] == __version__, "扩展清单版本号与后端不一致"


def test_changelog_newest_released_section_is_current_version() -> None:
    """`## [未发布]` 不算已发布段：它记的是 main 上还没打 tag 的内容。"""
    versions = RELEASED_SECTION.findall(CHANGELOG.read_text(encoding="utf-8"))

    assert versions, "CHANGELOG 里一个已发布版本段都没有"
    assert versions[0] == __version__, "CHANGELOG 最新段与当前版本号不一致"
