"""Ярлыки Джарвиса: на рабочем столе и в автозагрузке. Автозагрузка только по переключателю в настройках:
контроль времени работает, лишь пока Джарвис запущен, но решать о запуске вместе с Windows должен человек."""
from __future__ import annotations

import base64
import logging
import subprocess
import sys
from pathlib import Path

from .config import ASSETS_DIR, PROJECT_DIR

log = logging.getLogger("jarvis.autostart")

_SHORTCUT_NAME = "Джарвис.lnk"
_OLD_SHORTCUT = "Карен.lnk"


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _run(script: str) -> bool:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            timeout=20, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("Команда с ярлыком не сработала: %s", type(exc).__name__)
        return False


def _shortcut_script(folder_expression: str, arguments: str = "-m jarvis") -> str:
    return (
        f"$d={folder_expression};"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d '{_SHORTCUT_NAME}'));"
        f"$s.TargetPath='{_ps_quote(sys.executable)}';$s.Arguments='{arguments}';"
        f"$s.WorkingDirectory='{_ps_quote(str(PROJECT_DIR))}';"
        f"$s.IconLocation='{_ps_quote(str(ASSETS_DIR / 'jarvis.ico'))}';"
        "$s.Description='Джарвис — голосовой ассистент';$s.Save()"
    )


def create_desktop_shortcut() -> bool:
    return _run(_shortcut_script("[Environment]::GetFolderPath('Desktop')"))


def desktop_shortcut_exists() -> bool:
    from .tools import known_folder

    desktop = known_folder("desktop")
    return desktop is not None and (Path(desktop) / _SHORTCUT_NAME).exists()


def set_autostart(enabled: bool) -> bool:
    if enabled:
        # Вместе с Windows без большого окна: Джарвис встречает человека талисманом и уже слышит имя.
        return _run(_shortcut_script("[Environment]::GetFolderPath('Startup')", "-m jarvis --background"))
    return _run(f"Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath('Startup')) '{_SHORTCUT_NAME}') -ErrorAction SilentlyContinue")


def remove_old_shortcuts() -> bool:
    """Ярлыки Карен ведут в её окружение. Удаляем только свои, по точному имени."""
    return _run(
        "foreach($f in 'Desktop','Startup'){"
        f"Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath($f)) '{_OLD_SHORTCUT}') -ErrorAction SilentlyContinue"
        "}"
    )
