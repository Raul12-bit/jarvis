"""Сведения о ноутбуке и управление им: батарея, память, процессы, сеть, радио, тема, команды оболочки."""
from __future__ import annotations

import ctypes
import logging
import os
import re
import socket
import subprocess
import time
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

log = logging.getLogger("jarvis.system")

# Без них Windows не живёт: завершать такие процессы бессмысленно и опасно даже по прямой просьбе.
CRITICAL_PROCESSES = {"system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
                      "svchost.exe", "dwm.exe", "fontdrvhost.exe", "memcompression", "sihost.exe", "system idle process"}
COMMAND_SECONDS = 60
OUTPUT_LIMIT = 3500
SHELL_OUTPUT_LIMIT = 2 * 2**20  # такой вывод уже уходит на телефон файлом, а дальше обрезаем, чтобы не забить память
_CWD_MARK = "<<JARVIS_CWD>>"
_SLOW_NUMBERS_EVERY = 30.0
_THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW


def human_size(size: float) -> str:
    for unit, step in (("ГБ", 2**30), ("МБ", 2**20), ("КБ", 2**10)):
        if size >= step:
            value = size / step
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
    return f"{int(size)} Б"


def _human_span(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} дн {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def human_span(seconds: float) -> str:
    return _human_span(seconds)


def wifi_name() -> str:
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, timeout=5, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    # Вывод зависит от языка Windows, но строка с именем сети везде начинается с SSID, а BSSID отсекаем.
    text = out.stdout.decode("cp866", "replace")
    match = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", text, re.M)
    return match[1] if match else ""


def internet_available() -> bool:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=2):
            return True
    except OSError:
        return False


@dataclass
class Status:
    cpu: float
    ram_used: int
    ram_total: int
    disk_free: int
    disk_total: int
    battery: float | None
    plugged: bool
    battery_seconds: float | None
    uptime: float
    wifi: str
    internet: bool


def read_status(with_network: bool = True) -> Status:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
    battery = psutil.sensors_battery()
    left = None
    if battery is not None and not battery.power_plugged and battery.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
        left = float(battery.secsleft)
    return Status(
        cpu=psutil.cpu_percent(interval=0.25),
        ram_used=memory.used,
        ram_total=memory.total,
        disk_free=disk.free,
        disk_total=disk.total,
        battery=None if battery is None else float(battery.percent),
        plugged=bool(battery.power_plugged) if battery is not None else True,
        battery_seconds=left,
        uptime=time.time() - psutil.boot_time(),
        wifi=wifi_name() if with_network else "",
        internet=internet_available() if with_network else True,
    )


def status_lines(status: Status) -> list[str]:
    lines = []
    if status.battery is not None:
        state = "заряжается" if status.plugged else "от батареи"
        rest = f", осталось {_human_span(status.battery_seconds)}" if status.battery_seconds else ""
        lines.append(f"Батарея {status.battery:.0f}%, {state}{rest}")
    lines.append(f"Процессор {status.cpu:.0f}%")
    lines.append(f"Память {human_size(status.ram_used)} из {human_size(status.ram_total)}")
    lines.append(f"Диск свободно {human_size(status.disk_free)} из {human_size(status.disk_total)}")
    if status.wifi:
        lines.append(f"Сеть {status.wifi}" + ("" if status.internet else ", интернета нет"))
    elif not status.internet:
        lines.append("Интернета нет")
    lines.append(f"Работает {_human_span(status.uptime)}")
    return lines


class LiveMeter:
    """Приборы на панели большого окна. Только то, что измерено: скорость сети считается по разнице счётчиков между вызовами."""

    def __init__(self) -> None:
        self._net_at = 0.0
        self._net: Any = None
        self._slow_at = -1e9
        self._wifi = ""
        self._internet = True
        psutil.cpu_percent(None)  # первый вызов всегда возвращает ноль: дальше считается от него

    def read(self) -> dict[str, Any]:
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
        battery = psutil.sensors_battery()
        counters = psutil.net_io_counters()
        now = time.monotonic()
        down = up = 0.0
        if self._net is not None and now > self._net_at:
            passed = now - self._net_at
            down = max(0, counters.bytes_recv - self._net.bytes_recv) / passed
            up = max(0, counters.bytes_sent - self._net.bytes_sent) / passed
        self._net, self._net_at = counters, now
        # netsh запускает отдельный процесс: каждые три секунды это было бы заметно, а имя сети меняется редко.
        if now - self._slow_at >= _SLOW_NUMBERS_EVERY:
            self._slow_at = now
            self._wifi, self._internet = wifi_name(), internet_available()
        return {
            "cpu": psutil.cpu_percent(None), "ram_used": memory.used, "ram_total": memory.total,
            "disk_free": disk.free, "disk_total": disk.total,
            "battery": None if battery is None else float(battery.percent),
            "plugged": bool(battery.power_plugged) if battery is not None else True,
            "net_down": round(down), "net_up": round(up), "wifi": self._wifi, "internet": self._internet,
        }


