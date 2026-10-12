"""检查拟分发源码或实际发布目录中的敏感内容。

--repo 只扫描将进入发布包的源码；--root 扫描已收集的发布目录。
报告仅打印文件名和规则名，不把命中的凭据值写入 CI 日志。
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


SENSITIVE_NAMES = {"config.json", ".env", ".campusnet_obf.key"}
SENSITIVE_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".key",
    ".api-token",
    ".log",
    ".lock",
)
ACCOUNT_NUMBER = re.compile(r"(?<!\d)20\d{8}(?!\d)")
PHONE_NUMBER = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
PRIVATE_PORTAL = re.compile(r"(?<!\d)172\.26\.8\.11(?!\d)")
QUOTED_CREDENTIAL = re.compile(
    r"(?i)(?<![a-z0-9_])(?:password|passwd|pwd|api_?key|secret|token)"
    r"\s*[:=]\s*(['\"])([^'\"\r\n]{7,})\1"
)
ENV_CREDENTIAL = re.compile(
    r"(?m)(?<![a-z0-9_])(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY)"
    r"\s*=\s*([A-Za-z0-9_-]{6,})(?![A-Za-z0-9_-])"
)
CHINESE_CREDENTIAL = re.compile(
    r"(?:密码|口令|令牌)\s*[:：=]\s*(['\"])([^'\"\r\n]{6,})\1"
)


def is_distribution_source(relative: Path) -> bool:
    parts = relative.parts
    if parts[:2] == ("project", "backend") and "tests" in parts:
        return False
    if parts[:1] == ("project",):
        return True
    if parts[:1] == ("utils",):
        return relative != Path("utils/mock_portal.py") and "_tmp" not in parts
    return relative.as_posix() in {
        "database/schema.sql",
        "database/config.example.yml",
        "README.md",
        "LICENSE",
    }


def repository_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        Path(raw.decode("utf-8", errors="surrogateescape"))
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def scan_file(root: Path, relative: Path, *, fixture: bool = False) -> list[str]:
    name = relative.name.lower()
    if name in SENSITIVE_NAMES or name.endswith(SENSITIVE_SUFFIXES):
        return ["敏感文件名"]
    path = root / relative
    if not path.is_file():
        return ["文件无法读取"]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["文件无法读取"]
    rules = []
    if ACCOUNT_NUMBER.search(text):
        rules.append("疑似学号")
    if PHONE_NUMBER.search(text):
        rules.append("疑似手机号")
    if PRIVATE_PORTAL.search(text):
        rules.append("校园内网地址")
    if not fixture and (
        QUOTED_CREDENTIAL.search(text)
        or ENV_CREDENTIAL.search(text)
        or CHINESE_CREDENTIAL.search(text)
    ):
        rules.append("疑似明文凭据")
    return rules


def scan(root: Path, files: list[Path]) -> list[tuple[Path, str]]:
    findings = []
    for relative in files:
        # mock 门户使用固定夹具口令；豁免这一文件的口令匹配，不豁免真实号码。
        fixture = relative.as_posix() == "utils/mock_portal.py"
        for rule in scan_file(root, relative, fixture=fixture):
            findings.append((relative, rule))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--repo", type=Path, help="检查当前仓库中拟分发的文件")
    source.add_argument("--root", type=Path, help="检查已收集的分发目录")
    args = parser.parse_args()
    root = (args.repo or args.root).resolve()
    if args.repo:
        files = [
            item for item in repository_files(root) if is_distribution_source(item)
        ]
    else:
        files = [item.relative_to(root) for item in root.rglob("*") if item.is_file()]
    findings = scan(root, files)
    for relative, rule in findings:
        print(f"::error file={relative.as_posix()}::{rule}")
    if findings:
        print(f"发现 {len(findings)} 项疑似敏感内容；仅报告位置，不打印原文")
        return 1
    print(f"已检查 {len(files)} 个拟分发文件，未命中已配置规则")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
