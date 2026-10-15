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
#: 依赖锁文件必须入库（.gitignore 那边也是这个口径，所以只忽略 database/*.lock
#: 而不是 *.lock），可它们的名字又都落在 `.lock` 后缀上。内容是包管理器从公开
#: 仓库生成的包名、版本与哈希，写不进本机的凭据，所以按名字豁免。
DEPENDENCY_LOCKFILES = {
    "pubspec.lock",
    "uv.lock",
    "poetry.lock",
    "pipfile.lock",
    "cargo.lock",
    "gemfile.lock",
    "composer.lock",
}
ACCOUNT_NUMBER = re.compile(r"(?<!\d)20\d{8}(?!\d)")
PHONE_NUMBER = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

#: 本机私有标识清单（不入库）。真实校园内网地址、学校特有的 SSID 之类不该
#: 写进公开仓库，但检查规则又要能覆盖它们，所以改成从这份本地文件里读。
LOCAL_IDENTIFIERS_FILE = "utils/local_sensitive.txt"


def load_local_patterns(root: Path) -> list[re.Pattern[str]]:
    """读本地敏感标识清单，一行一条正则；文件不存在就返回空列表。"""
    path = root / LOCAL_IDENTIFIERS_FILE
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    patterns: list[re.Pattern[str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error:
            continue
    return patterns
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


#: 会进发布包的顶层文件，逐条对应 .github/workflows/release.yml 的收集步骤。
#: 漏掉一个「确实会发出去」的文件，等于给它开一个绕过敏感串检查的后门——
#: 原来这里只有 README/LICENSE/database 两条，install.sh、packaging/、
#: CHANGELOG.md 这些全都不在扫描范围内。
DISTRIBUTION_TOP = {
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "install.bat",
    "install.sh",
    "install.ps1",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.tailscale.yml",
    ".dockerignore",
    "database/schema.sql",
    "database/config.example.yml",
}

#: 整目录随包发的顶层目录。
DISTRIBUTION_DIRS = frozenset({"packaging"})

#: doc/ 下只有截图与自测指南会随包发；工作状态存档、实施方案这类内部文档
#: 留在仓库里，把它们拉进扫描范围只是白扫。
DISTRIBUTION_DOC = frozenset({"doc/自测指南.md"})

#: 这几份不套本机私有清单（utils/local_sensitive.txt），其余规则照旧生效。
#: 更新日志按惯例要写「适配了哪个门户」，校名是它绕不开的正文，而且早已随公开
#: git 历史与历次发布包公开——清单里那条裸校名正则对它永久报警、又永远修不掉。
#: 一条修不掉的告警最后只会被绕过，比不检查更糟。明文凭据、学号、手机号在这
#: 份文件上仍然照查。
LOCAL_PATTERN_EXEMPT = frozenset({"CHANGELOG.md"})


def is_distribution_source(relative: Path) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if parts[0] == "project":
        return not (parts[:2] == ("project", "backend") and "tests" in parts)
    if parts[0] == "utils":
        # _tmp / tmp 是抓包与验证工作区，release.yml 打包前会把它们删掉（那边写着
        # 原因：里面是从门户抓下来的第三方 JS 与明文账号口令，cp -r 不受 .gitignore
        # 约束）。它们既然不进包，就不该拿敏感串规则去卡本地抓包产物。
        if "_tmp" in parts or "tmp" in parts:
            return False
        return relative.as_posix() != "utils/mock_portal.py"
    if parts[0] in DISTRIBUTION_DIRS:
        return True
    posix = relative.as_posix()
    return (
        posix in DISTRIBUTION_TOP
        or posix in DISTRIBUTION_DOC
        or parts[:2] == ("doc", "images")
    )


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


def scan_file(
    root: Path,
    relative: Path,
    *,
    fixture: bool = False,
    local: list[re.Pattern[str]] | None = None,
) -> list[str]:
    name = relative.name.lower()
    if name in SENSITIVE_NAMES:
        return ["敏感文件名"]
    if name.endswith(SENSITIVE_SUFFIXES) and name not in DEPENDENCY_LOCKFILES:
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
    for index, pattern in enumerate(local or [], start=1):
        # 只报编号不报内容：清单里的值本身就是敏感的，不能进 CI 日志。
        if pattern.search(text):
            rules.append(f"本地敏感标识 #{index}")
    if not fixture and (
        QUOTED_CREDENTIAL.search(text)
        or ENV_CREDENTIAL.search(text)
        or CHINESE_CREDENTIAL.search(text)
    ):
        rules.append("疑似明文凭据")
    return rules


def scan(root: Path, files: list[Path]) -> list[tuple[Path, str]]:
    findings = []
    patterns = load_local_patterns(root)
    for relative in files:
        posix = relative.as_posix()
        # mock 门户使用固定夹具口令；豁免这一文件的口令匹配，不豁免真实号码。
        fixture = posix == "utils/mock_portal.py"
        local = None if posix in LOCAL_PATTERN_EXEMPT else patterns
        for rule in scan_file(root, relative, fixture=fixture, local=local):
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