def local_ip() -> str:
    """Адрес в локальной сети. Пакет никуда не уходит: UDP-сокет только выбирает маршрут."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 53))
            return sock.getsockname()[0]
    except OSError:
        return ""


def ping_ms(host: str = "1.1.1.1", port: int = 443) -> float | None:
    """Время подключения по TCP: обычный ping требует прав или разбора вывода на языке системы."""
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=3):
            return (time.perf_counter() - started) * 1000
    except OSError:
        return None


def drives() -> list[Path]:
    found = []
    for part in psutil.disk_partitions(all=False):
        if "cdrom" not in part.opts:
            found.append(Path(part.mountpoint))
    return found


@dataclass
class ProcessGroup:
    name: str
    memory: int
    cpu: float
    pids: list[int]


def _friendly(name: str) -> str:
    return name.removesuffix(".exe")


def top_processes(limit: int = 8, sort: str = "memory") -> list[ProcessGroup]:
    """Процессы одной программы складываем вместе: у браузера их десятки, а человек думает о браузере целиком."""
    groups: dict[str, ProcessGroup] = {}
    own = os.getpid()
    processes = list(psutil.process_iter(["name", "memory_info"]))
    for process in processes:
        try:
            process.cpu_percent(None)
        except psutil.Error:
            continue
    time.sleep(0.4)
    cores = psutil.cpu_count() or 1
    for process in processes:
        try:
            name = (process.info["name"] or "").lower()
            info = process.info["memory_info"]
            if not name or info is None or process.pid in (0, own):
                continue
            group = groups.setdefault(name, ProcessGroup(name, 0, 0.0, []))
            group.memory += info.rss
            group.cpu += process.cpu_percent(None) / cores
            group.pids.append(process.pid)
        except psutil.Error:
            continue
    key = (lambda g: -g.cpu) if sort == "cpu" else (lambda g: -g.memory)
    return sorted(groups.values(), key=key)[:limit]


def describe_processes(groups: list[ProcessGroup]) -> str:
    return "\n".join(f"{index}. {_friendly(group.name)}: память {human_size(group.memory)}, процессор {group.cpu:.0f}%"
                     for index, group in enumerate(groups, 1))


def kill_process(name: str) -> tuple[int, str]:
    """Завершает программу по имени. Возвращает (сколько процессов, пояснение)."""
    wanted = name.lower().strip()
    wanted = wanted if wanted.endswith(".exe") else wanted + ".exe"
    if wanted in CRITICAL_PROCESSES:
        return 0, "это системный процесс, без него Windows перестанет работать"
    killed = 0
    denied = 0
    own = os.getpid()
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info["name"] or "").lower() == wanted and process.pid != own:
                process.terminate()
                killed += 1
        except psutil.AccessDenied:
            denied += 1
        except psutil.Error:
            continue
    if not killed and denied:
        return 0, "он запущен от имени администратора"
    return killed, "" if killed else "такой программы сейчас нет"


def set_dark_theme(dark: bool) -> None:
    value = 0 if dark else 1
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _THEME_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, value)
        winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, value)
    # Без этого сообщения окна узнают о новой теме только при следующем запуске.
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, ctypes.c_wchar_p("ImmersiveColorSet"), 2, 800, None)


def screen_off() -> None:
    ctypes.windll.user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2)


async def list_radios() -> dict[str, bool]:
    """Какие радио есть и включены ли они: {"wifi": True, "bluetooth": False}."""
    from winrt.windows.devices.radios import Radio, RadioKind, RadioState

    radios = await Radio.get_radios_async()
    found: dict[str, bool] = {}
    for radio in radios:
        if radio.kind == RadioKind.WI_FI:
            found["wifi"] = radio.state == RadioState.ON
        elif radio.kind == RadioKind.BLUETOOTH:
            found["bluetooth"] = radio.state == RadioState.ON
    return found


async def set_radio(kind: str, on: bool) -> bool:
    from winrt.windows.devices.radios import Radio, RadioAccessStatus, RadioKind, RadioState

    wanted = RadioKind.WI_FI if kind == "wifi" else RadioKind.BLUETOOTH
    if await Radio.request_access_async() != RadioAccessStatus.ALLOWED:
        return False
    for radio in await Radio.get_radios_async():
        if radio.kind == wanted:
            result = await radio.set_state_async(RadioState.ON if on else RadioState.OFF)
            return result == RadioAccessStatus.ALLOWED
    return False


@dataclass
class CommandResult:
    code: int | None
    output: str
    timed_out: bool = False


def run_command(command: str, seconds: int = COMMAND_SECONDS, cwd: Path | None = None) -> CommandResult:
    """Команда PowerShell без окна, с ограничением времени и вывода. Решение запускать принимает человек, а не этот код."""
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + command
    try:
        process = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, cwd=str(cwd or Path.home()), creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return CommandResult(None, f"PowerShell не запустился: {type(exc).__name__}")
    try:
        output, _ = process.communicate(timeout=seconds)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(process.pid)
        output, _ = process.communicate()
        timed_out = True
    text = output.decode("utf-8", "replace").strip()
    if len(text) > OUTPUT_LIMIT:
        text = text[:OUTPUT_LIMIT // 2] + "\n…\n" + text[-OUTPUT_LIMIT // 2:]
    return CommandResult(process.returncode, text, timed_out)


# Только чтение: такие команды Джарвис выполняет без вопроса. Всё остальное спрашивает «да» голосом или кнопкой.
_READ_ONLY = re.compile(
    r"^\s*(?:get-(?!credential)[a-z]+|test-[a-z]+|select-string|select-object|sort-object|where-object|measure-object|format-[a-z]+|"
    r"out-string|ls|dir|gci|pwd|whoami|hostname|ipconfig|systeminfo|tasklist|netstat|ping|nslookup|where(?:\.exe)?|ver|cat|type|gc|"
    r"python\s+(?:-v|--version)|node\s+(?:-v|--version)|npm\s+(?:-v|--version|list|ls)|pip\s+(?:list|show|--version)|"
    r"git\s+(?:status|log|diff|branch|remote|show|describe)|winget\s+(?:list|search|show))(?:\s|$)",
    re.IGNORECASE,
)
_SHELL_TRICKS = re.compile(r"[;&<>`\n\r]|\$\(")
_SECRET_WORDS = re.compile(r"\.ssh|\.env|\.aws|\.pem|\.key|\.kdbx|appdata|password|пароль|token|токен|credential|cookies|secret|login data", re.IGNORECASE)


def is_read_only(command: str) -> bool:
    """Безопасна ли команда без подтверждения. Весь конвейер целиком: Get-Content x | Invoke-Expression уже не чтение."""
    if not command.strip() or _SHELL_TRICKS.search(command) or _SECRET_WORDS.search(command):
        return False
    return all(_READ_ONLY.match(part) for part in command.split("|"))


class ShellSession:
    """Терминал с телефона. Каждая команда отдельный процесс, а текущая папка живёт между ними: «cd проект» работает как дома."""

    def __init__(self, cwd: Path | None = None) -> None:
        self.cwd = cwd or Path.home()
        self.process: subprocess.Popen[bytes] | None = None
        self.stopped = False

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> bool:
        if not self.running:
            return False
        self.stopped = True
        assert self.process is not None
        _kill_tree(self.process.pid)
        return True

    def run(self, command: str, seconds: int = 120) -> CommandResult:
        # Перевод строки перед меткой: команда может заканчиваться комментарием или незакрытой строкой.
        script = f"[Console]::OutputEncoding=[Text.Encoding]::UTF8;{command}\n'{_CWD_MARK}'+(Get-Location).Path"
        self.stopped = False
        try:
            self.process = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, cwd=str(self.cwd), creationflags=_NO_WINDOW,
            )
        except OSError as exc:
            return CommandResult(None, f"PowerShell не запустился: {type(exc).__name__}")
        process = self.process
        try:
            output, _ = process.communicate(timeout=seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_tree(process.pid)
            output, _ = process.communicate()
            timed_out = True
        return CommandResult(process.returncode, self._take_cwd(output.decode("utf-8", "replace"))[:SHELL_OUTPUT_LIMIT], timed_out)

    def _take_cwd(self, text: str) -> str:
        head, mark, tail = text.rpartition(_CWD_MARK)
        if not mark:
            return text.strip()  # команда сама вызвала exit: папка осталась прежней
        place = Path(tail.strip())
        if place.is_dir():
            self.cwd = place
        return head.strip()


def _kill_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
        for process in [*root.children(recursive=True), root]:
            process.kill()
    except psutil.Error:
        pass


# ---------- Файлы ----------

# Ключи, пароли и системные папки. Содержимое отсюда не должно уйти ни в модель, ни на телефон, даже если попросили.
_BLOCKED_PARTS = {"appdata", ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker", "windows", "program files", "program files (x86)",
                  "programdata", "$recycle.bin", "system volume information"}
_BLOCKED_NAMES = re.compile(r"^(?:\.env(?:\..*)?|id_(?:rsa|ed25519|ecdsa|dsa)|credentials.*|.*\.(?:pem|key|pfx|p12|kdbx)|cookies.*|login data.*|ntuser.*)$", re.I)
_TEXT_SUFFIXES = {".txt", ".md", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".csv", ".html", ".htm", ".css", ".xml", ".yaml", ".yml",
                  ".ini", ".cfg", ".toml", ".c", ".cpp", ".h", ".hpp", ".ino", ".java", ".kt", ".go", ".rs", ".sql", ".sh", ".bat", ".ps1", ".tex", ".srt"}
_READ_LIMIT = 400_000
_BAD_NAME = re.compile(r'[<>:"/\\\\|?*\x00-\x1f]')


def blocked_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True
    parts = {part.lower() for part in resolved.parts}
    return bool(parts & _BLOCKED_PARTS) or bool(_BLOCKED_NAMES.match(resolved.name))


def list_folder(folder: Path, limit: int = 15, everything: bool = False) -> list[Path]:
    """everything: хозяин сам смотрит любые папки с телефона. Для модели этого нет, ей закрытое остаётся закрытым."""
    entries = []
    try:
        for entry in folder.iterdir():
            if not everything and (entry.name.startswith(".") or entry.name.lower() in ("desktop.ini", "thumbs.db") or blocked_path(entry)):
                continue
            try:
                entries.append((entry.stat().st_mtime, entry))
            except OSError:
                continue
    except OSError:
        return []
    entries.sort(key=lambda item: -item[0])
    return [path for _, path in entries[:limit]]


def read_text_file(path: Path) -> str | None:
    """Текст файла или None, если это не текст. Docx читаем без лишних библиотек: внутри это архив с XML."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            import html
            import zipfile

            with zipfile.ZipFile(path) as archive:
                xml = archive.read("word/document.xml").decode("utf-8", "replace")
            xml = re.sub(r"</w:p>", "\n", xml)
            return html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()
        if suffix not in _TEXT_SUFFIXES:
            return None
        raw = path.read_bytes()[:_READ_LIMIT]
    except (OSError, KeyError, ValueError):
        return None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def safe_file_name(name: str) -> str:
    clean = _BAD_NAME.sub("", " ".join(str(name).split())).strip(" .")
    return clean[:120]


