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
        ("http://172.26.8.11/", "校园内网地址"),
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


def test_distribution_source_excludes_tests_and_documents():
    assert scanner.is_distribution_source(Path("project/backend/campusnet/probe.py"))
    assert not scanner.is_distribution_source(
        Path("project/backend/tests/test_probe.py")
    )
    assert not scanner.is_distribution_source(Path("doc/自测指南.md"))
    assert not scanner.is_distribution_source(Path("utils/mock_portal.py"))
