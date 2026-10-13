"""API 契约：从真实路由表生成 OpenAPI 3.1 描述。

为什么从路由表生成而不是手写：手写的契约一定会漂移。这里让「契约」只是路由表
的一个视图（`/api/accounts` → `/api/v1/accounts`），再用测试锁住两边一致——
加了路由却忘了更新契约，或者契约里写了不存在的路径，测试会直接报红。

`/api/...` 依旧可用（老前端与扩展不受影响），`/api/v1/...` 是版本化别名，
给将来的移动端用：接口要改时可以在 v2 并存，而不是把现有客户端全弄坏。
"""

from __future__ import annotations

from typing import Any, Iterable

from . import __version__

PREFIX = "/api/v1"

_JSON = {"application/json": {"schema": {"type": "object"}}}

#: 主要接口的中文说明；没列到的自动回落成「METHOD 路径」。
_SUMMARIES: dict[tuple[str, str], str] = {
    ("GET", "/api/health"): "健康检查（版本与账号数）",
    ("GET", "/healthz"): "存活探针",
    ("GET", "/api/accounts"): "列出账号",
    ("POST", "/api/accounts"): "新增账号",
    ("PUT", "/api/accounts/{id}"): "更新账号",
    ("DELETE", "/api/accounts/{id}"): "删除账号",
    ("GET", "/api/probe"): "探活一次（不改状态）",
    ("GET", "/api/detect-portal"): "从跳转里识别门户地址",
    ("GET", "/api/discover"): "自动发现本网门户与字段",
    ("GET", "/api/progress"): "拉取实时检测进度（增量）",
    ("POST", "/api/accounts/{id}/login"): "手动登录某个账号",
    ("POST", "/api/check"): "立即检测全部启用账号",
    ("GET", "/api/monitor"): "监控运行状态",
    ("POST", "/api/monitor/start"): "启动监控并保存设置",
    ("POST", "/api/monitor/stop"): "停止监控",
    ("GET", "/api/events"): "查看事件日志",
    ("POST", "/api/events/clear"): "清空事件日志",
    ("GET", "/api/settings"): "读取设置",
    ("PUT", "/api/settings"): "写入设置",
    ("GET", "/api/bootstart"): "查看开机自启动状态",
    ("POST", "/api/bootstart/install"): "注册开机自启动",
    ("POST", "/api/bootstart/uninstall"): "移除开机自启动",
    ("GET", "/api/power/info"): "远程开机需要的信息（网卡 MAC 与唤醒命令）",
    ("POST", "/api/power/{action}"): "远程关机或重启（需 confirm）",
    ("GET", "/api/doctor/power"): "电源体检（远程开关机前置条件）",
    ("POST", "/api/doctor/power/fix"): "批量修复可自动化的电源体检项（需 confirm）",
    ("GET", "/api/wake-plans"): "列出唤醒计划",
    ("POST", "/api/wake-plans"): "新增唤醒计划",
    ("DELETE", "/api/wake-plans/{id}"): "删除唤醒计划",
    ("GET", "/api/devices"): "列出可唤醒设备",
    ("POST", "/api/devices"): "新增可唤醒设备",
    ("DELETE", "/api/devices/{id}"): "删除可唤醒设备",
    ("POST", "/api/devices/{id}/wake"): "一键唤醒设备",
    ("POST", "/api/pair"): "生成一次性扫码配对码",
    ("POST", "/api/pair/claim"): "凭配对码换取控制台会话",
    ("GET", "/api/auth/status"): "控制台认证状态",
    ("POST", "/api/auth/login"): "用令牌登录控制台",
    ("POST", "/api/auth/logout"): "退出控制台登录",
}


def versioned(path: str) -> str:
    """把 /api/x 映射到 /api/v1/x；非 /api 路径原样保留。"""
    if path.startswith("/api/"):
        return PREFIX + path[len("/api") :]
    return path


def _tag(path: str) -> str:
    parts = [item for item in path.strip("/").split("/") if item]
    if len(parts) >= 2 and parts[0] == "api":
        return parts[2] if len(parts) > 2 else "system"
    return "system"


def build(routes: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """按路由表生成契约；routes 是 (方法, 路径) 的集合。"""
    paths: dict[str, dict[str, Any]] = {}
    for method, path in routes:
        entry = paths.setdefault(versioned(path), {})
        entry[method.lower()] = {
            "summary": _SUMMARIES.get((method, path), f"{method} {path}"),
            "tags": [_tag(path)],
            "responses": {
                "200": {"description": "成功", "content": _JSON},
                "400": {"description": "参数错误"},
                "401": {"description": "需要控制台令牌"},
                "403": {"description": "跨来源请求被拒绝"},
                "500": {"description": "服务端错误"},
            },
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "校园网自连 · 控制台 API",
            "version": __version__,
            "description": (
                "由真实路由表生成。/api/v1/... 是版本化路径；不带 v1 的旧路径仍然可用。"
            ),
        },
        "paths": paths,
    }
