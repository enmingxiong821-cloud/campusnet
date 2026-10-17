"""门户适配规则：把「一所学校 = 一段代码」变成「一所学校 = 一条数据」。

规则只允许声明式数据：匹配特征（URL / 正文）、建议的登录模式与密码编码、
表单字段候选、成功与失败标志。**规则里不能有代码**——否则「从远端更新规则」
就等于任意代码执行，这是这类设计最容易踩的坑。

内置规则放在本目录的 profiles/ 下，一个厂商一个文件，按文件名顺序决定优先级
（数字前缀就是顺序）。用户可以用 CAMPUSNET_PROFILES_DIR 指向自己的目录，
同 id 的规则会覆盖内置规则。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

BUILTIN_DIR = Path(__file__).with_name("profiles")
USER_DIR_ENV = "CAMPUSNET_PROFILES_DIR"


class ProfileError(ValueError):
    """规则文件不合法。"""


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    match_url: tuple[str, ...] = ()
    match_body: tuple[str, ...] = ()
    login_mode: str = "browser"
    password_mode: str = "plain"
    username_fields: tuple[str, ...] = ()
    password_fields: tuple[str, ...] = ()
    success_markers: tuple[str, ...] = ()
    failure_markers: tuple[str, ...] = ()
    note: str = ""
    source: str = "builtin"
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def tokens(self) -> tuple[str, ...]:
        """用于旧调用方的特征串合集。"""
        return self.match_url + self.match_body

    @property
    def all_tokens(self) -> tuple[str, ...]:
        return self.tokens

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, source: str = "builtin") -> "Profile":
        if not isinstance(data, dict):
            raise ProfileError("规则必须是 JSON 对象")
        identifier = str(data.get("id", "")).strip()
        name = str(data.get("name", "")).strip()
        if not identifier or not name:
            raise ProfileError("规则必须包含 id 与 name")
        mode = str(data.get("login_mode", "browser")).strip() or "browser"
        if mode not in ("http", "srun", "browser", "manual"):
            raise ProfileError(f"{identifier}: 不支持的 login_mode {mode!r}")
        password_mode = str(data.get("password_mode", "plain")).strip() or "plain"
        if password_mode not in ("plain", "base64", "md5", "md5_base64", "sha256"):
            raise ProfileError(f"{identifier}: 不支持的 password_mode {password_mode!r}")

        def texts(key: str) -> tuple[str, ...]:
            value = data.get(key, [])
            if value is None:
                return ()
            if not isinstance(value, list):
                raise ProfileError(f"{identifier}: {key} 必须是字符串数组")
            return tuple(str(item) for item in value if str(item))

        known = {
            "id",
            "name",
            "match_url",
            "match_body",
            "login_mode",
            "password_mode",
            "username_fields",
            "password_fields",
            "success_markers",
            "failure_markers",
            "note",
        }
        return cls(
            id=identifier,
            name=name,
            match_url=texts("match_url"),
            match_body=texts("match_body"),
            login_mode=mode,
            password_mode=password_mode,
            username_fields=texts("username_fields"),
            password_fields=texts("password_fields"),
            success_markers=texts("success_markers"),
            failure_markers=texts("failure_markers"),
            note=str(data.get("note", "")),
            source=source,
            extra={key: value for key, value in data.items() if key not in known},
        )


def _read_dir(directory: Path) -> list[Profile]:
    if not directory.is_dir():
        return []
    loaded: list[Profile] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"{path.name} 读取失败：{exc}") from exc
        loaded.append(Profile.from_dict(raw, source=path.name))
    return loaded


def user_dir() -> Path | None:
    override = os.environ.get(USER_DIR_ENV, "").strip()
    return Path(override).expanduser() if override else None


def load_profiles(directory: Path | None = None) -> list[Profile]:
    """内置规则 + 用户规则；同 id 时用户规则覆盖内置规则，其余按文件名顺序。"""
    ordered = _read_dir(directory or BUILTIN_DIR)
    extra = user_dir()
    if directory is None and extra is not None:
        try:
            overrides = {item.id: item for item in _read_dir(extra)}
        except ProfileError:
            # 用户自己写的规则坏了，不该让整个程序起不来：discovery 在 import
            # 时就调 load_profiles()，抛在这里等于 CLI、服务和 doctor 全打不开。
            # 内置规则（或显式传入的目录）出错仍然照旧抛——那是打包问题，
            # 测试也按这个语义断言。
            overrides = {}
        if overrides:
            ordered = [overrides.pop(item.id, item) for item in ordered] + list(
                overrides.values()
            )
    return ordered


def identify(
    html_text: str, url: str = "", profiles: Iterable[Profile] | None = None
) -> tuple[Profile | None, str]:
    """按顺序匹配；返回命中的规则与命中的特征串。"""
    body = (html_text or "").lower()
    target = (url or "").lower()
    for profile in profiles if profiles is not None else load_profiles():
        for token in profile.match_url:
            if token.lower() in target:
                return profile, token
        for token in profile.match_body:
            if token.lower() in body:
                return profile, token
    return None, ""


def validate_all(directory: Path | None = None) -> tuple[list[Profile], list[str]]:
    """加载全部规则并返回 (规则, 错误列表)；供 CLI 与测试使用。"""
    errors: list[str] = []
    try:
        items = load_profiles(directory)
    except ProfileError as exc:
        return [], [str(exc)]
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            errors.append(f"规则 id 重复：{item.id}")
        seen.add(item.id)
        if not item.tokens:
            errors.append(f"{item.id}: 没有 match_url / match_body，永远不会命中")
    return items, errors
