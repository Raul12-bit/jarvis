"""Смена версии: то, из-за чего талисман ни разу не запустился. Старый процесс в памяти, старый профиль и старые ярлыки."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis import __main__ as main
from jarvis import migrate
from jarvis.config import DEFAULT_VOICE, ConfigStore
from jarvis.memory import Memory

ROOT = Path(__file__).resolve().parent.parent


# ---------- Какой код сейчас в памяти ----------


def test_build_id_changes_with_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "jarvis").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "requirements.txt").write_text("a==1", "utf-8")
    (tmp_path / "jarvis" / "core.py").write_text("x = 1", "utf-8")
    (tmp_path / "web" / "app.js").write_text("let a", "utf-8")
    monkeypatch.setattr(migrate, "PROJECT_DIR", tmp_path)
    first = migrate.build_id()
    assert migrate.build_id() == first  # без правок отпечаток тот же
    (tmp_path / "web" / "app.js").write_text("let b", "utf-8")
    second = migrate.build_id()
    assert second != first  # страница и код зависят друг от друга: новая страница со старым сервером и сломала талисман
    (tmp_path / "jarvis" / "core.py").write_text("x = 2", "utf-8")
    assert migrate.build_id() != second


def test_real_build_id_is_short_and_stable() -> None:
    assert migrate.build_id() == migrate.build_id() and len(migrate.build_id()) == 16


def test_instance_health_exposes_build(config: ConfigStore) -> None:
    from fastapi.testclient import TestClient

    from jarvis.server import create_app

    app = create_app(config, "t" * 16, 47999, build="abc123")
    with TestClient(app, base_url="http://127.0.0.1:47999") as client:
        assert client.get("/health").json() == {"app": "jarvis", "version": main.__version__, "build": "abc123"}


def test_existing_instance_reports_its_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Reply:
        def __enter__(self) -> "Reply":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"app": "jarvis", "build": "old"}'

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"pid": 1, "port": 1, "token": "t"}), "utf-8")
    monkeypatch.setattr(main.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *a, **k: Reply())
    assert main._existing_instance(runtime)["build"] == "old"  # noqa: SLF001


def test_old_karen_is_not_taken_for_jarvis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Reply:
        def __enter__(self) -> "Reply":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        @staticmethod
        def read() -> bytes:
            return b'{"app": "karen"}'

    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"pid": 1, "port": 1, "token": "t"}), "utf-8")
    monkeypatch.setattr(main.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *a, **k: Reply())
    assert main._existing_instance(runtime) is None  # noqa: SLF001


# ---------- Замена работающего процесса ----------


def _sleeper(tmp_path: Path, module: str = "karen") -> subprocess.Popen[bytes]:
    """Настоящий процесс с такой же командной строкой, как у старой версии."""
    package = tmp_path / module
    package.mkdir(exist_ok=True)
    (package / "__init__.py").write_text("", "utf-8")
    (package / "__main__.py").write_text("import time\ntime.sleep(60)\n", "utf-8")
    return subprocess.Popen([sys.executable, "-m", module], cwd=tmp_path, creationflags=subprocess.CREATE_NO_WINDOW)


def test_stop_instance_ends_a_running_old_version(tmp_path: Path) -> None:
    process = _sleeper(tmp_path)
    runtime = tmp_path / "runtime.json"
    # Порт никому не принадлежит: старая версия не отвечает на вежливую просьбу, и её приходится завершить.
    runtime.write_text(json.dumps({"pid": process.pid, "port": 9, "token": "t"}), "utf-8")
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and migrate._our_process(process.pid) is None:  # noqa: SLF001
            time.sleep(0.05)
        started = time.monotonic()
        migrate._STOP_SECONDS, saved = 0.2, migrate._STOP_SECONDS  # noqa: SLF001
        try:
            assert migrate.stop_instance(runtime) is True
        finally:
            migrate._STOP_SECONDS = saved  # noqa: SLF001
        assert time.monotonic() - started < 15
        assert process.wait(5) is not None
        assert not runtime.exists()
    finally:
        process.kill()


def test_stop_instance_never_touches_a_foreign_process(tmp_path: Path) -> None:
    """Номер из файла мог достаться другой программе после перезагрузки: её завершать нельзя."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=subprocess.CREATE_NO_WINDOW)
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"pid": other.pid, "port": 9, "token": "t"}), "utf-8")
    try:
        assert migrate.stop_instance(runtime) is True
        assert other.poll() is None
        assert not runtime.exists()
    finally:
        other.kill()


