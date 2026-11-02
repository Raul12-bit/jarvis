from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_launcher_uses_crlf() -> None:
    # С переводами строк LF cmd.exe разбирает файл неправильно и установка падает сразу же.
    data = (ROOT / "Jarvis.cmd").read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n") > 0


def test_assets_are_present() -> None:
    for name in ("assets/silero_vad_16k_op15.onnx", "assets/silero_vad.LICENSE.txt", "assets/jarvis.ico", "web/icon.png"):
        assert (ROOT / name).stat().st_size > 0, name


def test_outdated_components_are_detected(tmp_path: Path, monkeypatch) -> None:
    """Код обновился, а окружение старое: без этой проверки Джарвис упал бы на импорте новой библиотеки."""
    from jarvis import __main__ as main

    monkeypatch.setattr(main.sys, "prefix", str(tmp_path))
    monkeypatch.delenv("JARVIS_HOME", raising=False)
    assert main._components_outdated() is False  # окружение разработчика без файла блокировки  # noqa: SLF001
    (tmp_path / "requirements.lock").write_bytes(b"fastapi==0.1\n")
    assert main._components_outdated() is True  # noqa: SLF001
    (tmp_path / "requirements.lock").write_bytes((ROOT / "requirements.txt").read_bytes())
    assert main._components_outdated() is False  # noqa: SLF001


def test_checks_never_start_the_installer(tmp_path: Path, monkeypatch) -> None:
    """Настоящий случай: проверка с отдельной папкой данных запустила установщик из песочницы приложения Claude,
    и окружение легло в виртуальную копию AppData, заслонив настоящую."""
    from jarvis import __main__ as main

    monkeypatch.setattr(main.sys, "prefix", str(tmp_path))
    (tmp_path / "requirements.lock").write_bytes(b"fastapi==0.1\n")
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))
    assert main._components_outdated() is False  # noqa: SLF001


def test_old_profile_moves_to_almaty_once(tmp_path: Path) -> None:
    import json

    from jarvis.config import ConfigStore
    from jarvis.memory import Memory

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"city": "Конаев", "user_name": "Р"}, ensure_ascii=False), encoding="utf-8")
    store = ConfigStore(home)
    assert store.settings.city == "Алматы" and store.upgraded and store.settings.user_name == "Р"

    store.update(city="Астана")
    again = ConfigStore(home)
    assert again.settings.city == "Астана" and not again.upgraded  # выбор человека больше не трогаем

    memory = Memory(tmp_path / "m.db")
    memory.add_message("user", "Включи песню")
    memory.add_message("assistant", "Песня включена")
    memory.retire_history()
    assert memory.recent_messages() == []
    assert memory.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2  # записи не удалены


def test_fresh_profile_is_not_an_upgrade(tmp_path: Path) -> None:
    from jarvis.config import ConfigStore

    store = ConfigStore(tmp_path / "new")
    assert store.settings.city == "Алматы" and not store.upgraded


def test_requirements_are_pinned() -> None:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").split()
    assert lines and all("==" in line for line in lines)
    assert not any(line.startswith("pysilero") for line in lines)  # этот пакет занимал почти три ядра
