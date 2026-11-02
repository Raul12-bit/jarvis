"""Что сейчас играет и как это остановить: системные медиасессии Windows.

Мультимедийная клавиша только переключает состояние: если видео уже на паузе, она его запустит.
Сессии дают явную паузу, а ещё честный ответ, идёт ли видео на самом деле.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import winapi

log = logging.getLogger("jarvis.media")

_PLAYING = 4
_BROWSER_APPS = ("msedge", "chrome", "firefox", "browser", "opera", "brave", "vivaldi", "308046b0af4a39cb")
_PLAYER_APPS = ("vlc", "mpc-hc", "potplayer", "wmplayer", "zunevideo", "microsoft.media.player", "mpv", "kmplayer", "smplayer")
_MEDIA_KEY_PLAY_PAUSE = 0xB3
_MEDIA_KEY_NEXT = 0xB0
_MEDIA_KEY_PREVIOUS = 0xB1


@dataclass(frozen=True)
class NowPlaying:
    app: str
    title: str
    artist: str
    playing: bool

    @property
    def is_video_source(self) -> bool:
        app = self.app.lower()
        return any(name in app for name in _BROWSER_APPS + _PLAYER_APPS)


class MediaSessions:
    def __init__(self) -> None:
        self._manager: Any = None
        self._broken = False

    @property
    def available(self) -> bool:
        """Работают ли сами сессии. Пустой список при этом значит, что и правда ничего не играет."""
        return not self._broken

    async def _sessions(self) -> list[Any]:
        if self._broken:
            return []
        try:
            if self._manager is None:
                from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as Manager

                self._manager = await Manager.request_async()
            return list(self._manager.get_sessions())
        except Exception as exc:  # noqa: BLE001 — без сессий остаются обычные мультимедийные клавиши
            log.info("Медиасессии недоступны: %s", type(exc).__name__)
            self._broken = True
            return []

    async def current(self) -> list[NowPlaying]:
        result = []
        for session in await self._sessions():
            try:
                playing = int(session.get_playback_info().playback_status) == _PLAYING
                title = artist = ""
                try:
                    props = await session.try_get_media_properties_async()
                    title, artist = props.title or "", props.artist or ""
                except Exception as exc:  # noqa: BLE001 — название не главное, статус важнее
                    log.debug("Нет сведений о записи: %s", type(exc).__name__)
                result.append(NowPlaying(session.source_app_user_model_id or "", title, artist, playing))
            except Exception as exc:  # noqa: BLE001 — сессия могла закрыться прямо сейчас
                log.debug("Сессия недоступна: %s", type(exc).__name__)
        return result

    async def is_video_playing(self) -> bool | None:
        """Идёт ли видео в браузере или плеере. None значит, что узнать нельзя."""
        if self._broken:
            return None
        sessions = await self.current()
        if self._broken:
            return None
        return any(item.playing and item.is_video_source for item in sessions)

    async def pause_all(self, only_video: bool = False) -> int:
        """Явная пауза. Возвращает, сколько источников остановлено."""
        count = 0
        for session in await self._sessions():
            try:
                if int(session.get_playback_info().playback_status) != _PLAYING:
                    continue
                app = (session.source_app_user_model_id or "").lower()
                if only_video and not any(name in app for name in _BROWSER_APPS + _PLAYER_APPS):
                    continue
                if await session.try_pause_async():
                    count += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("Пауза не сработала: %s", type(exc).__name__)
        return count

    async def resume(self) -> bool:
        sessions = await self._sessions()
        for session in sessions:
            try:
                if int(session.get_playback_info().playback_status) == _PLAYING:
                    return True
            except Exception as exc:  # noqa: BLE001
                log.debug("Сессия недоступна: %s", type(exc).__name__)
        current = self._manager.get_current_session() if sessions else None
        if current is None:
            return False
        try:
            return bool(await current.try_play_async())
        except Exception as exc:  # noqa: BLE001
            log.debug("Запуск не сработал: %s", type(exc).__name__)
            return False

    async def skip(self, forward: bool) -> bool:
        current = self._manager.get_current_session() if await self._sessions() else None
        if current is None:
            winapi.send_combo([_MEDIA_KEY_NEXT if forward else _MEDIA_KEY_PREVIOUS])
            return True
        try:
            return bool(await (current.try_skip_next_async() if forward else current.try_skip_previous_async()))
        except Exception as exc:  # noqa: BLE001
            log.debug("Перемотка не сработала: %s", type(exc).__name__)
            return False

    async def toggle_with_key(self) -> None:
        winapi.send_combo([_MEDIA_KEY_PLAY_PAUSE])