@pytest.mark.parametrize("content", ["", "не json", "{}", '{"pid": "x", "port": 1, "token": "t"}'])
def test_stop_instance_survives_broken_file(tmp_path: Path, content: str) -> None:
    runtime = tmp_path / "runtime.json"
    runtime.write_text(content, "utf-8")
    assert migrate.stop_instance(runtime) is True


def test_stop_instance_without_file(tmp_path: Path) -> None:
    assert migrate.stop_instance(tmp_path / "нет.json") is True


# ---------- Выключение ----------

LEAVING = '''
import asyncio, sys, time
from jarvis.__main__ import _Departure

def hung_thread():
    time.sleep(120)

departure = _Departure(lambda: print("finish", flush=True), seconds=float(sys.argv[1]))

async def run():
    if sys.argv[2] == "hung":
        asyncio.get_running_loop().run_in_executor(None, hung_thread)
    departure.arm()

asyncio.run(run())
departure.finish()
'''


def _leave(seconds: float, mode: str) -> tuple[subprocess.CompletedProcess, float]:
    started = time.monotonic()
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", LEAVING, str(seconds), mode], cwd=ROOT, capture_output=True, encoding="utf-8", timeout=90,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return done, time.monotonic() - started


def test_hung_thread_does_not_hold_the_exit() -> None:
    """Поток нельзя прервать, а Python ждёт его при выходе. Здесь настоящий процесс и настоящий выход Python."""
    done, took = _leave(0.3, "hung")
    assert took < 30
    assert done.returncode == 0
    assert done.stdout.split() == ["finish"]  # файл запуска убран и перезапуск сделан ровно один раз
    assert "hung_thread" in done.stderr  # в журнале видно, что именно держало


def test_clean_exit_does_not_wait_for_the_guard() -> None:
    done, took = _leave(60.0, "clean")
    assert took < 30
    assert done.returncode == 0
    assert done.stdout.split() == ["finish"]


def test_forced_exit_happens_even_if_finishing_fails() -> None:
    script = LEAVING.replace('lambda: print("finish", flush=True)', "lambda: 1 / 0")
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script.replace("departure.finish()\n", ""), "0.3", "hung"], cwd=ROOT, capture_output=True, encoding="utf-8", timeout=90,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert done.returncode == 0


def test_closed_window_is_not_an_error_in_the_log() -> None:
    seen: list[dict] = []

    class Loop:
        def default_exception_handler(self, context: dict) -> None:
            seen.append(context)

    main._quiet_disconnects(Loop(), {"exception": ConnectionResetError()})  # noqa: SLF001
    assert seen == []
    main._quiet_disconnects(Loop(), {"exception": ValueError()})  # noqa: SLF001
    main._quiet_disconnects(Loop(), {"message": "без исключения"})  # noqa: SLF001
    assert len(seen) == 2


# ---------- Переезд данных ----------


