"""Точка входа: python -m jarvis. Запускается без консоли через pythonw."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import logging.handlers
import os
import secrets
import socket
import subprocess
import sys
import threading
import traceback
import urllib.request
from pathlib import Path
from typing import Any, Callable

import psutil

from . import __version__, migrate, winapi
from .autostart import create_desktop_shortcut, desktop_shortcut_exists, remove_old_shortcuts, set_autostart
from .config import DEFAULT_PORT, PROJECT_DIR, ConfigStore, app_home

log = logging.getLogger("jarvis")

# Меньше, чем новая версия ждёт старую при замене: старая должна успеть уйти сама.
_EXIT_SECONDS = 5.0


def _message_box(text: str) -> None:
    ctypes.windll.user32.MessageBoxW(None, text, "Джарвис", 0x10)


def _busy_threads() -> str:
    frames = sys._current_frames()
    busy = [thread for thread in threading.enumerate() if not thread.daemon and thread.ident in frames]
    return "\n".join(f"{thread.name}:\n{''.join(traceback.format_stack(frames[thread.ident])[-5:])}" for thread in busy)


class _Departure:
    """Выход не заложник потоков.

    asyncio.run и сам Python при выходе ждут все рабочие потоки, а прервать поток нельзя. Замер: с нарочно подвешенным
    поиском процесс жил ещё 33 секунды после закрытия окна. К этому моменту всё важное уже записано, поэтому ждать нечего.
    """

    def __init__(self, finish: Callable[[], None], seconds: float = _EXIT_SECONDS) -> None:
        self._finish = finish
        self._seconds = seconds
        self._lock = threading.Lock()
        self._finished = False
        self._timer: threading.Timer | None = None

    def arm(self) -> None:
        """Выключиться решено: через заданное время процесс завершится, что бы его ни держало."""
        if self._timer is None:
            self._timer = threading.Timer(self._seconds, self._force)
            self._timer.daemon = True
            self._timer.start()

    def finish(self) -> None:
        with self._lock:
            if not self._finished:
                self._finished = True
                self._finish()

    def _force(self) -> None:
        try:
            log.warning("Выключение задерживают потоки, выхожу не дожидаясь их:\n%s", _busy_threads())
            self.finish()
        finally:
            os._exit(0)


def _quiet_disconnects(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    # Proactor на Windows жалуется, когда браузер закрылся раньше сокета: это обычное закрытие окна, а не ошибка.
    if not isinstance(context.get("exception"), ConnectionResetError):
        loop.default_exception_handler(context)


def _setup_logging(home: Path) -> None:
    folder = home / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        folder / "jarvis.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "primp", "ddgs", "uvicorn.error", "websockets", "comtypes"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # pythonw запускается без консоли: stdout и stderr в нём равны None.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    # Запись в журнал не должна молчать, если упало что-то в чужом потоке: иначе поломка талисмана останется невидимой.
    threading.excepthook = lambda args: log.error(
        "Сбой в потоке %s", args.thread.name if args.thread else "?", exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(preferred: int) -> int:
    # Постоянный порт важен: разрешение на микрофон браузер запоминает для конкретного адреса.
    for port in (preferred, *range(preferred + 1, preferred + 10)):
        if _port_free(port):
            return port
    raise RuntimeError("Нет свободного порта")


def _existing_instance(path: Path) -> dict | None:
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
        if not psutil.pid_exists(int(info["pid"])):
            return None
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/health", timeout=1.5) as resp:  # noqa: S310
            health = json.load(resp)
    except (OSError, ValueError, KeyError):
        return None
    if health.get("app") != "jarvis":
        return None
    info["build"] = health.get("build", "")
    return info


def _ask_to_show(existing: dict) -> bool:
    request = urllib.request.Request(  # noqa: S310
        f"http://127.0.0.1:{int(existing['port'])}/api/window/show", method="POST",
        headers={"x-jarvis-token": str(existing["token"])},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310
            return bool(json.load(resp).get("ok"))
    except (OSError, ValueError):
        return False


def _create_shortcut(config: ConfigStore) -> None:
    """Ярлык на рабочем столе. Создаётся один раз, удалённый пользователем не возвращается.

    Если окружение или папка проекта переехали, существующий ярлык указывает в пустоту: его переписываем на новый путь.
    """
    if not sys.executable.lower().endswith("pythonw.exe"):
        return
    settings = config.settings
    place = f"{sys.executable}|{PROJECT_DIR}"
    if settings.shortcut_created and (settings.shortcut_target == place or not desktop_shortcut_exists()):
        return
    if create_desktop_shortcut():
        config.update(shortcut_created=True, shortcut_target=place)


def _settle_shortcuts(config: ConfigStore, adopted: bool) -> None:
    """Ярлыки после переезда: без этого старая автозагрузка вела бы в окружение Карен."""
    if not sys.executable.lower().endswith("pythonw.exe") or os.environ.get("JARVIS_TEST_NO_SHORTCUTS"):
        return
    if adopted:
        # Новый ярлык создаётся и для тех, кто старый когда-то удалял: это другое имя, а человек запустил новую версию сам.
        config.update(shortcut_created=False)
        remove_old_shortcuts()
    _create_shortcut(config)
    if config.settings.autostart:
        # Ярлык автозагрузки мог создать старый код: без --background Windows открывала бы большое окно при каждом входе.
        set_autostart(True)


def _components_outdated() -> bool:
    """Обновили код, а зависимости в окружении старые? Сравниваем так же, как Jarvis.cmd."""
    # С отдельной папкой данных работают проверки. Настоящий случай: проверка запустила установщик из песочницы приложения,
    # и новое окружение легло в виртуальную копию AppData, заслонив настоящую.
    if os.environ.get("JARVIS_HOME"):
        return False
    lock = Path(sys.prefix) / "requirements.lock"
    wanted = PROJECT_DIR / "requirements.txt"
    try:
        return lock.read_bytes() != wanted.read_bytes()
    except OSError:
        return False  # Нет файла блокировки: это окружение разработчика, оно обновляется вручную.


def _hand_over_to_installer() -> None:
    # Обновлять надо в окне с прогрессом: без него несколько секунд тишины выглядят как поломка.
    subprocess.Popen(["cmd", "/c", "start", "", "cmd", "/c", str(PROJECT_DIR / "Jarvis.cmd")], creationflags=subprocess.CREATE_NO_WINDOW)


def main() -> int:
    parser = argparse.ArgumentParser(prog="jarvis")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-window", action="store_true", help="только сервер, для отладки и тестов")
    parser.add_argument("--token", default=None, help="токен доступа, для тестов")
    parser.add_argument("--browser-arg", action="append", default=[], help="дополнительный флаг браузера, для тестов")
    parser.add_argument("--background", action="store_true", help="без большого окна: автозапуск вместе с Windows")
    args = parser.parse_args()

    if not args.no_window and _components_outdated():
        _hand_over_to_installer()
        return 0

    winapi.enable_dpi_awareness()
    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    _setup_logging(home)
    runtime = home / "runtime.json"

    from .browser import launch_window
    from .window_host import WindowHost

    build = migrate.build_id()
    existing = None if args.no_window else _existing_instance(runtime)
    if existing and existing.get("build") != build:
        # В памяти работает другая версия кода. Просить её показать окно значит навсегда остаться на старой.
        log.info("Заменяю работающую версию %s новой %s", existing.get("build") or "?", build)
        migrate.stop_instance(runtime)
        migrate.close_browser_profile(home / "browser")
        existing = None
    if existing:
        # Джарвис уже запущен, возможно, со скрытым окном: просим его показаться, а не открываем второе.
        if not args.background and not _ask_to_show(existing):
            launch_window(f"http://127.0.0.1:{existing['port']}/?t={existing['token']}", home)
        return 0

    adopted = False if args.no_window else migrate.leave_karen(home)
    config = ConfigStore(home)
    log.info("Запуск Джарвиса %s, код %s, режим %s", __version__, build, "без окна" if args.no_window else "фоновый" if args.background else "с окном")

    import uvicorn

    from .server import create_app

    token = args.token or secrets.token_urlsafe(32)
    try:
        port = _pick_port(args.port)
    except RuntimeError as exc:
        _message_box(f"Джарвис не смог запуститься: {exc}")
        return 1
    url = f"http://127.0.0.1:{port}/?t={token}"

    server_box: dict[str, uvicorn.Server] = {}

    def finish() -> None:
        runtime.unlink(missing_ok=True)
        if app.state.assistant.restart_wanted:
            # Порт и файл запуска уже свободны. Без большого окна: просили перезапуск, а не показать окно ещё раз.
            subprocess.Popen([sys.executable, "-m", "jarvis", "--background"], cwd=PROJECT_DIR, creationflags=subprocess.CREATE_NO_WINDOW)

    departure = _Departure(finish)

    def stop_server() -> None:
        departure.arm()
        server_box["server"].should_exit = True

    app = create_app(config, token, port, on_all_clients_gone=None if args.no_window else stop_server, build=build)
    # Запрос, который ждёт зависший поток, не должен мешать сохранить данные при выходе: через 2 секунды его отменяют.
    uv_config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, access_log=False, timeout_graceful_shutdown=2,
        ws="websockets-sansio", ws_ping_interval=None, ws_ping_timeout=None, lifespan="on", loop="asyncio",
    )
    server = uvicorn.Server(uv_config)
    server_box["server"] = server

    runtime.write_text(json.dumps({"pid": os.getpid(), "port": port, "token": token}), encoding="utf-8")
    threading.Thread(target=_settle_shortcuts, args=(config, adopted), name="jarvis-shortcuts", daemon=True).start()

    async def run() -> None:
        asyncio.get_running_loop().set_exception_handler(_quiet_disconnects)
        serve = asyncio.create_task(server.serve())
        while not server.started and not serve.done():  # у uvicorn нет события готовности
            await asyncio.sleep(0.05)
        if server.started and not args.no_window:
            assistant = app.state.assistant
            host = WindowHost(url, config.home, port, args.browser_arg)
            # Прошлый запуск мог аварийно завершиться и оставить скрытое окно с включённым микрофоном.
            host.close()
            hidden = args.background and config.settings.onboarded
            started = await host.start_hidden() if hidden else host.start_visible()
            if server.should_exit:
                host.close()  # Выключиться попросили, пока окно открывалось: Assistant о нём ещё не знает.
            elif not started:
                _message_box("Нужен Microsoft Edge или Google Chrome: Джарвис показывает окно через него.")
                server.should_exit = True
            else:
                assistant.browser_pid = host.pid
                assistant.desktop.jarvis_pid = host.pid
                assistant.attach_host(host)
                if hidden:
                    # Запуск вместе с Windows: теперь с телефона можно достучаться, и об этом стоит сказать.
                    notice = asyncio.create_task(assistant.phone.notify("Джарвис на связи, сэр."))
                    notice.add_done_callback(lambda t: t.cancelled() or t.exception())
        await serve

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Аварийное завершение")
        _message_box("Джарвис завершился с ошибкой. Подробности в журнале: " + str(config.home / "logs"))
        return 1
    finally:
        runtime.unlink(missing_ok=True)
    departure.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
