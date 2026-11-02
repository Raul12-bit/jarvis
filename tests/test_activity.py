"""Контроль времени на подставных окнах: настоящие игры закрывать в тестах нельзя."""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from jarvis import activity, winapi
from jarvis.activity import ActivityTracker, classify, human_minutes, rest_advice
from jarvis.config import ConfigStore
from jarvis.memory import Memory
from jarvis.winapi import WindowInfo


def win(exe: str, title: str = "Окно", path: str | None = None, hwnd: int = 7) -> WindowInfo:
    return WindowInfo(hwnd, title, "X", 1, exe, path or f"C:\\Apps\\{exe}", False, False, (0, 0, 800, 600))


@pytest.mark.parametrize(
    ("window", "kind"),
    [
        (win("cs2.exe", "Counter-Strike 2"), "game"),
        (win("Game.exe", "Какая-то игра", "D:\\SteamLibrary\\steamapps\\common\\Game\\Game.exe"), "game"),
        (win("acs.exe", "Assassin's Creed", "C:\\Program Files (x86)\\Ubisoft\\Ubisoft Game Launcher\\games\\AC\\acs.exe"), "game"),
        (win("javaw.exe", "Minecraft 1.21"), "game"),
        (win("javaw.exe", "Какая-то программа на Java"), "other"),
        (win("steam.exe", "Steam"), "other"),
        (win("upc.exe", "Ubisoft Connect"), "other"),
        (win("vlc.exe", "Фильм - VLC"), "video"),
        (win("msedge.exe", "Прогулка по Алматы - YouTube и еще 1 страница — Личный: Microsoft Edge"), "video"),
        (win("chrome.exe", "Сериал, 3 сезон 5 серия смотреть онлайн - Google Chrome"), "video"),
        (win("msedge.exe", "YouTube и еще 1 страница — Личный: Microsoft Edge"), "other"),
        (win("msedge.exe", "Новости - Microsoft Edge"), "other"),
        (win("chrome.exe", "owner/repo: GitHub - Google Chrome"), "work"),
        (win("code.exe", "main.py - Visual Studio Code"), "work"),
        (win("winword.exe", "Отчёт.docx"), "work"),
        (win("telegram.exe", "Telegram"), "other"),
    ],
)
def test_classify(window: WindowInfo, kind: str) -> None:
    assert classify(window) == kind


def test_classify_nothing() -> None:
    assert classify(None) == "other"


def test_custom_rules_win() -> None:
    assert classify(win("code.exe", "Игра"), {"code": "game"}) == "game"
    assert classify(win("msedge.exe", "Лекция - YouTube"), {"youtube": "work"}) == "work"


def test_fullscreen_unknown_app_counts_as_game() -> None:
    assert classify(win("newgame.exe", "Новая игра"), fullscreen=True) == "game"
    assert classify(win("newgame.exe", "Новая игра"), fullscreen=False) == "other"
    # Системные полноэкранные окна игрой не считаем.
    system = win("explorer.exe", "Проводник", "C:\\Windows\\explorer.exe")
    assert classify(system, fullscreen=True) == "other"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(60, "1 минуту"), (120, "2 минуты"), (300, "5 минут"), (660, "11 минут"), (3600, "1 час"), (5400, "полтора часа"),
     (7200, "2 часа"), (7500, "2 часа 5 минут"), (4920, "1 час 22 минуты")],
)
def test_human_minutes(seconds: int, text: str) -> None:
    assert human_minutes(seconds) == text


def test_rest_advice_depends_on_time() -> None:
    assert "спать" in rest_advice(datetime(2026, 1, 1, 23, 30))
    assert "спать" in rest_advice(datetime(2026, 1, 1, 2, 0))
    assert "поработать" in rest_advice(datetime(2026, 1, 1, 15, 0))
    assert "отдохнуть" in rest_advice(datetime(2026, 1, 1, 21, 0))


class FakeMedia:
    def __init__(self) -> None:
        self.playing: bool | None = True
        self.paused = 0
        self.result = 1

    async def is_video_playing(self) -> bool | None:
        return self.playing

    async def pause_all(self, only_video: bool = False) -> int:
        self.paused += 1
        return self.result


