"""分支保护的临时 canary：故意只在 windows 上失败。

用途：验证「windows 那格红了会不会真的挡住合并」。改名之前 ubuntu 与 windows
同叫 `Python 3.x`，windows 的红可能被 ubuntu 的绿顶掉；这个 PR 就是来撞那件事的。
验收完即关闭、删分支，不合并。
"""

import os

import pytest


@pytest.mark.skipif(os.name != "nt", reason="只在 windows 上失败；ubuntu 那格要绿")
def test_canary_fails_on_windows_only() -> None:
    raise AssertionError("故意失败：用于验证 must-pass 检查会挡住合并")
