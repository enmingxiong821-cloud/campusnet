"""唤醒计划时间表：解析与到点判断（纯函数，不碰系统）。"""

from __future__ import annotations

from datetime import datetime

from campusnet import wakeplan


def test_parse_daily() -> None:
    s = wakeplan.parse_schedule("07:30")

    assert s is not None
    assert s.days is None
    assert s.hour == 7 and s.minute == 30


def test_parse_weekdays() -> None:
    s = wakeplan.parse_schedule("mon-fri 07:30")

    assert s is not None
    assert s.days == frozenset({0, 1, 2, 3, 4})


def test_parse_days_list() -> None:
    s = wakeplan.parse_schedule("mon,wed 07:30")

    assert s is not None
    assert s.days == frozenset({0, 2})


def test_parse_wraparound() -> None:
    s = wakeplan.parse_schedule("fri-mon 07:30")

    assert s is not None
    assert s.days == frozenset({4, 5, 6, 0})


def test_parse_invalid() -> None:
    assert wakeplan.parse_schedule("") is None
    assert wakeplan.parse_schedule("25:00") is None
    assert wakeplan.parse_schedule("mon-fri") is None
    assert wakeplan.parse_schedule("foo 07:30") is None
    assert wakeplan.parse_schedule("07:30 extra") is None


def test_should_fire_before_time() -> None:
    s = wakeplan.parse_schedule("07:30")

    assert s is not None
    assert wakeplan.should_fire(s, datetime(2026, 10, 13, 7, 0), None) is False


def test_should_fire_first_time() -> None:
    s = wakeplan.parse_schedule("07:30")

    assert s is not None
    assert wakeplan.should_fire(s, datetime(2026, 10, 13, 8, 0), None) is True


def test_should_fire_not_again_same_day() -> None:
    s = wakeplan.parse_schedule("07:30")
    now = datetime(2026, 10, 13, 8, 0)
    last = datetime(2026, 10, 13, 7, 31)

    assert s is not None
    assert wakeplan.should_fire(s, now, last) is False


def test_should_fire_again_next_day() -> None:
    s = wakeplan.parse_schedule("07:30")
    now = datetime(2026, 10, 14, 8, 0)
    last = datetime(2026, 10, 13, 7, 31)

    assert s is not None
    assert wakeplan.should_fire(s, now, last) is True


def test_should_fire_skips_off_weekday() -> None:
    # 2026-10-17 是周六
    s = wakeplan.parse_schedule("mon-fri 07:30")

    assert s is not None
    assert wakeplan.should_fire(s, datetime(2026, 10, 17, 8, 0), None) is False


def test_parse_last_run_roundtrip() -> None:
    assert wakeplan.parse_last_run("2026-10-13 07:31:00") == datetime(
        2026, 10, 13, 7, 31
    )
    assert wakeplan.parse_last_run(None) is None
    assert wakeplan.parse_last_run("garbage") is None


def test_send_wol_delegates_to_power(monkeypatch) -> None:  # noqa: ANN001
    from campusnet import power

    calls: list[tuple] = []
    sentinel = object()

    def _wake(mac, broadcast, port):  # noqa: ANN001, ANN202
        calls.append((mac, broadcast, port))
        return sentinel

    monkeypatch.setattr(power, "wake", _wake)

    # 返回值用哨兵而不是那个常量：原来 mock 无脑返回 "255.255.255.255:9"，
    # 再断言它等于同一个字面量，等于拿自己刚设的值验自己，恒真。
    assert wakeplan.send_wol("AA:BB:CC:DD:EE:FF", "10.0.0.255", 7) is sentinel
    # 三个参数都要记：原来只记 mac 与 port，broadcast 传丢或串位也照样绿。
    assert calls == [("AA:BB:CC:DD:EE:FF", "10.0.0.255", 7)]


def test_should_fire_not_again_when_clock_rolled_back() -> None:
    """今天已经发过，就算时钟被回拨到触发点之前，当天也不该再发一次。"""
    s = wakeplan.parse_schedule("07:30")
    now = datetime(2026, 10, 13, 8, 0)
    last = datetime(2026, 10, 13, 7, 0)

    assert s is not None
    assert wakeplan.should_fire(s, now, last) is False
