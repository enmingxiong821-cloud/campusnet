"""一键自检：把「无人值守到底处在什么状态」集中成一次输出。

doctor 只做只读检查（默认再加一次真实探活），不会登录、不会改配置、不会写事件。
用途很具体：机器在宿舍、人在外面，需要一条命令回答「它到底能不能连上、
为什么连不上、日志在哪」。
"""

from __future__ import annotations

import json
import os
import platform
import stat as stat_mod
import sys
from dataclasses import asdict, dataclass
from typing import Any

from . import __version__, bootstart, ports, probe as probe_mod
from .lockfile import SingleInstance, pid_alive
from .paths import (
    API_HOST,
    API_PORT,
    ROOT,
    api_token_path,
    db_path,
    instance_lock_path,
)

OK = "ok"
WARN = "warn"
FAIL = "fail"

_LABEL = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
PROBE_TIMEOUT = 4.0


@dataclass
class Check:
    name: str
    status: str
    detail: str


def _environment_check() -> Check:
    version = ".".join(str(item) for item in sys.version_info[:3])
    if sys.version_info < (3, 10):
        return Check("运行环境", FAIL, f"Python {version} 低于要求的 3.10")
    system = f"{platform.system()} {platform.release()}"
    return Check("运行环境", OK, f"Python {version}（{system}），程序 {__version__}")


def _root_check() -> Check:
    """数据目录要可写：数据库、日志都落在它下面（冻结运行时是用户目录）。"""
    label = "数据目录" if getattr(sys, "frozen", False) else "仓库目录"
    marker = ROOT / ".campusnet-doctor.tmp"
    try:
        marker.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return Check(label, FAIL, f"{ROOT} 不可写：{exc}")
    finally:
        try:
            marker.unlink()
        except OSError:
            pass
    return Check(label, OK, str(ROOT))


def _storage_check() -> tuple[Check, Any | None]:
    from .storage import SCHEMA_VERSION, Store

    path = db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check("数据库", FAIL, f"无法创建目录 {path.parent}：{exc}"), None
    try:
        store = Store(path)
    except Exception as exc:  # noqa: BLE001 - 打不开就如实报，不带崩整个自检
        return Check("数据库", FAIL, f"无法打开 {path}：{type(exc).__name__}: {exc}"), None
    conn = store.connect()
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    detail = f"{path}（结构版本 {version}/{SCHEMA_VERSION}）"
    if version < SCHEMA_VERSION:
        return Check("数据库", WARN, f"{detail}，下次启动会执行迁移"), store
    return Check("数据库", OK, detail), store


def _accounts_check(store: Any) -> Check:
    accounts = store.list_accounts()
    enabled = [item for item in accounts if item.enabled]
    if not accounts:
        return Check("账号", WARN, "还没有账号：先跑 discover 再 add，或在 Web 控制台添加")
    return Check("账号", OK, f"共 {len(accounts)} 个，启用 {len(enabled)} 个")


def _lock_check() -> Check:
    path = instance_lock_path()
    lock = SingleInstance(path)
    holder = lock.holder_pid()
    if lock.is_held_by_other_process():
        return Check("实例锁", WARN, f"已有服务实例在运行（持有者 PID {holder}，{path}）")
    if path.exists():
        # 判定以内核文件锁为准，所以残留文件不影响启动；这里只把原因说清楚。
        if holder and pid_alive(holder):
            return Check(
                "实例锁",
                OK,
                f"锁文件残留，记录的 PID {holder} 仍在运行但没有持有文件锁"
                f"（多半是无关进程，PID 被系统复用了）；不影响启动（{path}）",
            )
        return Check("实例锁", OK, f"锁文件是上次异常退出的残留（{path}），不影响启动")
    return Check("实例锁", OK, f"未被占用（{path}）")


def _token_check() -> Check:
    path = api_token_path()
    if not path.exists():
        return Check("控制台令牌", OK, f"尚未生成（{path}），首次以非回环地址启动时创建")
    if os.name != "posix":
        return Check("控制台令牌", OK, f"已存在（{path}），Windows 权限由 ACL 管理")
    mode = stat_mod.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return Check("控制台令牌", WARN, f"权限过宽 {oct(mode)}（建议 0o600）：{path}")
    return Check("控制台令牌", OK, f"权限 {oct(mode)}：{path}")


def _bootstart_check() -> Check:
    try:
        result = bootstart.status()
    except Exception as exc:  # noqa: BLE001 - 查询失败不该中断自检
        return Check("开机自启动", WARN, f"查询失败：{type(exc).__name__}: {exc}")
    where = f"（{result.path}）" if result.path else ""
    if result.installed and not getattr(result, "effective", True):
        return Check("开机自启动", FAIL, f"已注册但不会生效：{result.detail}{where}")
    status = OK if result.installed else WARN
    return Check("开机自启动", status, f"{result.detail}{where}")