def _old_home(base: Path) -> Path:
    old = base / "Karen"
    old.mkdir(parents=True)
    (old / "config.json").write_text(json.dumps({
        "voice": "en-US-EmmaMultilingualNeural", "user_name": "Сэр", "groq_api_key": "gsk_ключ", "telegram_bot_token": "1:token",
        "telegram_owner_id": 8360548464, "autostart": True, "profile_version": 2, "city": "Астана", "phone_shell": False,
        "onboarded": True, "shortcut_created": True, "shortcut_target": "старый путь", "speech_rate": 10,
    }, ensure_ascii=False), "utf-8")
    memory = Memory(old / "karen.db")
    memory.add_fact("пью кофе без сахара")
    memory.add_note("купить молоко")
    memory.add_message("user", "Как дела?")
    memory.add_message("assistant", "Я проверила почту.")
    memory.close()
    (old / "models").mkdir()
    (old / "models" / "модель.bin").write_bytes(b"model")
    return old


def test_old_data_moves_as_a_copy_and_nothing_is_lost(tmp_path: Path) -> None:
    old = _old_home(tmp_path)
    new = tmp_path / "Jarvis"
    assert migrate.adopt_old_data(old, new) is True
    assert (old / "config.json").exists() and (old / "karen.db").exists()  # откат возможен: старое на месте
    assert (new / "models" / "модель.bin").read_bytes() == b"model"

    config = ConfigStore(new)
    settings = config.settings
    assert settings.groq_api_key == "gsk_ключ" and settings.telegram_bot_token == "1:token" and settings.telegram_owner_id == 8360548464
    assert settings.onboarded and settings.autostart and settings.speech_rate == 10
    assert settings.city == "Астана"  # выбор человека не трогаем
    assert settings.voice == DEFAULT_VOICE  # женский голос не стал причиной считать файл битым
    assert settings.mascot and settings.phone_shell and settings.allow_camera  # хозяин попросил полный доступ
    assert config.upgraded
    assert not (new / "config.broken.json").exists()

    memory = Memory(new / "jarvis.db")
    assert [fact["text"] for fact in memory.facts()] == ["пью кофе без сахара"]
    assert [note["text"] for note in memory.notes()] == ["купить молоко"]
    memory.retire_history()  # женский род в истории не должен служить примером для модели
    assert memory.recent_messages() == []
    memory.close()


def test_second_launch_does_not_overwrite_new_settings(tmp_path: Path) -> None:
    old = _old_home(tmp_path)
    new = tmp_path / "Jarvis"
    assert migrate.adopt_old_data(old, new)
    ConfigStore(new).update(city="Алматы")
    assert migrate.adopt_old_data(old, new) is False
    assert ConfigStore(new).settings.city == "Алматы"


def test_interrupted_move_starts_again(tmp_path: Path) -> None:
    """Настройки копируются последними: без них незаконченный переезд повторится, а не оставит пустой профиль."""
    old = _old_home(tmp_path)
    new = tmp_path / "Jarvis"
    new.mkdir()
    (new / "jarvis.db").write_bytes(b"")
    assert migrate.adopt_old_data(old, new) is True
    assert (new / "config.json").exists()


def test_nothing_to_move_without_karen(tmp_path: Path) -> None:
    assert migrate.adopt_old_data(tmp_path / "нет", tmp_path / "Jarvis") is False


def test_leave_karen_is_off_with_test_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """С JARVIS_HOME работают тесты и проверки: они не должны трогать настоящую Карен на этом компьютере."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _old_home(tmp_path)
    assert migrate.leave_karen(tmp_path / "home") is False
    assert not (tmp_path / "home" / "config.json").exists()


def test_leave_karen_moves_data_in_real_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _old_home(tmp_path)
    monkeypatch.setattr(migrate, "close_browser_profile", lambda profile: 0)
    assert migrate.leave_karen(tmp_path / "Jarvis") is True
    assert ConfigStore(tmp_path / "Jarvis").settings.telegram_owner_id == 8360548464


def test_profile_migration_keeps_the_chosen_city_on_later_versions(tmp_path: Path) -> None:
    """Прошлая миграция заново ставила Алматы при каждой новой версии профиля и затёрла бы выбор человека."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"city": "Астана", "profile_version": 2}, ensure_ascii=False), "utf-8")
    assert ConfigStore(home).settings.city == "Астана"


