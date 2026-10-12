"""API 契约：与真实路由表保持一致，且版本化路径可用。"""

from __future__ import annotations

from campusnet import openapi
from campusnet import server as server_mod
from campusnet.monitor import MonitorManager


def _routes(store) -> set[tuple[str, str]]:  # noqa: ANN001
    handler = server_mod.build_handler(store, MonitorManager())
    return set(handler.routes)


def test_spec_covers_every_route(store) -> None:  # noqa: ANN001
    routes = _routes(store)

    spec = openapi.build(routes)

    assert spec["openapi"].startswith("3.1")
    assert spec["info"]["version"]
    for method, path in routes:
        entry = spec["paths"].get(openapi.versioned(path))
        assert entry is not None, f"契约里缺少 {path}"
        assert method.lower() in entry, f"契约里缺少 {method} {path}"
    # 契约里也不能有路由表之外的路径：手写漂移会被这条抓住
    assert set(spec["paths"]) == {openapi.versioned(path) for _, path in routes}


def test_spec_is_json_serializable(store) -> None:  # noqa: ANN001
    import json

    text = json.dumps(openapi.build(_routes(store)), ensure_ascii=False)

    assert "/api/v1/accounts" in text
    assert "/api/v1/power/{action}" in text


def test_versioned_keeps_non_api_paths() -> None:
    assert openapi.versioned("/healthz") == "/healthz"
    assert openapi.versioned("/api/health") == "/api/v1/health"
    assert openapi.versioned("/api/accounts/{id}/login") == "/api/v1/accounts/{id}/login"


def test_every_operation_has_a_summary(store) -> None:  # noqa: ANN001
    spec = openapi.build(_routes(store))

    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            assert operation["summary"], f"{method} {path} 没有说明"
