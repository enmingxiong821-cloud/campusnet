"""发布内容检查器使用合成样例，不能引入真实账号或密钥。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "distribution_check", ROOT / "utils/check_distribution.py"
)
assert SPEC is not None and SPEC.loader is not None
scanner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scanner)


@pytest.mark.parametrize(
    "content,rule",
    [
        ('password="example-secret-value"', "疑似明文凭据"),
        ("TOKEN=example_test_token", "疑似明文凭据"),
        ('密码: "realpass"', "疑似明文凭据"),
        ('口令："example-secret"', "疑似明文凭据"),
        ("令牌='example-token'", "疑似明文凭据"),
        ("student=2012345678", "疑似学号"),
        ("phone=13800000000", "疑似手机号"),
    ],
)
def test_distribution_scan_reports_synthetic_values(tmp_path, content, rule):
    relative = Path("example.py")
    (tmp_path / relative).write_text(content, encoding="utf-8")
    assert rule in scanner.scan_file(tmp_path, relative)


def test_distribution_scan_ignores_nonliteral_code(tmp_path):
    relative = Path("module.py")
    (tmp_path / relative).write_text(
        "password=args.password\npassword=crypto.encode_password(value)\n",
        encoding="utf-8",
    )
    assert scanner.scan_file(tmp_path, relative) == []


def test_local_identifiers_come_from_private_list(tmp_path):
    """真实内网地址这类标识走本地清单，公开仓库里不留硬编码。"""
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "local_sensitive.txt").write_text(
        "# 注释行\n172\\.26\\.1\\.1\n", encoding="utf-8"
    )
    relative = Path("example.py")
    (tmp_path / relative).write_text(
        "portal=http://172.26.1.1/eportal\n", encoding="utf-8"
    )

    patterns = scanner.load_local_patterns(tmp_path)
    findings = scanner.scan_file(tmp_path, relative, local=patterns)

    assert patterns
    assert findings == ["本地敏感标识 #1"]


def test_missing_local_list_is_not_an_error(tmp_path):
    assert scanner.load_local_patterns(tmp_path) == []


def test_distribution_scan_rejects_sensitive_filename(tmp_path):
    assert scanner.scan_file(tmp_path, Path("database/private.db")) == ["敏感文件名"]
    assert scanner.scan_file(tmp_path, Path("database/campusnet.lock")) == [
        "敏感文件名"
    ]


def test_fixture_exemption_does_not_exempt_phone_number(tmp_path):
    relative = Path("utils/mock_portal.py")
    (tmp_path / relative.parent).mkdir()
    (tmp_path / relative).write_text(
        'password="example-secret-value"\nphone=13800000000', encoding="utf-8"
    )
    assert scanner.scan_file(tmp_path, relative, fixture=True) == ["疑似手机号"]


def test_distribution_source_covers_every_shipped_file():
    """扫描面必须与发布流水线实际收进包的文件对齐。

    这里原来断言的是「`doc/自测指南.md` 不在扫描面内」——而 release.yml 明确把它
    `cp` 进了包。漏掉一个确实会发出去的文件，等于给它开一个绕过敏感串检查的后门：
    install.sh、packaging/、更新日志这些当时全在门外面。
    """
    kept = [
        "project/backend/campusnet/probe.py",
        "install.sh",
        "install.ps1",
        "install.bat",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.tailscale.yml",
        "packaging/campusnet.spec",
        "doc/自测指南.md",
        "doc/images/console.png",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "database/schema.sql",
    ]
    for relative in kept:
        assert scanner.is_distribution_source(Path(relative)), relative


def test_distribution_source_excludes_repo_only_files():
    """留在仓库里、不进包的东西不要白扫一遍（内部文档还带本机绝对路径）。"""
    skipped = [
        "project/backend/tests/test_probe.py",
        "utils/mock_portal.py",
        "utils/_tmp/portal.js",
        "utils/tmp/capture.py",
        "doc/工作状态存档.md",
        "doc/实施方案.md",
        "doc/后续步骤.md",
        ".github/workflows/ci.yml",
        "CHANGELOG.md.bak",
    ]
    for relative in skipped:
        assert not scanner.is_distribution_source(Path(relative)), relative


def test_private_list_skipped_for_changelog_but_other_rules_still_apply(tmp_path):
    """更新日志不套本机私有清单，明文凭据那几条照旧生效。

    更新日志按惯例要写「适配了哪个门户」，校名绕不开；而它早已随公开 git 历史与历次
    发布包出去，私有清单里那条裸校名正则对它永久报警又永远修不掉。豁免只限这一层。
    """
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "local_sensitive.txt").write_text(
        "某某大学\n", encoding="utf-8"
    )
    body = '适配某某大学门户\npassword="example-secret-value"\n'
    changelog = Path("CHANGELOG.md")
    (tmp_path / changelog).write_text(body, encoding="utf-8")
    assert [rule for _, rule in scanner.scan(tmp_path, [changelog])] == ["疑似明文凭据"]

    # 同样的正文放进没有豁免的文件里，两条规则都要报出来。
    readme = Path("README.md")
    (tmp_path / readme).write_text(body, encoding="utf-8")
    rules = [rule for _, rule in scanner.scan(tmp_path, [readme])]
    assert rules == ["本地敏感标识 #1", "疑似明文凭据"]
