"""Джарвис сам замечает то, что человек пропустил бы: садится батарея, кончается диск, пропал интернет, человек вернулся.

Говорит редко и только по делу: каждое предупреждение звучит один раз, пока ситуация не сменится. На этом ноутбуке
память почти всегда занята на 90 %, поэтому порог по ней высокий, иначе Джарвис ворчал бы постоянно.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .system import Status, human_size

BATTERY_WARN = 20
BATTERY_CRITICAL = 10
DISK_WARN = 3 * 2**30
MEMORY_WARN = 0.97
MEMORY_REPEAT = 1800.0
RETURN_AFTER = 1200.0
OFFLINE_REPORT = 180.0


@dataclass
class Notice:
    text: str
    urgent: bool = False


@dataclass
class WatcherState:
    battery_warned: set[int] = field(default_factory=set)
    disk_warned: bool = False
    memory_high: int = 0
    memory_said_at: float = -1e9
    offline_since: float | None = None
    longest_idle: float = 0.0
    locked: bool | None = None
    locked_at: float = 0.0
    plugged: bool | None = None
    last_tick: float | None = None


def check_status(state: WatcherState, status: Status, now: float, heaviest: str = "") -> list[Notice]:
    """Что сказать по этой сводке. Без времени и окон, поэтому проверяется тестами."""
    notices: list[Notice] = []
    if status.battery is not None:
        if status.plugged or status.battery > BATTERY_WARN + 3:
            state.battery_warned.clear()
        if not status.plugged:
            if status.battery <= BATTERY_CRITICAL and BATTERY_CRITICAL not in state.battery_warned:
                state.battery_warned.update({BATTERY_WARN, BATTERY_CRITICAL})
                notices.append(Notice(f"Батарея почти села: {status.battery:.0f} процентов. Подключите зарядку, сэр, иначе ноутбук скоро выключится.", True))
            elif status.battery <= BATTERY_WARN and BATTERY_WARN not in state.battery_warned:
                state.battery_warned.add(BATTERY_WARN)
                notices.append(Notice(f"Батарея {status.battery:.0f} процентов, сэр, пора подумать о зарядке."))
    if status.disk_free < DISK_WARN and not state.disk_warned:
        state.disk_warned = True
        notices.append(Notice(f"На системном диске осталось {human_size(status.disk_free)}. Скоро может не хватить места."))
    elif status.disk_free > DISK_WARN * 2:
        state.disk_warned = False
    if status.ram_total and status.ram_used / status.ram_total >= MEMORY_WARN:
        state.memory_high += 1
        if state.memory_high >= 3 and now - state.memory_said_at >= MEMORY_REPEAT:
            state.memory_said_at = now
            who = f" Больше всего занимает {heaviest}." if heaviest else ""
            notices.append(Notice(f"Память почти закончилась, ноутбук может начать тормозить.{who}"))
    else:
        state.memory_high = 0
    if not status.internet:
        if state.offline_since is None:
            state.offline_since = now
    elif state.offline_since is not None:
        if now - state.offline_since >= OFFLINE_REPORT:
            notices.append(Notice("Интернет снова работает."))
        state.offline_since = None
    return notices


_LONG_LOCK = 60.0  # минута: случайное блокирование и быстрый вход назад не стоят сообщения на телефон
_SLEEP_GAP = 30.0  # цикл идёт раз в пять секунд: такая пауза значит, что ноутбук спал


def check_presence(state: WatcherState, locked: bool, plugged: bool, has_battery: bool, now: float) -> list[str]:
    """Что сообщить хозяину на телефон: кто-то вошёл, ноутбук проснулся, выдернули зарядку. Без окон, поэтому проверяется тестами."""
    alerts: list[str] = []
    if state.last_tick is not None and now - state.last_tick >= _SLEEP_GAP:
        alerts.append("Ноутбук проснулся, сэр.")
    state.last_tick = now
    if state.locked is not None and locked != state.locked:
        if locked:
            state.locked_at = now
        elif now - state.locked_at >= _LONG_LOCK:
            alerts.append("Сэр, ноутбук только что разблокировали.")
    state.locked = locked
    if has_battery:
        if state.plugged is True and not plugged:
            alerts.append("Сэр, зарядку отключили: ноутбук работает от батареи.")
        state.plugged = plugged
    return alerts


def came_back(state: WatcherState, idle_seconds: float) -> bool:
    """Человек вернулся после долгого перерыва: талисман его поприветствует."""
    state.longest_idle = max(state.longest_idle, idle_seconds)
    if idle_seconds < 5 and state.longest_idle >= RETURN_AFTER:
        state.longest_idle = 0.0
        return True
    if idle_seconds < 5:
        state.longest_idle = 0.0
    return False
