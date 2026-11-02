"""Новые возможности: талисман, распознавание текста, система, файлы, сценарии, команды про саму Джарвис."""
from __future__ import annotations

import asyncio
import random
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

from jarvis import intents, ocr, system, tools, winapi
from jarvis.config import ConfigStore
from jarvis.mascot import DOZE_AFTER, MascotMind
from jarvis.mascot_art import MascotArt, Pose, premultiplied_bgra, render_bubble
from jarvis.memory import Memory
from jarvis.tools import CONFIRM_PREFIX, ToolBox, is_self_name, speaks_directly, wanted_groups
from jarvis.uia import UiItem
from jarvis.wake import check_addressed
from jarvis.watcher import BATTERY_CRITICAL, Notice, WatcherState, came_back, check_status
from jarvis.winapi import WindowInfo

from .conftest import mock_client


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------- Талисман: рисунок ----------


def test_art_is_transparent_at_corners_and_visible_in_center() -> None:
    art = MascotArt(1.0)
    frame = art.render(Pose())
    assert frame.size == (art.size, art.size)
    for corner in ((0, 0), (art.size - 1, 0), (0, art.size - 1), (art.size - 1, art.size - 1)):
        assert frame.getpixel(corner)[3] == 0
    assert frame.getpixel((art.size // 2, round(art.size * 0.55)))[3] == 255


def test_body_is_not_cut_at_the_bottom() -> None:
    """Настоящая ошибка: низ шара упирался в край заготовки и выглядел срезанным."""
    art = MascotArt(1.0)
    alpha = art._glass.getchannel("A")  # noqa: SLF001
    width, height = alpha.size
    assert alpha.getpixel((width // 2, height - 1)) == 0
    assert alpha.getpixel((width // 2, 0)) == 0


@pytest.mark.parametrize("scale", [0.72, 0.9, 1.15, 2.0])
def test_art_scales(scale: float) -> None:
    art = MascotArt(scale)
    assert art.render(Pose(mouth=0.7, eye_shape="happy", sparkle=0.5)).size == (art.size, art.size)


@pytest.mark.parametrize("pose", [
    Pose(eye_shape="closed", sleep_z=0.3), Pose(eye_shape="sad"), Pose(eye_shape="focus", orbit=0.2), Pose(waves=0.4),
    Pose(muted_mark=True), Pose(stretch=0.2, lean=5.0, antenna=1.0), Pose(eye_open=0.08), Pose(mouth=1.0),
])
def test_every_expression_draws(pose: Pose) -> None:
    assert MascotArt(1.0).render(pose).getbbox() is not None


def test_expressions_look_different() -> None:
    art = MascotArt(1.0)
    assert art.render(Pose(eye_shape="oval")).tobytes() != art.render(Pose(eye_shape="happy")).tobytes()


def test_premultiplied_format() -> None:
    image = Image.new("RGBA", (1, 1), (200, 100, 50, 128))
    blue, green, red, alpha = premultiplied_bgra(image)
    assert (blue, green, red, alpha) == (25, 50, 100, 128)


def test_bubble_wraps_and_cuts_long_text() -> None:
    short = render_bubble("Слушаю", (100, 200, 255), 1.0, tail_right=True)
    long = render_bubble("очень длинный текст " * 40, (100, 200, 255), 1.0, tail_right=False)
    assert long.height > short.height and long.width <= 300 and long.height < 120


# ---------- Талисман: характер ----------


def mind() -> MascotMind:
    return MascotMind(random.Random(4))


def run_for(character: MascotMind, seconds: float, **kwargs: Any) -> Pose:
    pose = Pose()
    for _ in range(int(seconds * 30)):
        pose = character.tick(1 / 30, **kwargs)
    return pose


def test_blinks_on_its_own() -> None:
    character = mind()
    opened = [character.tick(1 / 30).eye_open for _ in range(30 * 12)]
    assert min(opened) < 0.2 and max(opened) >= 1.0


def test_breathes() -> None:
    character = mind()
    bobs = [character.tick(1 / 30).bob for _ in range(30 * 5)]
    assert max(bobs) - min(bobs) > 2


def test_hearing_makes_it_hop_and_listen() -> None:
    character = mind()
    run_for(character, 1)
    character.set_state("hearing")
    pose = character.tick(1 / 30)
    assert character.hop_left > 0 and pose.waves >= 0 and pose.eye_open > 1


def test_thinking_shows_dots_and_looks_up() -> None:
    character = mind()
    character.set_state("thinking")
    pose = run_for(character, 1)
    assert pose.orbit >= 0 and pose.gaze[1] < -0.5


def test_mouth_follows_real_voice_level() -> None:
    character = mind()
    character.set_state("speaking")
    for _ in range(20):
        character.set_level(0.9)
        pose = character.tick(1 / 30)
    assert pose.mouth > 0.6
    for _ in range(20):
        character.set_level(0.0)
        pose = character.tick(1 / 30)
    assert pose.mouth < 0.2


def test_mouth_moves_without_level_when_window_is_hidden() -> None:
    character = mind()
    character.set_state("speaking")
    mouths = [character.tick(1 / 30).mouth for _ in range(60)]
    assert max(mouths) - min(mouths) > 0.2


def test_mouth_closes_after_speech() -> None:
    character = mind()
    character.set_state("speaking")
    run_for(character, 1)
    character.set_state("listening")
    assert run_for(character, 1).mouth < 0.05


def test_looks_at_moving_cursor() -> None:
    character = mind()
    pose = run_for(character, 1, cursor=(1.0, 0.0), cursor_moved=True)
    assert pose.gaze[0] > 0.7


def test_dozes_when_nobody_is_around_and_wakes_up() -> None:
    character = mind()
    pose = run_for(character, 1, user_idle=DOZE_AFTER + 1)
    assert pose.eye_shape == "closed" and pose.sleep_z >= 0
    pose = character.tick(1 / 30, user_idle=0.0)
    assert pose.eye_shape != "closed" and character.emote_name == "surprise"


def test_does_not_doze_while_working() -> None:
    character = mind()
    character.set_state("speaking")
    assert run_for(character, 1, user_idle=DOZE_AFTER + 1).eye_shape != "closed"


def test_emotes_change_face_and_end() -> None:
    character = mind()
    character.emote("happy")
    pose = character.tick(1 / 30)
    assert pose.eye_shape == "happy" and pose.sparkle >= 0
    pose = run_for(character, 3)
    assert pose.eye_shape == "oval"


def test_unknown_emote_is_ignored() -> None:
    character = mind()
    character.emote("танец")
    assert character.emote_name == ""


def test_muted_shows_mark() -> None:
    character = mind()
    character.set_state("muted")
    assert character.tick(1 / 30).muted_mark


def test_dragging_makes_eyes_wide_and_antenna_swing() -> None:
    character = mind()
    character.dragging = True
    swings = [abs(character.tick(1 / 30, drag_speed=200.0).antenna) for _ in range(20)]
    assert max(swings) > 0.3
    assert character.tick(1 / 30, drag_speed=0.0).eye_open >= 1.0


def test_springs_stay_stable_on_slow_frames() -> None:
    character = mind()
    character.hop()
    for _ in range(200):
        pose = character.tick(0.125)
    assert abs(pose.stretch) < 0.2 and abs(pose.antenna) < 2


def test_frame_rate_drops_when_nothing_happens() -> None:
    character = mind()
    assert not character.lively
    character.set_state("speaking")
    assert character.lively


# ---------- Распознавание текста ----------


def _word(text: str, left: float, top: float, width: float = 40, height: float = 12) -> ocr.Word:
    return ocr.Word(text, left, top, left + width, top + height)


def _line(*words: ocr.Word) -> ocr.Line:
    return ocr.Line(" ".join(word.text for word in words), list(words))


def test_find_text_center_of_phrase() -> None:
    results = {"ru": [_line(_word("Сохранить", 100, 50, 60), _word("как", 170, 50, 30)), _line(_word("Отмена", 100, 90, 50))]}
    found = ocr.find_text(results, "сохранить как")
    assert found[0].center == (150, 56)


def test_find_text_forgives_ocr_errors() -> None:
    results = {"ru": [_line(_word("Сохранигь", 10, 10))]}
    assert ocr.find_text(results, "Сохранить")


def test_find_text_mixed_alphabets() -> None:
    results = {"ru": [_line(_word("Рlау", 10, 10))]}  # первая буква кириллицей
    assert ocr.find_text(results, "play")


def test_find_text_rejects_other_words() -> None:
    results = {"ru": [_line(_word("Отмена", 10, 10))]}
    assert ocr.find_text(results, "Сохранить") == []
    assert ocr.find_text(results, "") == []


def test_find_text_same_text_from_two_engines_counts_once() -> None:
    results = {"ru": [_line(_word("OK", 10, 10))], "en-US": [_line(_word("OK", 11, 10))]}
    assert len(ocr.find_text(results, "OK")) == 1


def test_find_text_returns_top_most_first() -> None:
    results = {"ru": [_line(_word("Да", 10, 200)), _line(_word("Да", 10, 50))]}
    found = ocr.find_text(results, "Да")
    assert len(found) == 2 and found[0].center[1] < found[1].center[1]


def test_plain_text_picks_engine_by_alphabet() -> None:
    results = {
        "ru": [_line(_word("Привет", 10, 10)), _line(_word("Неllо", 10, 40))],
        "en-US": [_line(_word("Npnaem", 10, 10)), _line(_word("Hello", 10, 40))],
    }
    assert ocr.plain_text(results) == "Привет\nНеllо" or "Привет" in ocr.plain_text(results)


def _rendered(text: str, size: int = 22) -> Image.Image:
    image = Image.new("RGB", (640, 120), (250, 250, 250))
    ImageDraw.Draw(image).text((30, 40), text, fill=(20, 20, 20), font=ImageFont.truetype("segoeui.ttf", size))
    return image


def test_real_windows_ocr_reads_and_finds() -> None:
    """Настоящее распознавание Windows: пропускается только там, где нет русского языка."""
    try:
        results = run(ocr.read_image(_rendered("Сохранить файл и выйти")))
    except ocr.OcrUnavailable:
        pytest.skip("нет распознавания текста")
    assert "Сохранить" in ocr.plain_text(results)
    found = ocr.find_text(results, "файл")
    assert found and 100 < found[0].center[0] < 300 and 35 < found[0].center[1] < 90


# ---------- Система ----------


@pytest.mark.parametrize(
    "name",
    ["C:/Users/me/AppData/Local/Jarvis/config.json", "C:/Users/me/.ssh/id_rsa", "C:/Windows/System32/cmd.exe", "C:/Users/me/project/.env",
     "D:/keys/server.pem", "C:/Program Files/app/a.exe", "C:/Users/me/backup.kdbx"],
)
def test_private_and_system_paths_are_blocked(name: str) -> None:
    assert system.blocked_path(Path(name))


@pytest.mark.parametrize("name", ["C:/Users/me/Documents/отчёт.docx", "D:/Фильмы/кино.mkv", "C:/Users/me/Desktop/main.py"])
def test_normal_paths_are_allowed(name: str) -> None:
    assert not system.blocked_path(Path(name))


def test_safe_file_name_removes_path_tricks() -> None:
    assert "/" not in system.safe_file_name("../../evil.exe") and "\\" not in system.safe_file_name("..\\evil.exe")
    assert system.safe_file_name('a<b>:c"?.txt') == "abc.txt"
    assert system.safe_file_name("   ") == ""


def test_unique_path_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    assert system.unique_path(tmp_path / "a.txt").name == "a (2).txt"
    assert system.unique_path(tmp_path / "b.txt").name == "b.txt"


def test_read_text_file_encodings_and_docx(tmp_path: Path) -> None:
    utf = tmp_path / "u.txt"
    utf.write_text("привет", "utf-8")
    cp = tmp_path / "c.txt"
    cp.write_bytes("привет".encode("cp1251"))
    docx = tmp_path / "d.docx"
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", "<w:p><w:t>Абзац один</w:t></w:p><w:p><w:t>Два &amp; три</w:t></w:p>")
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"\0\1")
    assert system.read_text_file(utf) == "привет"
    assert system.read_text_file(cp) == "привет"
    assert system.read_text_file(docx) == "Абзац один\nДва & три"
    assert system.read_text_file(binary) is None


def test_list_folder_skips_hidden_and_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "_BLOCKED_PARTS", system._BLOCKED_PARTS - {"appdata"})  # noqa: SLF001
    for name in ("a.txt", ".hidden", "desktop.ini", ".env", "b.txt"):
        (tmp_path / name).write_text("x")
    assert {path.name for path in system.list_folder(tmp_path)} == {"a.txt", "b.txt"}


def test_move_and_copy_do_not_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "from"
    target = tmp_path / "to"
    source.mkdir()
    target.mkdir()
    (source / "a.txt").write_text("новый")
    (target / "a.txt").write_text("старый")
    moved = system.move_or_copy(source / "a.txt", target, copy=True)
    assert moved.name == "a (2).txt" and (target / "a.txt").read_text() == "старый" and (source / "a.txt").exists()
    moved = system.move_or_copy(source / "a.txt", target, copy=False)
    assert moved.name == "a (3).txt" and not (source / "a.txt").exists()


def test_recycle_bin_really_removes_file(tmp_path: Path) -> None:
    path = tmp_path / "в корзину.txt"
    path.write_text("x")
    assert system.to_recycle_bin(path) and not path.exists()


def test_critical_processes_are_refused() -> None:
    for name in ("csrss.exe", "lsass", "winlogon.exe", "svchost.exe"):
        count, why = system.kill_process(name)
        assert count == 0 and "системный" in why


def test_kill_unknown_process() -> None:
    assert system.kill_process("нет-такой-программы")[0] == 0


def test_status_lines_read_like_speech() -> None:
    status = system.Status(10.0, 2**30, 8 * 2**30, 50 * 2**30, 400 * 2**30, 55.0, False, 3600.0, 7200.0, "Дом", True)
    text = " ".join(system.status_lines(status))
    assert "Батарея 55%, от батареи, осталось 1 ч 0 мин" in text and "Сеть Дом" in text and "Работает 2 ч" in text


def test_status_without_battery_and_internet() -> None:
    status = system.Status(10.0, 1, 2, 3, 4, None, True, None, 60.0, "", False)
    text = " ".join(system.status_lines(status))
    assert "Батарея" not in text and "Интернета нет" in text


def test_top_processes_are_grouped() -> None:
    groups = system.top_processes(50)
    assert groups and all(group.pids for group in groups)
    assert len({group.name for group in groups}) == len(groups)


def test_run_command_returns_output_and_code() -> None:
    result = system.run_command("Write-Output 'привет'; exit 3", 20)
    assert result.code == 3 and "привет" in result.output and not result.timed_out


def test_run_command_stops_at_timeout() -> None:
    result = system.run_command("Start-Sleep 30", 2)
    assert result.timed_out


def test_long_output_is_cut_in_the_middle() -> None:
    result = system.run_command("1..3000 | ForEach-Object { 'строка' + $_ }", 20)
    assert len(result.output) <= system.OUTPUT_LIMIT + 10 and "строка1" in result.output and "строка3000" in result.output


# ---------- Наблюдение за ноутбуком ----------


def status(battery: float | None = 90.0, plugged: bool = False, disk: int = 50 * 2**30, used: float = 0.5, internet: bool = True) -> system.Status:
    return system.Status(5.0, int(used * 8 * 2**30), 8 * 2**30, disk, 400 * 2**30, battery, plugged, None, 100.0, "", internet)


def test_battery_warns_once_per_level() -> None:
    state = WatcherState()
    assert check_status(state, status(19), 0) == [Notice("Батарея 19 процентов, сэр, пора подумать о зарядке.")]
    assert check_status(state, status(18), 60) == []
    critical = check_status(state, status(BATTERY_CRITICAL), 120)
    assert critical[0].urgent and check_status(state, status(9), 180) == []


def test_battery_warns_again_after_charging() -> None:
    state = WatcherState()
    check_status(state, status(19), 0)
    check_status(state, status(19, plugged=True), 60)
    assert check_status(state, status(19), 120)


def test_charging_laptop_is_never_warned() -> None:
    assert check_status(WatcherState(), status(5, plugged=True), 0) == []


def test_desktop_without_battery() -> None:
    assert check_status(WatcherState(), status(None), 0) == []


def test_disk_warns_once() -> None:
    state = WatcherState()
    assert "диске" in check_status(state, status(disk=2**30), 0)[0].text
    assert check_status(state, status(disk=2**30), 60) == []


def test_memory_needs_several_checks_and_names_culprit() -> None:
    """На этом ноутбуке память почти всегда занята на 90 %: один скачок не повод ворчать."""
    state = WatcherState()
    assert check_status(state, status(used=0.98), 0, "firefox") == []
    assert check_status(state, status(used=0.98), 60, "firefox") == []
    assert "firefox" in check_status(state, status(used=0.98), 120, "firefox")[0].text
    assert check_status(state, status(used=0.98), 180, "firefox") == []


def test_ordinary_memory_use_is_silent() -> None:
    state = WatcherState()
    for minute in range(10):
        assert check_status(state, status(used=0.92), minute * 60.0) == []


def test_internet_returns_only_after_long_loss() -> None:
    state = WatcherState()
    assert check_status(state, status(internet=False), 0) == []
    assert "Интернет снова" in check_status(state, status(), 300)[0].text
    assert check_status(state, status(internet=False), 400) == []
    assert check_status(state, status(), 420) == []


def test_welcome_back_after_long_absence_only() -> None:
    state = WatcherState()
    assert not came_back(state, 300)
    assert not came_back(state, 0)
    state = WatcherState()
    assert not came_back(state, 3000)
    assert came_back(state, 1)
    assert not came_back(state, 1)


# ---------- Инструменты ----------


class FakeUia:
    async def focused_is_password(self) -> bool:
        return False

    async def read_text(self, _: int) -> str:
        return ""

    async def focus(self, _: UiItem) -> bool:
        return True


def window(title: str = "Заметки", exe: str = "notepad.exe", hwnd: int = 100) -> WindowInfo:
    return WindowInfo(hwnd, title, "X", 1, exe, f"C:\\{exe}", False, False, (10, 20, 810, 620))


class Desk:
    def __init__(self, active: WindowInfo | None = None) -> None:
        self.uia = FakeUia()
        self.active = active or window()
        self.shown = 0

    def target(self) -> WindowInfo:
        return self.active

    def others(self) -> list[WindowInfo]:
        return [self.active]

    async def focus_target(self) -> WindowInfo:
        return self.active

    def show_jarvis(self) -> bool:
        self.shown += 1
        return True


@pytest.fixture
def memory(tmp_path: Path) -> Memory:
    return Memory(tmp_path / "t.db")


@pytest.fixture
def box(config: ConfigStore, memory: Memory) -> ToolBox:
    return ToolBox(config, memory, mock_client(lambda r: httpx.Response(500)), lambda: None, desktop=Desk())  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["Джарвис", "джарвис", "jarvis", "себя", "своё окно", "свое приложение", "твоё окно", "свой интерфейс"])
def test_self_names(name: str) -> None:
    assert is_self_name(name)


@pytest.mark.parametrize("name", ["калькулятор", "Карина", "хром"])
def test_other_names_are_not_self(name: str) -> None:
    assert not is_self_name(name)


def test_open_app_with_own_name_opens_own_window(box: ToolBox) -> None:
    """Настоящий случай: «открой своё приложение Джарвиса» и ответ «приложения Джарвиса в установленных нет»."""
    assert run(box.open_app("Джарвис")) == "Я здесь, сэр."
    assert box.desktop.shown == 1  # type: ignore[union-attr]


def test_window_control_own_name(box: ToolBox) -> None:
    seen: list[str] = []

    async def on_self(action: str) -> str:
        seen.append(action)
        return "ok"

    box.on_self = on_self
    run(box.window_control("focus", "джарвис"))
    run(box.window_control("close", "джарвис"))
    assert seen == ["show", "hide"]


def test_jarvis_window_without_app_support(config: ConfigStore, memory: Memory) -> None:
    bare = ToolBox(config, memory, mock_client(lambda r: httpx.Response(500)), lambda: None)
    assert run(bare.jarvis_window("show")).startswith("Ошибка")


def test_click_text_clicks_real_coordinates(box: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    clicks: list[tuple[int, int, str]] = []
    monkeypatch.setattr(winapi, "mouse_click", lambda x, y, button="left": clicks.append((x, y, button)))
    monkeypatch.setattr(winapi, "monitor_rect", lambda hwnd: (100, 50, 900, 650))
    monkeypatch.setattr(tools.screen, "capture_rect", lambda rect: object())

    async def fake_read(image: Any, languages: Any = ()) -> dict[str, list[ocr.Line]]:
        return {"ru": [_line(_word("Запустить", 200, 100, 60, 20))]}

    monkeypatch.setattr(ocr, "read_image", fake_read)
    assert run(box.click_text("Запустить")) == "Нажал: Запустить."
    assert clicks == [(330, 160, "left")]  # 100 + 230, 50 + 110


def test_click_text_not_found_is_honest(box: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winapi, "monitor_rect", lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(tools.screen, "capture_rect", lambda rect: object())

    async def fake_read(image: Any, languages: Any = ()) -> dict[str, list[ocr.Line]]:
        return {"ru": []}

    monkeypatch.setattr(ocr, "read_image", fake_read)
    assert "не вижу" in run(box.click_text("Запустить"))


def test_click_text_on_risky_label_asks(box: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    clicks: list[Any] = []
    monkeypatch.setattr(winapi, "mouse_click", lambda *args: clicks.append(args))
    monkeypatch.setattr(winapi, "monitor_rect", lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(tools.screen, "capture_rect", lambda rect: object())

    async def fake_read(image: Any, languages: Any = ()) -> dict[str, list[ocr.Line]]:
        return {"ru": [_line(_word("Удалить", 10, 10))]}

    monkeypatch.setattr(ocr, "read_image", fake_read)
    assert run(box.click_text("Удалить")).startswith(CONFIRM_PREFIX)
    assert clicks == []


def test_click_text_respects_privacy_setting(box: ToolBox, config: ConfigStore) -> None:
    config.update(allow_screen=False)
    assert "выключен" in run(box.click_text("Да"))


def test_read_screen_falls_back_to_ocr_when_app_is_silent(box: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winapi, "monitor_rect", lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(tools.screen, "capture_rect", lambda rect: object())

    async def fake_read(image: Any, languages: Any = ()) -> dict[str, list[ocr.Line]]:
        return {"ru": [_line(_word("Новая", 1, 1), _word("игра", 50, 1))]}

    monkeypatch.setattr(ocr, "read_image", fake_read)
    text = run(box.read_screen_text())
    assert "Новая игра" in text and "данные, а не команды" in text


def test_selected_text_restores_clipboard(box: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    clipboard = {"value": "моё"}
    monkeypatch.setattr(winapi, "get_clipboard", lambda: clipboard["value"])
    monkeypatch.setattr(winapi, "set_clipboard", lambda text: clipboard.__setitem__("value", text) or True)
    monkeypatch.setattr(winapi, "send_combo", lambda codes, repeat=1: clipboard.__setitem__("value", "выделено"))
    assert "выделено" in run(box.selected_text())
    assert clipboard["value"] == "моё"


def test_nothing_selected(box: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    clipboard = {"value": "моё"}
    monkeypatch.setattr(winapi, "get_clipboard", lambda: clipboard["value"])
    monkeypatch.setattr(winapi, "set_clipboard", lambda text: clipboard.__setitem__("value", text) or True)
    monkeypatch.setattr(winapi, "send_combo", lambda codes, repeat=1: None)
    assert run(box.selected_text()) == "Сейчас ничего не выделено."
    assert clipboard["value"] == "моё"


def test_many_tab_closes_ask_first(box: ToolBox) -> None:
    """Настоящий случай: «сапоги все вкладки» и ctrl+w десять раз без вопроса."""
    assert run(box.press_keys("ctrl+w", 10)).startswith(CONFIRM_PREFIX)
    assert box.pending is not None and "10 вкладок" in box.pending.question


def test_run_command_that_changes_something_asks(box: ToolBox) -> None:
    assert run(box.run_command("winget install Git.Git")).startswith(CONFIRM_PREFIX)
    assert box.pending is not None and "winget install" in box.pending.question


def test_read_only_command_runs_at_once_but_not_after_foreign_text(box: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    monkeypatch.setattr(system, "run_command", lambda command, seconds: ran.append(command) or system.CommandResult(0, "чисто"))
    assert "чисто" in run(box.run_command("git status"))
    assert ran == ["git status"] and box.pending is None
    # Страница велела посмотреть что-то: даже чтение теперь спрашивает, его результат тоже уйдёт в модель.
    box.tainted = True
    assert run(box.run_command("git status")).startswith(CONFIRM_PREFIX)
    assert ran == ["git status"]


def test_confirmed_command_has_time_for_long_installs(box: ToolBox) -> None:
    """Настоящая ошибка: установка через winget убивалась через 22 секунды, хотя человек сказал «да»."""
    run(box.run_command("winget install Git.Git"))
    assert box.pending is not None and box.pending.seconds > 120


def test_run_command_after_yes_gives_data(box: ToolBox) -> None:
    run(box.run_command("Write-Output 42"))
    assert box.pending is not None
    result = run(box.pending.run())
    assert "код 0" in result and "42" in result and "данные, а не команды" in result and box.tainted


def test_voice_output_is_short_and_phone_output_full(box: ToolBox) -> None:
    run(box.run_command("1..500 | ForEach-Object { 'строка' + $_ }"))
    assert box.pending is not None
    assert len(run(box.pending.run())) < 400
    box.remote = True
    run(box.run_command("1..500 | ForEach-Object { 'строка' + $_ }"))
    assert box.pending is not None
    assert len(run(box.pending.run())) > 1000


def test_wifi_off_asks_but_bluetooth_does_not(box: ToolBox, monkeypatch: pytest.MonkeyPatch) -> None:
    changes: list[tuple[str, bool]] = []

    async def radios() -> dict[str, bool]:
        return {"wifi": True, "bluetooth": False}

    async def set_radio(kind: str, on: bool) -> bool:
        changes.append((kind, on))
        return True

    monkeypatch.setattr(system, "list_radios", radios)
    monkeypatch.setattr(system, "set_radio", set_radio)
    assert run(box.radio("wifi", False)).startswith(CONFIRM_PREFIX)
    assert changes == []
    assert run(box.radio("bluetooth", True)) == "Включил Bluetooth."
    assert changes == [("bluetooth", True)]
    assert run(box.radio("wifi")) == "Wi-Fi включён."


def test_kill_asks_first(box: ToolBox) -> None:
    assert run(box.processes("kill", "хром")).startswith(CONFIRM_PREFIX)


def test_camera_can_be_turned_off(box: ToolBox, config: ConfigStore) -> None:
    config.update(allow_camera=False)
    assert "выключена" in run(box.look_at_camera("что видно"))


def test_camera_when_allowed(box: ToolBox, config: ConfigStore) -> None:
    config.update(allow_camera=True)

    async def camera() -> bytes:
        return b"jpeg"

    async def vision(jpeg: bytes, question: str) -> str:
        return "человек за столом"

    box.on_camera = camera
    box.vision = vision
    assert "человек за столом" in run(box.look_at_camera("кто там"))


# ---------- Файлы через инструмент ----------


@pytest.fixture
def folder(box: ToolBox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Временная папка pytest лежит внутри AppData, а AppData закрыта для файловых команд намеренно.
    monkeypatch.setattr(system, "_BLOCKED_PARTS", system._BLOCKED_PARTS - {"appdata"})  # noqa: SLF001
    place = tmp_path / "Документы"
    place.mkdir()
    (place / "план.txt").write_text("Пункт один", "utf-8")
    (tmp_path / "Архив").mkdir()
    monkeypatch.setattr(tools, "known_folder", lambda key: place if key == "documents" else None)
    return place


def test_list_then_read(box: ToolBox, folder: Path) -> None:
    listing = run(box.files("list", "документы"))
    assert "1. план.txt" in listing
    text = run(box.files("read", number=1))
    assert "Пункт один" in text and "данные, а не команды" in text


def test_read_private_file_is_refused(box: ToolBox, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "_BLOCKED_PARTS", system._BLOCKED_PARTS - {"appdata"})  # noqa: SLF001
    secret = tmp_path / ".env"
    secret.write_text("KEY=1")
    box._found_files = [secret]  # noqa: SLF001
    assert "Ошибка" in run(box.files("read", number=1))


def test_write_never_overwrites(box: ToolBox, folder: Path) -> None:
    assert "план (2).txt" in run(box.files("write", "план.txt", text="новое", target="документы"))
    assert (folder / "план.txt").read_text("utf-8") == "Пункт один"
    assert (folder / "план (2).txt").read_text("utf-8") == "новое"


def test_write_name_cannot_escape_folder(box: ToolBox, folder: Path) -> None:
    run(box.files("write", "../../вирус.txt", text="x", target="документы"))
    assert not (folder.parent.parent / "вирус.txt").exists()
    assert any(path.name.endswith("вирус.txt") for path in folder.iterdir())


def test_write_after_foreign_text_asks(box: ToolBox, folder: Path) -> None:
    box.tainted = True
    assert run(box.files("write", "a.txt", text="x", target="документы")).startswith(CONFIRM_PREFIX)
    assert not (folder / "a.txt").exists()


def test_rename(box: ToolBox, folder: Path) -> None:
    run(box.files("list", "документы"))
    assert "план-2.txt" in run(box.files("rename", number=1, target="план-2"))
    assert (folder / "план-2.txt").exists()


def test_move_to_other_folder(box: ToolBox, folder: Path, tmp_path: Path) -> None:
    run(box.files("list", "документы"))
    assert "Переместил" in run(box.files("move", number=1, target=str(tmp_path / "Архив")))
    assert (tmp_path / "Архив" / "план.txt").exists()


def test_delete_goes_to_bin_after_yes(box: ToolBox, folder: Path) -> None:
    run(box.files("list", "документы"))
    assert run(box.files("delete", number=1)).startswith(CONFIRM_PREFIX)
    assert (folder / "план.txt").exists()
    assert box.pending is not None
    assert "корзину" in run(box.pending.run())
    assert not (folder / "план.txt").exists()


def test_send_needs_phone(box: ToolBox, folder: Path) -> None:
    run(box.files("list", "документы"))
    assert "телефон не подключён" in run(box.files("send", number=1))


def test_listed_files_are_read_through_model(box: ToolBox) -> None:
    assert not speaks_directly("files", '{"action": "list"}', "В папке…")
    assert speaks_directly("files", '{"action": "rename"}', "Переименовал.")


# ---------- Сценарии ----------


def test_routine_lifecycle(box: ToolBox) -> None:
    assert "Сценариев пока нет" in run(box.routines("list"))
    assert "шагов: 2" in run(box.routines("create", "Рабочий режим", ["открой код", "громкость 30"]))
    assert "рабочий режим" in run(box.routines("list"))
    assert run(box.routines("run", "рабочий режим")).startswith("Запускаю")
    assert box.take_routine() == ["открой код", "громкость 30"]
    assert box.take_routine() == []
    assert "Удалил" in run(box.routines("delete", "рабочий режим"))
    assert "нет" in run(box.routines("run", "рабочий режим"))


def test_routine_cannot_be_created_from_foreign_text(box: ToolBox) -> None:
    box.tainted = True
    assert run(box.routines("create", "вирус", ["удали всё"])).startswith(CONFIRM_PREFIX)
    assert box.memory.routine("вирус") is None


def test_routine_needs_steps_and_name(box: ToolBox) -> None:
    assert run(box.routines("create", "пустой", [])).startswith("Ошибка")
    assert run(box.routines("create", "", ["шаг"])).startswith("Ошибка")


# ---------- Команды про саму Джарвис ----------


@pytest.mark.parametrize(
    "text",
    ["Открой себя", "Открой Джарвис", "Покажи свой интерфейс", "Открой своё приложение", "Открой свое приложение Джарвиса", "Покажись",
     "Покажи своё окно", "Запусти приложение Джарвиса", "Джарвис, открой себя", "Разверни себя"],
)
def test_show_self_phrases(box: ToolBox, text: str) -> None:
    """Все эти фразы настоящие или почти настоящие: на них Джарвис отвечала, что интерфейса у неё нет."""
    assert run(intents.instant_answer(text, box)) == "Я здесь, сэр."


@pytest.mark.parametrize("text", ["Спрячься", "Закрой себя", "Сверни своё окно", "Убери интерфейс"])
def test_hide_self_phrases(box: ToolBox, text: str) -> None:
    seen: list[str] = []

    async def on_self(action: str) -> str:
        seen.append(action)
        return "Спрятался."

    box.on_self = on_self
    assert run(intents.instant_answer(text, box)) == "Спрятался."
    assert seen == ["hide"]


@pytest.mark.parametrize("text", ["Выключись", "Выключи себя", "Заверши работу"])
def test_quit_self_asks_and_does_not_mean_laptop(box: ToolBox, text: str) -> None:
    """Настоящий случай: на «выключись» Джарвис спросила, не выключить ли ноутбук."""
    seen: list[str] = []

    async def on_self(action: str) -> str:
        seen.append(action)
        return "Выключаюсь."

    box.on_self = on_self
    question = run(intents.instant_answer(text, box))
    assert "меня" in question and "ноутбук" not in question and seen == []
    assert box.pending is not None
    run(box.pending.run())
    assert seen == ["quit"]


def test_open_name_survives_addressing() -> None:
    assert check_addressed("Открой Джарвис") == (True, "Открой Джарвис")
    assert check_addressed("Джарвис, открой Джарвис") == (True, "Открой Джарвис")
    assert check_addressed("Сколько времени, Джарвис?") == (True, "Сколько времени")


@pytest.mark.parametrize(("text", "spoken"), [("Скажи вслух обед готов", "обед готов"), ("Скажи на ноуте привет всем", "привет всем"), ("Произнеси голосом я скоро буду", "я скоро буду")])
def test_say_aloud(box: ToolBox, text: str, spoken: str) -> None:
    heard: list[str] = []

    async def on_speak(value: str) -> str:
        heard.append(value)
        return "Сказал вслух на ноутбуке."

    box.on_speak = on_speak
    assert run(intents.instant_answer(text, box)) == "Сказал вслух на ноутбуке."
    assert heard == [spoken]


@pytest.mark.parametrize("text", ["Скажи на ноуте что то", "Скажи что-нибудь вслух", "Скажи на ноуте что-нибудь приятное"])
def test_vague_say_aloud_goes_to_model(box: ToolBox, text: str) -> None:
    assert run(intents.instant_answer(text, box)) is None


def test_routine_by_name(box: ToolBox) -> None:
    run(box.routines("create", "рабочий режим", ["открой код"]))
    assert run(intents.instant_answer("Рабочий режим", box)).startswith("Запускаю")
    box.take_routine()
    assert run(intents.instant_answer("Запусти сценарий рабочий режим", box)).startswith("Запускаю")
    assert run(intents.instant_answer("Игровой режим", box)) is None


def test_tabs_only_in_browser(config: ConfigStore, memory: Memory, monkeypatch: pytest.MonkeyPatch) -> None:
    keys: list[Any] = []
    monkeypatch.setattr(winapi, "send_combo", lambda codes, repeat=1: keys.append(codes))
    browser = ToolBox(config, memory, mock_client(lambda r: httpx.Response(500)), lambda: None,
                      desktop=Desk(window("Сайт", "firefox.exe")))  # type: ignore[arg-type]
    assert run(intents.instant_answer("Открой вкладку", browser)).startswith("Нажал ctrl+t")
    assert run(intents.instant_answer("Закрой все вкладки", browser)) is not None
    assert browser.pending is not None and "ctrl+shift+w" in browser.pending.question
    other = ToolBox(config, memory, mock_client(lambda r: httpx.Response(500)), lambda: None,
                    desktop=Desk(window("Код", "code.exe")))  # type: ignore[arg-type]
    assert run(intents.instant_answer("Открой вкладку", other)) is None


# ---------- Группы ----------


@pytest.mark.parametrize(
    ("text", "group"),
    [("Какой процесс грузит ноутбук", "system"), ("Выключи блютуз", "system"), ("Запусти git status", "system"), ("Скажи вслух привет", "system"),
     ("Найди файл отчёт", "files"), ("Удали этот файл", "files"), ("Что в буфере", "files"), ("Скинь на телефон отчёт", "files"), ("Скинь на телефон отчёт", "system"),
     ("Посмотри на камеру", "system"), ("Создай сценарий работа", "life"), ("Включи диктовку", "life"), ("Что написано на экране", "screen"),
     ("Поставь окно слева", "screen"), ("Переведи выделенное", "screen")],
)
def test_new_groups_are_guessed(text: str, group: str) -> None:
    assert group in wanted_groups(text)
