"""扫码配对：配对码生成/哈希，以及 pair_codes 存储语义。"""

from __future__ import annotations

from campusnet import pair
from campusnet.storage import Store


def test_generate_code_shape() -> None:
    code = pair.generate_code()

    assert len(code) == pair.CODE_LENGTH
    assert all(ch in pair.CROCKFORD for ch in code)


def test_generate_code_unique() -> None:
    codes = {pair.generate_code() for _ in range(200)}

    assert len(codes) == 200


def test_code_hash_deterministic() -> None:
    assert pair.code_hash("ABC123") == pair.code_hash("ABC123")
    assert pair.code_hash("ABC123") != pair.code_hash("ABC124")


def test_pair_code_lifecycle(store: Store) -> None:
    chash = pair.code_hash("ABC123")
    store.add_pair_code(chash, "2099-01-01 00:00:00")

    rec = store.get_pair_code(chash)
    assert rec is not None
    assert rec["failures"] == 0
    assert rec["used_at"] is None

    store.mark_pair_code_used(chash, "test-ua")
    assert store.get_pair_code(chash)["used_at"] is not None
    assert store.get_pair_code(chash)["user_agent"] == "test-ua"


def test_pair_code_failure_and_purge(store: Store) -> None:
    chash = pair.code_hash("ABC123")
    store.add_pair_code(chash, "2000-01-01 00:00:00")  # 已过期

    assert store.record_pair_failure(chash) == 1
    assert store.record_pair_failure(chash) == 2

    assert store.purge_pair_codes() >= 1
    assert store.get_pair_code(chash) is None


def test_mark_pair_code_used_is_single_use(store: Store) -> None:
    """同码只能被领走一次：第二次领取必须失败，否则「单次使用」是句空话。"""
    chash = pair.code_hash("XYZ789")
    store.add_pair_code(chash, "2099-01-01 00:00:00")

    assert store.mark_pair_code_used(chash, "ua-1") is True
    assert store.mark_pair_code_used(chash, "ua-2") is False
    assert store.get_pair_code(chash)["user_agent"] == "ua-1"
