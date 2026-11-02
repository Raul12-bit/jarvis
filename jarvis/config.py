"""Настройки и пути. Всё изменяемое живёт в %LOCALAPPDATA%\\Jarvis, а не в папке проекта:
проект лежит в OneDrive, а ключам API, базе и модели синхронизироваться незачем."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger("jarvis.config")

PROJECT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_DIR / "web"
ASSETS_DIR = PROJECT_DIR / "assets"
DEFAULT_PORT = 47291

# Только мужские голоса: копировать голос актёра нельзя, поэтому берём низкий спокойный тембр и обработку в браузере.
# Замер настоящими запросами из этой сети, 2 ноября 2026, по 20 фраз на голос с паузами, как в разговоре:
#   Реми 20/20, 1,7 с, основной тон 98 Гц; Брайан 20/20, 2,0 с, 131 Гц; Флориан 20/20, 1,9 с, 142 Гц; Дмитрий 1/20, остальное NoAudioReceived.
# Чистота русского по распознаванию Vosk одинаковая: 94–96 %. Поэтому основной самый низкий, а родной русский Дмитрий последний.
# Сдвиг понижает голос прямо в сервисе синтеза, это дешевле и чище, чем обрабатывать звук после.
VOICES: list[dict[str, str]] = [
    {"id": "fr-FR-RemyMultilingualNeural", "label": "Реми — самый низкий, мультиязычный", "pitch": "+0Hz"},
    {"id": "en-US-BrianMultilingualNeural", "label": "Брайан — спокойный, мультиязычный", "pitch": "-8Hz"},
    {"id": "de-DE-FlorianMultilingualNeural", "label": "Флориан — ровный, мультиязычный", "pitch": "-6Hz"},
    {"id": "ru-RU-DmitryNeural", "label": "Дмитрий — родной русский, но сервис отвечает редко", "pitch": "-6Hz"},
]
DEFAULT_VOICE = VOICES[0]["id"]
_VOICE_IDS = {v["id"] for v in VOICES}


def voice_pitch(voice: str) -> str:
    return next((item["pitch"] for item in VOICES if item["id"] == voice), "+0Hz")


def backup_voices(voice: str) -> list[str]:
    """Сервис иногда не отвечает отдельному голосу. Тогда говорит другой мужской голос, а не женский запасной из Windows."""
    return [item["id"] for item in VOICES if item["id"] != voice]


def app_home() -> Path:
    override = os.environ.get("JARVIS_HOME")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Jarvis"


DEFAULT_CITY = "Алматы"
_PROFILE_VERSION = 3


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    user_name: str = Field(default="", max_length=60)
    city: str = Field(default=DEFAULT_CITY, max_length=80)
    voice: str = DEFAULT_VOICE
    voice_fx: bool = True
    speech_rate: int = Field(default=0, ge=-30, le=30)
    wake_word: bool = True
    barge_in: bool = True
    followup_seconds: int = Field(default=12, ge=4, le=60)
    power_saver: bool = False
    mic_device: str = Field(default="", max_length=300)
    groq_api_key: str = Field(default="", max_length=300)
    gemini_api_key: str = Field(default="", max_length=300)
    onboarded: bool = False
    shortcut_created: bool = False
    shortcut_target: str = Field(default="", max_length=600)
    profile_version: int = 0
    allow_screen: bool = True
    track_time: bool = True
    limit_game_minutes: int = Field(default=60, ge=0, le=720)
    limit_video_minutes: int = Field(default=90, ge=0, le=720)
    limit_work_minutes: int = Field(default=90, ge=0, le=720)
    game_action: str = Field(default="close", pattern="^(close|warn)$")
    custom_kinds: dict[str, str] = Field(default_factory=dict)
    autostart: bool = False
    telegram_bot_token: str = Field(default="", max_length=200)
    telegram_owner_id: int = 0
    keep_awake: bool = False
    mascot: bool = True
    mascot_size: str = Field(default="medium", pattern="^(small|medium|large)$")
    mascot_captions: bool = True
    mascot_hide_fullscreen: bool = False
    mascot_x: int | None = None
    mascot_y: int | None = None
    # Хозяин попросил полный доступ, поэтому команды и камера включены сразу. Выключаются только на самом ноутбуке.
    phone_shell: bool = True
    allow_camera: bool = True
    phone_any_path: bool = True
    phone_alerts: bool = True
    phone_pin: str = Field(default="", max_length=32)
    proactive: bool = True
    wake_cloud_check: bool = True

    @field_validator("voice")
    @classmethod
    def _known_voice(cls, value: str) -> str:
        # Голос из старого профиля не повод считать весь файл битым: вместе с ним пропали бы ключи и привязка телефона.
        return value if value in _VOICE_IDS else DEFAULT_VOICE

    @field_validator("user_name", "city")
    @classmethod
    def _strip(cls, value: str) -> str:
        return " ".join(value.split())


class SettingsPatch(BaseModel):
    """Что интерфейсу разрешено менять. Ключи API сюда намеренно не входят."""

    model_config = ConfigDict(extra="forbid")

    user_name: str | None = Field(default=None, max_length=60)
    city: str | None = Field(default=None, max_length=80)
    voice: str | None = None
    voice_fx: bool | None = None
    speech_rate: int | None = Field(default=None, ge=-30, le=30)
    wake_word: bool | None = None
    barge_in: bool | None = None
    followup_seconds: int | None = Field(default=None, ge=4, le=60)
    power_saver: bool | None = None
    mic_device: str | None = Field(default=None, max_length=300)
    onboarded: bool | None = None
    allow_screen: bool | None = None
    track_time: bool | None = None
    limit_game_minutes: int | None = Field(default=None, ge=0, le=720)
    limit_video_minutes: int | None = Field(default=None, ge=0, le=720)
    limit_work_minutes: int | None = Field(default=None, ge=0, le=720)
    game_action: str | None = Field(default=None, pattern="^(close|warn)$")
    autostart: bool | None = None
    keep_awake: bool | None = None
    mascot: bool | None = None
    mascot_size: str | None = Field(default=None, pattern="^(small|medium|large)$")
    mascot_captions: bool | None = None
    mascot_hide_fullscreen: bool | None = None
    phone_shell: bool | None = None
    allow_camera: bool | None = None
    phone_any_path: bool | None = None
    phone_alerts: bool | None = None
    proactive: bool | None = None
    wake_cloud_check: bool | None = None

    @field_validator("voice")
    @classmethod
    def _known_voice(cls, value: str | None) -> str | None:
        # Здесь неизвестный голос честная ошибка: профиль не переезжает, а интерфейс прислал что-то не то.
        if value is not None and value not in _VOICE_IDS:
            raise ValueError("неизвестный голос")
        return value


def mask_key(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) >= 12 else ""


class ConfigStore:
    def __init__(self, home: Path | None = None) -> None:
        self.home = home or app_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "config.json"
        self._lock = threading.Lock()
        self.settings = self._load()
        self.upgraded = False
        self._migrate()

    def _migrate(self) -> None:
        version = self.settings.profile_version
        if version >= _PROFILE_VERSION:
            return
        self.upgraded = self.path.exists()
        changes: dict[str, Any] = {"profile_version": _PROFILE_VERSION}
        if version < 2:
            # Человек попросил считать домом Алматы. Один раз: если потом сменит город в настройках, это останется за ним.
            changes["city"] = DEFAULT_CITY
        if version < 3:
            # Карен стала Джарвисом. Хозяин попросил полный доступ, а талисмана в прошлом профиле могло и не быть.
            changes |= {"phone_shell": True, "allow_camera": True, "mascot": True}
        self.update(**changes)

    def _load(self) -> Settings:
        try:
            return Settings(**json.loads(self.path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return Settings()
        except (ValueError, TypeError) as exc:
            # Битый конфиг не должен мешать запуску, но и молча его затирать нельзя.
            backup = self.path.with_suffix(".broken.json")
            self.path.replace(backup)
            log.warning("config.json повреждён (%s), копия: %s", type(exc).__name__, backup)
            return Settings()

    def _save(self) -> None:
        data = json.dumps(self.settings.model_dump(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.home, prefix="config.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def update(self, **changes: Any) -> Settings:
        with self._lock:
            # None значит «не менять», поэтому пустая строка здесь законна: так снимают ПИН и удаляют ключ.
            merged = self.settings.model_dump() | {k: v for k, v in changes.items() if v is not None}
            self.settings = Settings(**merged)
            self._save()
            return self.settings

    def key(self, provider: str) -> str:
        env = {"groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}[provider]
        return os.environ.get(env) or getattr(self.settings, f"{provider}_api_key")

    def public(self) -> dict[str, Any]:
        """Настройки для интерфейса: сами ключи наружу никогда не отдаём."""
        data = self.settings.model_dump(exclude={
            "groq_api_key", "gemini_api_key", "telegram_bot_token", "telegram_owner_id", "phone_pin",
            "shortcut_created", "shortcut_target", "profile_version",
        })
        data["phone_pin_set"] = bool(self.settings.phone_pin)
        for provider in ("groq", "gemini"):
            key = self.key(provider)
            data[f"{provider}_key_set"] = bool(key)
            data[f"{provider}_key_hint"] = mask_key(key)
        data["voices"] = VOICES
        return data