def _port_check(host: str, port: int) -> Check:
    if ports.is_port_free(port, host=host):
        return Check("服务端口", OK, f"{host}:{port} 空闲")
    conflict = ports.describe_conflict({port: "服务"}, host=host)
    return Check("服务端口", WARN, conflict or f"{host}:{port} 已被占用")


def _log_check() -> Check:
    from .logging_setup import log_path

    target = log_path()
    if target.exists():
        return Check("运行日志", OK, str(target))
    return Check("运行日志", OK, f"本次运行会写入 {target}")


def _probe_checks(store: Any) -> list[Check]:
    checks: list[Check] = []
    for account in store.list_accounts(enabled_only=True):
        result = probe_mod.probe_configured(account.probe_url, timeout=PROBE_TIMEOUT)
        detail = f"{result.status} - {result.detail[:80]}"
        checks.append(Check(f"探活 {account.name}", OK if result.online else WARN, detail))
    if not checks:
        checks.append(Check("探活", WARN, "没有启用的账号，跳过"))
    return checks


UPDATE_API = (
    "https://api.github.com/repos/enmingxiong821-cloud/campusnet/releases/latest"
)


def compare_versions(local: str, latest: str) -> str:
    """返回 "same" / "newer" / "older"，只比数字段，避免预发布后缀误判。"""

    def parts(text: str) -> tuple[int, ...]:
        items: list[int] = []
        for chunk in str(text).lstrip("v").split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            items.append(int(digits) if digits else 0)
        return tuple(items)

    left, right = parts(local), parts(latest)
    # 补齐位数再比较：1.1 与 1.1.0 是同一个版本，不该被判成「旧」。
    width = max(len(left), len(right))
    left = left + (0,) * (width - len(left))
    right = right + (0,) * (width - len(right))
    if left == right:
        return "same"
    return "older" if left < right else "newer"


def _update_check(enabled: bool = True) -> Check:
    """对比本地版本与 GitHub 最新 Release。

    联网失败一律当成「没检查」：检查更新只是锦上添花，
    绝不能因为它把 doctor 搞成红叉。
    """
    if not enabled:
        return Check("版本", OK, f"当前 {__version__}（未联网检查更新）")
    import json
    import urllib.request

    try:
        request = urllib.request.Request(
            UPDATE_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "campusnet",
            },
        )
        with urllib.request.urlopen(request, timeout=4) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        latest = str(payload.get("tag_name", "")).strip()
    except Exception:  # noqa: BLE001 - 检查更新失败不影响自检结论
        return Check("版本", OK, f"当前 {__version__}（没能获取最新版本，已忽略）")
    if not latest:
        return Check("版本", OK, f"当前 {__version__}（没能获取最新版本，已忽略）")
    relation = compare_versions(__version__, latest)
    if relation == "older":
        return Check(
            "版本",
            WARN,
            f"有新版本 {latest}（当前 {__version__}，可到 Releases 下载）",
        )
    if relation == "same":
        return Check("版本", OK, f"当前 {__version__}（已是最新）")
    return Check("版本", OK, f"当前 {__version__}（比最新发布还新）")


def _notify_check(store: Any | None) -> Check:
    if store is None:
        return Check("失败通知", WARN, "数据库打不开，无法读取通知配置")
    from .notify import CHANNELS, Notifier

    notifier = Notifier.from_store(store)
    if notifier.enabled:
        label = CHANNELS.get(notifier.channel, notifier.channel)
        return Check(
            "失败通知",
            OK,
            f"已配置 {label}（连续失败 {notifier.after_failures} 次起通知，恢复时也通知）",
        )
    return Check(
        "失败通知",
        WARN,
        "未配置：机器出问题时不会主动通知你"
        "（设置里选渠道填 key，或用 CAMPUSNET_NOTIFY_URL 临时启用）",
    )


def run(
    probe: bool = True, host: str = API_HOST, port: int = API_PORT
) -> list[Check]:
    """执行全部检查；probe=False 时只查本地状态，不发任何网络请求。"""
    checks = [_environment_check(), _root_check()]
    storage, store = _storage_check()
    checks.append(storage)
    if store is not None:
        checks.append(_accounts_check(store))
    checks.extend(
        [
            _lock_check(),
            _token_check(),
            _bootstart_check(),
            _port_check(host, port),
            _log_check(),
            _notify_check(store),
            _update_check(enabled=probe),
        ]
    )
    if probe and store is not None:
        checks.extend(_probe_checks(store))
    return checks


def render(checks: list[Check], *, as_json: bool = False) -> str:
    if as_json:
        return json.dumps([asdict(item) for item in checks], ensure_ascii=False, indent=2)
    return "\n".join(
        f"[{_LABEL.get(item.status, item.status)}] {item.name}: {item.detail}"
        for item in checks
    )


def exit_code(checks: list[Check]) -> int:
    """只有 FAIL 才返回 1；WARN 属于「能用但要注意」，不该让脚本中断。"""
    return 1 if any(item.status == FAIL for item in checks) else 0