def test_user_can_turn_full_access_off_and_it_stays_off(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = ConfigStore(home)
    store.update(phone_shell=False, allow_camera=False)
    again = ConfigStore(home)
    assert not again.settings.phone_shell and not again.settings.allow_camera


# ---------- Старые ярлыки ----------


def test_old_shortcuts_lead_to_the_new_launcher(tmp_path: Path) -> None:
    """Ярлыки Карен запускают python -m karen: без мостика pythonw падал бы молча, и человек видел бы только тишину."""
    source = (ROOT / "karen" / "__main__.py").read_text("utf-8")
    assert "Jarvis.cmd" in source and "import jarvis" not in source
    assert (ROOT / "karen" / "__init__.py").exists()


def test_installer_window_does_not_stay_open() -> None:
    """«start файл.cmd» запускает его через cmd /K: после установки на экране оставалось пустое чёрное окно."""
    for rel in ("jarvis/__main__.py", "karen/__main__.py"):
        assert '"start", "", "cmd", "/c"' in (ROOT / rel).read_text("utf-8"), rel


def test_new_launcher_uses_crlf() -> None:
    data = (ROOT / "Jarvis.cmd").read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n") > 0
    assert b"-m jarvis" in data


def test_shortcut_is_rewritten_when_project_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = ConfigStore(tmp_path / "home")
    created: list[int] = []
    monkeypatch.setattr(main.sys, "executable", "C:\\venv\\pythonw.exe")
    monkeypatch.setattr(main, "desktop_shortcut_exists", lambda: True)
    monkeypatch.setattr(main, "create_desktop_shortcut", lambda: created.append(1) or True)
    main._create_shortcut(config)  # noqa: SLF001
    main._create_shortcut(config)  # noqa: SLF001
    assert len(created) == 1  # без переезда ярлык не трогаем
    monkeypatch.setattr(main, "PROJECT_DIR", Path("D:\\Новая папка"))
    main._create_shortcut(config)  # noqa: SLF001
    assert len(created) == 2


def test_deleted_shortcut_does_not_come_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = ConfigStore(tmp_path / "home")
    created: list[int] = []
    monkeypatch.setattr(main.sys, "executable", "C:\\venv\\pythonw.exe")
    monkeypatch.setattr(main, "create_desktop_shortcut", lambda: created.append(1) or True)
    monkeypatch.setattr(main, "desktop_shortcut_exists", lambda: True)
    main._create_shortcut(config)  # noqa: SLF001
    monkeypatch.setattr(main, "desktop_shortcut_exists", lambda: False)
    monkeypatch.setattr(main, "PROJECT_DIR", Path("D:\\Другая"))
    main._create_shortcut(config)  # noqa: SLF001
    assert len(created) == 1


def test_autostart_shortcut_is_repaired_on_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Старый ярлык автозагрузки создан прошлой версией: без --background он открывал бы большое окно при каждом входе."""
    config = ConfigStore(tmp_path / "home")
    config.update(autostart=True)
    calls: list[str] = []
    monkeypatch.setattr(main.sys, "executable", "C:\\venv\\pythonw.exe")
    monkeypatch.setattr(main, "remove_old_shortcuts", lambda: calls.append("remove") or True)
    monkeypatch.setattr(main, "create_desktop_shortcut", lambda: calls.append("desktop") or True)
    monkeypatch.setattr(main, "desktop_shortcut_exists", lambda: True)
    monkeypatch.setattr(main, "set_autostart", lambda on: calls.append(f"autostart {on}") or True)
    main._settle_shortcuts(config, adopted=True)  # noqa: SLF001
    assert calls == ["remove", "desktop", "autostart True"]


def test_shortcut_scripts_point_to_jarvis() -> None:
    from jarvis import autostart

    script = autostart._shortcut_script("X", "-m jarvis --background")  # noqa: SLF001
    assert "Джарвис.lnk" in script and "-m jarvis --background" in script and "jarvis.ico" in script
