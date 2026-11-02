"""Громкость Windows. Функции с COM вызываются только в его потоке (см. uia.UiaClient.run).

При запуске Джарвиса лишь читает громкость: менять её можно только по прямой просьбе человека.
"""
from __future__ import annotations

import logging

log = logging.getLogger("jarvis.sysaudio")


def read_output_state() -> tuple[int, bool] | None:
    """(громкость в процентах, выключен ли звук) или None, если узнать не удалось."""
    from .audio_com import endpoint

    try:
        device = endpoint()
        return round(device.GetMasterVolumeLevelScalar() * 100), bool(device.GetMute())
    except (OSError, ValueError) as exc:  # COMError наследует Exception, он ниже
        log.info("Не удалось узнать громкость: %s", type(exc).__name__)
        return None
    except Exception as exc:  # noqa: BLE001 — нет устройства вывода и подобное: это не повод падать
        log.info("Не удалось узнать громкость: %s", type(exc).__name__)
        return None


def set_volume(percent: int) -> int:
    from .audio_com import endpoint

    level = max(0, min(100, int(percent)))
    device = endpoint()
    device.SetMasterVolumeLevelScalar(level / 100, None)
    if level > 0 and device.GetMute():
        device.SetMute(False, None)
    return round(device.GetMasterVolumeLevelScalar() * 100)


def set_mute(muted: bool) -> None:
    from .audio_com import endpoint

    endpoint().SetMute(bool(muted), None)


def silence_notice(state: tuple[int, bool] | None) -> str | None:
    if state is None:
        return None
    volume, muted = state
    if muted:
        return "Звук в Windows выключен, поэтому меня не слышно, сэр. Скажите «включи звук» или нажмите значок громкости."
    if volume < 5:
        return f"Громкость Windows всего {volume} %, меня может быть не слышно, сэр."
    return None