class Rig:
    """Часы подводим вручную: час игры проходит за миллисекунды."""

    def __init__(self, config: ConfigStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.memory = Memory(tmp_path / "t.db")
        self.media = FakeMedia()
        self.said: list[str] = []
        self.updates: list[int] = []
        self.window: WindowInfo | None = win("cs2.exe", "Counter-Strike 2")
        self.closed: list[int] = []
        self.alive = True
        self.idle = 1.0
        self.locked = False
        self.tracker = ActivityTracker(
            config, self.memory, self.media,  # type: ignore[arg-type]
            lambda w: w.title == "Джарвис", self.said.append, lambda: self.updates.append(1),
        )
        self.now = 0.0
        self.tracker._last_tick = 0.0  # noqa: SLF001
        self.tracker._last_flush = 0.0  # noqa: SLF001
        monkeypatch.setattr(winapi, "foreground", lambda: self.window)
        monkeypatch.setattr(winapi, "window_exists", lambda h: self.alive)
        monkeypatch.setattr(winapi, "close_window", lambda h: self.closed.append(h) or True)
        monkeypatch.setattr(winapi, "fills_monitor", lambda w: False)
        monkeypatch.setattr(winapi, "idle_seconds", lambda: self.idle)
        monkeypatch.setattr(winapi, "session_locked", lambda: self.locked)

    def advance(self, seconds: float) -> None:
        async def go() -> None:
            end = self.now + seconds
            while self.now < end:
                self.now += activity.TICK_SECONDS
                await self.tracker.tick(self.now)

        asyncio.run(go())


@pytest.fixture
def rig(config: ConfigStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    config.update(limit_game_minutes=60, limit_video_minutes=90, limit_work_minutes=90)
    return Rig(config, tmp_path, monkeypatch)


def test_game_warning_then_close(rig: Rig) -> None:
    rig.advance(54 * 60)
    assert rig.said == []
    rig.advance(90)
    assert len(rig.said) == 1 and "Сохраните игру" in rig.said[0]
    rig.advance(4.5 * 60 + 10)
    assert len(rig.said) == 2 and "Через минуту закрою" in rig.said[1]
    assert rig.closed == []
    rig.advance(45)
    assert rig.closed == []
    rig.advance(20)
    assert rig.closed == [rig.window.hwnd]  # type: ignore[union-attr]
    rig.alive = False
    rig.advance(10)
    assert "Игра закрыта" in rig.said[-1]


def test_game_that_refuses_to_close_is_reported(rig: Rig) -> None:
    rig.advance(60 * 60 + 90)
    assert rig.closed
    rig.advance(activity.CLOSE_CHECK_AFTER + 10)
    assert "не закрывается" in rig.said[-1]
    # Убивать процесс Джарвиса не пытается: закрытие только одно.
    assert len(rig.closed) == 1


def test_game_warn_only_setting(rig: Rig, config: ConfigStore) -> None:
    config.update(game_action="warn")
    rig.advance(60 * 60 + 200)
    assert rig.closed == []
    assert any("это ваш лимит" in said for said in rig.said)


def test_extension_postpones_closing(rig: Rig) -> None:
    rig.advance(60 * 60 + 10)
    assert rig.tracker.close_plan is not None
    assert rig.tracker.extend(15).startswith("Хорошо")
    assert rig.tracker.close_plan is None
    rig.advance(5 * 60)
    assert rig.closed == []
    rig.advance(5 * 60)
    assert any("Сохраните игру" in said for said in rig.said[2:])
    rig.advance(6 * 60)
    assert rig.tracker.close_plan is not None or rig.closed


def test_extension_without_limit_does_nothing(rig: Rig) -> None:
    rig.advance(60)
    assert "продлевать нечего" in rig.tracker.extend(15)


def test_video_is_paused_at_limit_and_again_if_resumed(rig: Rig) -> None:
    rig.window = win("msedge.exe", "Фильм - YouTube — Microsoft Edge")
    rig.advance(86 * 60)
    assert len(rig.said) == 1 and "поставлю на паузу" in rig.said[0]
    assert rig.media.paused == 0
    rig.advance(4 * 60 + 10)
    assert rig.media.paused == 1
    assert "Поставил на паузу" in rig.said[1]
    rig.advance(activity.VIDEO_NAG_EVERY + 5)
    assert rig.media.paused == 2 and "Снова" in rig.said[2]
    rig.advance(activity.VIDEO_NAG_EVERY * 3)
    assert "ваше решение" in rig.said[-1]
    count = rig.media.paused
    rig.advance(600)
    assert rig.media.paused == count  # после трёх попыток Джарвис больше не настаивает


def test_failed_pause_is_admitted(rig: Rig) -> None:
    rig.media.result = 0
    rig.window = win("vlc.exe", "Кино")
    rig.advance(90 * 60 + 10)
    assert any("не получилось" in said for said in rig.said)


def test_paused_video_is_not_counted(rig: Rig) -> None:
    rig.window = win("msedge.exe", "Фильм - YouTube — Microsoft Edge")
    rig.media.playing = False
    rig.advance(120 * 60)
    assert rig.said == [] and rig.tracker.sessions["video"].seconds == 0


def test_idle_stops_the_clock_but_not_for_movies(rig: Rig) -> None:
    rig.idle = activity.IDLE_LIMIT + 10
    rig.advance(600)
    assert rig.tracker.sessions["game"].seconds == 0
    rig.window = win("vlc.exe", "Кино")
    rig.advance(600)
    assert rig.tracker.sessions["video"].seconds >= 590


def test_locked_session_is_not_counted(rig: Rig) -> None:
    rig.locked = True
    rig.advance(600)
    assert rig.tracker.sessions["game"].seconds == 0


def test_long_break_resets_session(rig: Rig) -> None:
    rig.advance(40 * 60)
    rig.window = win("telegram.exe", "Telegram")
    rig.advance(activity.SESSION_GAP + 30)
    assert rig.tracker.sessions["game"].seconds == 0
    rig.window = win("cs2.exe", "Counter-Strike 2")
    rig.advance(25 * 60)
    assert rig.said == []


def test_short_break_keeps_session(rig: Rig) -> None:
    rig.advance(40 * 60)
    rig.window = win("telegram.exe", "Telegram")
    rig.advance(120)
    rig.window = win("cs2.exe", "Counter-Strike 2")
    rig.advance(16 * 60)
    assert rig.said and "Сохраните игру" in rig.said[0]


def test_work_only_gets_a_rare_quiet_question(rig: Rig) -> None:
    rig.window = win("code.exe", "main.py")
    rig.advance(89 * 60)
    assert rig.said == []
    rig.advance(120)
    assert len(rig.said) == 1 and rig.said[0] in activity._CHECK_INS  # noqa: SLF001
    rig.advance(30 * 60)
    assert len(rig.said) == 1  # не чаще раза в час
    rig.advance(activity.WORK_NAG_EVERY)
    assert len(rig.said) == 2
    assert rig.closed == [] and rig.media.paused == 0  # работу Джарвис не трогает


@pytest.mark.parametrize("exe", ["code.exe", "claude.exe", "chrome.exe", "msedge.exe", "firefox.exe", "cursor.exe"])
def test_work_apps_are_protected_even_if_they_look_like_a_game(exe: str) -> None:
    window = win(exe, "Minecraft - лучшая игра")
    assert activity.is_protected(window)
    assert not activity.closable_game(window)
    assert not activity.closable_game(window, {exe.removesuffix(".exe"): "game"})


def test_fullscreen_guess_is_counted_but_never_closed() -> None:
    window = win("newgame.exe", "Новая игра")
    assert classify(window, fullscreen=True) == "game"
    assert not activity.closable_game(window)


def test_known_game_can_be_closed() -> None:
    assert activity.closable_game(win("cs2.exe", "Counter-Strike 2"))
    assert activity.closable_game(win("Game.exe", "Игра", "D:\\SteamLibrary\\steamapps\\common\\Game\\Game.exe"))


def test_limit_zero_turns_rule_off(rig: Rig, config: ConfigStore) -> None:
    config.update(limit_game_minutes=0)
    rig.advance(300 * 60)
    assert rig.said == [] and rig.closed == []


def test_tracking_can_be_disabled(rig: Rig, config: ConfigStore) -> None:
    config.update(track_time=False)
    rig.advance(300 * 60)
    assert rig.said == [] and rig.tracker.sessions["game"].seconds == 0


def test_jarvis_window_keeps_current_activity(rig: Rig) -> None:
    rig.advance(60)
    rig.window = WindowInfo(9, "Джарвис", "Chrome_WidgetWin_1", 2, "msedge.exe", "", False, False, (0, 0, 1, 1))
    rig.advance(60)
    assert rig.tracker.current_kind == "game"
    assert rig.tracker.sessions["game"].seconds >= 115


def test_focus_mode(rig: Rig) -> None:
    rig.window = win("code.exe", "main.py")
    assert "25 минут" in rig.tracker.start_focus(25)
    rig.tracker.focus_started = rig.now
    rig.tracker.focus_until = rig.now + 25 * 60
    rig.tracker.focus_next_nag = rig.now
    rig.advance(60)
    assert rig.said == []
    rig.window = win("msedge.exe", "Смешные коты - YouTube")
    rig.advance(30)
    assert any("Поставил видео на паузу" in said for said in rig.said)
    assert rig.media.paused == 1
    rig.window = win("code.exe", "main.py")
    rig.advance(25 * 60)
    assert "Фокус окончен" in rig.said[-1]
    assert rig.tracker.focus_until == 0


def test_focus_never_closes_games(rig: Rig) -> None:
    rig.tracker.start_focus(10)
    rig.tracker.focus_started = rig.now - 100
    rig.tracker.focus_until = rig.now + 600
    rig.tracker.focus_next_nag = rig.now
    rig.advance(30)
    assert any("игра подождёт" in said for said in rig.said)
    assert rig.closed == []


def test_report_and_persistence(rig: Rig) -> None:
    rig.advance(120)
    rig.window = win("code.exe", "main.py")
    rig.advance(180)
    report: dict[str, Any] = rig.tracker.report()
    assert 110 <= report["totals"]["game"] <= 130
    assert 170 <= report["totals"]["work"] <= 190
    assert report["limits"]["game"] == 60
    assert report["apps"][0]["app"] == "VS Code"
    assert "работа 3 минуты" in rig.tracker.speakable_report()
    assert rig.memory.activity_for(report["day"])


def test_empty_report_is_spoken_kindly(rig: Rig) -> None:
    assert "почти ничего" in rig.tracker.speakable_report()