def unique_path(path: Path) -> Path:
    """Не перезаписываем чужую работу: занятое имя получает номер."""
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError("нет свободного имени")


def move_or_copy(path: Path, folder: Path, copy: bool) -> Path:
    import shutil

    target = unique_path(folder / path.name)
    if copy:
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)
    else:
        shutil.move(str(path), str(target))
    return target


def zip_folder(folder: Path, limit: int) -> Path | None:
    """Папка одним архивом во временном файле. None: не поместилась в предел. Недоступные файлы пропускаем: их держат программы."""
    import tempfile
    import zipfile

    target = Path(tempfile.mkdtemp(prefix="jarvis-zip-")) / f"{safe_file_name(folder.name) or 'folder'}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in folder.rglob("*"):
            if not path.is_file():
                continue
            try:
                archive.write(path, path.relative_to(folder))
            except (OSError, ValueError):
                continue
            if target.stat().st_size > limit:
                return None
    return target


class _ShFileOp(ctypes.Structure):
    _fields_ = [("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int), ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p)]


def to_recycle_bin(path: Path) -> bool:
    """В корзину, а не насовсем: человек сможет вернуть файл."""
    delete, allow_undo, no_confirm, silent, no_errors = 3, 0x40, 0x10, 0x4, 0x400
    operation = _ShFileOp(wFunc=delete, pFrom=str(path) + "\0", fFlags=allow_undo | no_confirm | silent | no_errors)
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation)) == 0 and not operation.fAnyOperationsAborted
