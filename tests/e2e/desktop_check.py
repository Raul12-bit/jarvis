"""Настоящая проверка рук Джарвис на настоящем Edge, в отдельном профиле и в отдельных окнах.

Открывает своё окно браузера, читает страницу через UI Automation, нажимает, печатает, ставит на паузу.
Чужие окна не трогает: перед вводом с клавиатуры ждёт, пока человек перестанет пользоваться компьютером,
и проверяет, что на переднем плане именно окно проверки. Снимок экрана для зрения делается только с этого окна.

Запуск: python tests/e2e/desktop_check.py <рабочая_папка> [--vision]
Ключ для зрения берётся из GROQ_API_KEY.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
WORK = Path(sys.argv[1]).resolve()
os.environ["JARVIS_HOME"] = str(WORK / "desktop-home")

from jarvis import screen, sysaudio, winapi  # noqa: E402
from jarvis.config import ConfigStore  # noqa: E402
from jarvis.desktop import Desktop, pick_videos  # noqa: E402
from jarvis.media import MediaSessions  # noqa: E402
from jarvis.memory import Memory  # noqa: E402
from jarvis.tools import CONFIRM_PREFIX, ToolBox  # noqa: E402
from jarvis.uia import UiaClient  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
FIREFOX = r"C:\Program Files\Mozilla Firefox\firefox.exe"
USE_FIREFOX = "--firefox" in sys.argv
BROWSER_EXE = "firefox.exe" if USE_FIREFOX else "msedge.exe"
BROWSER_APP = "308046" if USE_FIREFOX else "msedge"
PROFILE = WORK / "desktop-profile"
YOUTUBE = "https://www.youtube.com/results?search_query=%D0%B0%D0%BB%D0%BC%D0%B0%D1%82%D1%8B"
PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Проверка Джарвис</title></head><body style="font:20px sans-serif">
<h1>Проверочная страница Джарвиса</h1><p id="text">Секретное слово на странице: кумыс. Этот абзац должен прочитать инструмент.</p>
<a id="go" href="#clicked" onclick="document.title='Нажато'; return false;">Обычная ссылка</a><br><br>
<button onclick="document.title='Кнопка сработала'">Нажми меня</button><br><br>
<label>Поле <input id="field" oninput="document.title='Ввод: '+this.value" style="font-size:20px"></label><br><br>
<label>Пароль <input id="pwd" type="password"></label>
<script>window.addEventListener('keydown',e=>{if(e.key==='F9')document.title='Клавиша F9'})</script>
</body></html>"""
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(("OK   " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""), flush=True)


def edge(url: str) -> subprocess.Popen:
    if USE_FIREFOX:
        PROFILE.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen([FIREFOX, "-no-remote", "-profile", str(PROFILE), "-new-window", url])
    return subprocess.Popen([EDGE, f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
                             "--disable-sync", "--window-size=1280,860", "--new-window", url])


def find_window(needle: str, timeout: float = 25) -> winapi.WindowInfo | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in winapi.top_windows():
            # Окна человека с тем же браузером отличаем по профилю процесса.
            if window.exe == BROWSER_EXE and needle in window.title.lower() and _is_test_process(window.pid):
                return window
        time.sleep(0.25)
    return None


def _is_test_process(pid: int) -> bool:
    try:
        return any("desktop-profile" in part for part in psutil.Process(pid).cmdline())
    except psutil.Error:
        return False


def title_of(window: winapi.WindowInfo) -> str:
    info = winapi.window_info(window.hwnd)
    return info.title if info else ""


async def wait_title(window: winapi.WindowInfo, needle: str, timeout: float = 5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in title_of(window):
            return True
        await asyncio.sleep(0.1)
    return False


async def wait_user_idle(seconds: float = 8, timeout: float = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if winapi.idle_seconds() >= seconds:
            return True
        await asyncio.sleep(0.5)
    return False


class Only:
    """Подменяет выбор окна: инструменты работают только с окном проверки, а не с верхним окном человека."""

    def __init__(self, desktop: Desktop, window: winapi.WindowInfo) -> None:
        desktop.target = lambda: winapi.window_info(window.hwnd)  # type: ignore[method-assign]
        desktop.target_browser = lambda: winapi.window_info(window.hwnd)  # type: ignore[method-assign]
        # Иначе «включи второе видео» и «открой ссылку» ушли бы в браузеры человека.
        desktop.browsers = lambda: [info] if (info := winapi.window_info(window.hwnd)) else []  # type: ignore[method-assign]


async def page_checks(box: ToolBox, desktop: Desktop, uia: UiaClient) -> None:
    html = Path(tempfile.gettempdir()) / "jarvis_check_page.html"
    html.write_text(PAGE, encoding="utf-8")
    edge(html.as_uri())
    window = find_window("проверка джарвис")
    check("Окно проверки открылось", window is not None)
    if window is None:
        return
    Only(desktop, window)

    # Чтение текста страницы.
    text = await box.run("read_screen_text")
    check("Чтение текста страницы через UI Automation", "кумыс" in text, text[:120].replace("\n", " "))
    check("Прочитанный текст помечен как данные и не доверенный", box.tainted and "данные, а не команды" in text)
    box.begin_turn()

    # Список элементов.
    items = await box.run("screen_items")
    print(items, flush=True)
    check("Элементы страницы перечислены", "Нажми меня" in items and "Обычная ссылка" in items)
    if "Адресная строка" in items:
        check("Сначала идёт страница, потом кнопки браузера", items.index("Нажми меня") < items.index("Адресная строка"))
    box.begin_turn()
    lines = items.splitlines()
    number = next(int(line.split(".")[0]) for line in lines if "Нажми меня" in line)
    started = time.perf_counter()
    result = await box.run("click_item", number=number)
    check("Кнопка нажата без мыши", await wait_title(window, "Кнопка сработала"), f"{result} за {time.perf_counter() - started:.2f} с")

    # Нажатие по ссылке через тот же путь.
    await box.run("screen_items")
    box.begin_turn()
    fresh = await desktop.list_items("link", "Обычная")
    await desktop.click_item(fresh[1][0])
    check("Ссылка нажата без мыши", await wait_title(window, "Нажато"))

    # Клавиатура: только когда человек не печатает и окно действительно наше.
    if await wait_user_idle():
        activated = await asyncio.to_thread(winapi.activate, window.hwnd)
        foreground = winapi.foreground()
        check("Окно выводится на передний план из фона", activated and foreground is not None and foreground.hwnd == window.hwnd)
        if activated:
            fields = await desktop.list_items("input", "Поле")
            await uia.focus(fresh[1][0] if not fields[1] else fields[1][0])
            if winapi.foreground() is not None and winapi.foreground().hwnd == window.hwnd:
                result = await box.run("type_text", text="Привет, Алматы 123")
                check("Текст напечатан любой раскладкой", await wait_title(window, "Привет, Алматы 123"), f"{result}; заголовок: {title_of(window)[:60]}")
            if winapi.foreground() is not None and winapi.foreground().hwnd == window.hwnd:
                await box.run("press_keys", keys="f9")
                check("Нажата клавиша", await wait_title(window, "Клавиша F9"), title_of(window)[:50])
            passwords = await desktop.list_items("input", "Пароль")
            if passwords[1]:
                check("Поле пароля распознано", passwords[1][0].password)
                await uia.focus(passwords[1][0])
                await asyncio.sleep(0.2)
                refused = await box.run("type_text", text="секрет")
                check("В поле пароля печатать отказывается", "паролей" in refused, refused)
    else:
        check("Клавиатура (пропущено: человек за компьютером)", True, "ввод не отправлялся, чтобы не помешать")

    # Снимок окна.
    image = await asyncio.to_thread(screen.capture_window, winapi.window_info(window.hwnd))
    check("Снимок окна через PrintWindow", image is not None and not screen.is_blank(image), str(image.size if image else None))
    if image is not None:
        jpeg = await asyncio.to_thread(screen.to_jpeg, image)
        (WORK / "desktop_window.jpg").write_bytes(jpeg)
        check("Снимок сжат для отправки", 5_000 < len(jpeg) < 2_000_000, f"{len(jpeg) // 1024} КБ")
        if "--vision" in sys.argv and box.vision is not None:
            started = time.perf_counter()
            answer = await box.vision(jpeg, "Какое секретное слово написано на странице? Ответь одним словом.")
            check("Настоящая модель зрения прочла снимок", "кумыс" in answer.lower(), f"{answer!r} за {time.perf_counter() - started:.2f} с")

    # Управление окном. Без имени действие идёт к окну проверки, а не к окнам человека.
    box.begin_turn()
    minimized = await box.run("window_control", action="minimize")
    await asyncio.sleep(0.4)
    check("Окно свернуто", winapi.window_info(window.hwnd).minimized, minimized)  # type: ignore[union-attr]
    await asyncio.to_thread(winapi.restore, window.hwnd)
    await asyncio.sleep(0.4)
    check("Окно возвращено", not winapi.window_info(window.hwnd).minimized)  # type: ignore[union-attr]
    # Браузер это рабочая программа: сам Джарвис его не закрывает, а по просьбе сначала переспрашивает.
    closed = await box.run("window_control", action="close")
    await asyncio.sleep(0.5)
    check("Закрытие браузера требует подтверждения", closed.startswith(CONFIRM_PREFIX) and winapi.window_exists(window.hwnd), closed[:70])
    if box.pending is not None:
        closed = await box.pending.run()
    await asyncio.sleep(0.8)
    check("После «да» окно закрыто вежливо", not winapi.window_exists(window.hwnd), closed)


async def video_checks(box: ToolBox, desktop: Desktop, media: MediaSessions) -> None:
    edge(YOUTUBE)
    window = find_window("youtube")
    check("YouTube открылся", window is not None)
    if window is None:
        return
    Only(desktop, window)
    await asyncio.sleep(4)
    started = time.perf_counter()
    _, found = await desktop.videos()
    check("Видео найдены на настоящей странице YouTube", len(found) >= 3, f"{len(found)} шт. за {time.perf_counter() - started:.2f} с: {[v.title[:30] for v in found[:3]]}")
    if len(found) < 2:
        return
    second = found[1]
    started = time.perf_counter()
    result = await box.run("play_video", position=2)
    spent = time.perf_counter() - started
    check("«Включи второе видео» открыло именно второе", second.url.split("v=")[1].split("&")[0] in await _address(desktop, window),
          f"{result} за {spent:.2f} с")
    check("Команда заняла меньше 5 с", spent < 5, f"{spent:.2f} с")

    # Идёт ли видео и слушается ли явной паузы. Сначала ждём, пока запись сменится на открытое видео:
    # на странице выдачи под курсором могло играть превью совсем другого ролика.
    wanted = second.title[:15].lower()
    playing = None
    deadline = time.time() + 15
    while time.time() < deadline:
        sessions = await media.current()
        mine = [s for s in sessions if s.app.lower().startswith(BROWSER_APP) and s.playing and wanted in s.title.lower()]
        if mine:
            playing = mine[0]
            break
        await asyncio.sleep(0.5)
    if playing is None:
        # Перед роликом YouTube часто показывает рекламу: тогда в медиасессии её заголовок, а не заголовок ролика.
        started_ads = [s for s in await media.current() if s.app.lower().startswith(BROWSER_APP) and s.playing]
        playing = started_ads[0] if started_ads else None
    check("Открытое видео реально заиграло (медиасессия Windows)", playing is not None, str(playing or await media.current()))
    if playing is None:
        return
    check("Видео опознано как источник видео", playing.is_video_source)
    # Этот запуск работает при открытых окнах человека: останавливаем только когда больше ничего не играет.
    others = [s for s in await media.current() if s.playing and wanted not in s.title.lower()]
    if others:
        check("Пауза (пропущено: у человека играет что-то своё)", True, str(len(others)))
    else:
        paused = await media.pause_all(only_video=True)
        stopped = await _until(lambda v: v is False, media.is_video_playing)
        check("Явная пауза остановил видео", paused >= 1 and stopped, f"остановлено {paused}")
        resumed = await media.resume()
        running = await _until(lambda v: v is True, media.is_video_playing)
        check("Воспроизведение продолжено", resumed and running)
        await media.pause_all(only_video=True)

    now = winapi.window_info(window.hwnd)
    from jarvis.activity import classify

    check("Контроль времени считает это видео", now is not None and classify(now) == "video", now.title[:60] if now else "")
    second_look = await desktop.uia.snapshot(window.hwnd)
    check("На странице видео снова есть что выбрать", len(pick_videos(second_look)) >= 1, str(len(pick_videos(second_look))))
    winapi.close_window(window.hwnd)


async def _until(predicate, probe, timeout: float = 6) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(await probe()):
            return True
        await asyncio.sleep(0.3)
    return False


async def _address(desktop: Desktop, window: winapi.WindowInfo) -> str:
    deadline = time.time() + 6
    address = ""
    while time.time() < deadline:
        items = await desktop.uia.snapshot(window.hwnd, False)
        # У Edge адресная строка это поле ввода, у Firefox выпадающий список.
        address = next((i.value for i in items if i.role in ("поле ввода", "выпадающий список") and "youtube" in i.value), "")
        if "watch" in address:
            return address
        await asyncio.sleep(0.3)
    return address


async def volume_check(uia: UiaClient) -> None:
    # Только чтение и запись того же значения: громкость человека не меняется.
    state = await uia.run(sysaudio.read_output_state)
    check("Громкость читается через COM", state is not None, str(state))
    if state is not None:
        final = await uia.run(sysaudio.set_volume, state[0])
        check("Запись той же громкости не меняет её", final == state[0], str(final))
        if state[1]:
            await uia.run(sysaudio.set_mute, True)  # set_volume снимает выключенный звук: возвращаем как было
        after = await uia.run(sysaudio.read_output_state)
        check("Состояние звука осталось прежним", after == state, str(after))


async def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    config = ConfigStore()
    memory = Memory(config.home / "t.db")
    uia = UiaClient()
    desktop = Desktop(uia)
    media = MediaSessions()
    import httpx

    http = httpx.AsyncClient(timeout=15)
    vision = None
    if "--vision" in sys.argv and os.environ.get("GROQ_API_KEY"):
        from jarvis.llm import LLMRouter

        vision = LLMRouter(config, http).vision
    box = ToolBox(config, memory, http, lambda: None, desktop=desktop, media=media, vision=vision)
    baseline = psutil.Process()
    try:
        started = time.perf_counter()
        windows = winapi.top_windows()
        check("Окна перечислены", len(windows) >= 1, f"{len(windows)} окон за {(time.perf_counter() - started) * 1000:.1f} мс")
        await volume_check(uia)
        await page_checks(box, desktop, uia)
        await video_checks(box, desktop, media)
        check("Память процесса проверки", True, f"{baseline.memory_info().rss // 2**20} МБ RSS")
    finally:
        for proc in psutil.process_iter(["cmdline"]):
            try:
                if any("desktop-profile" in part for part in proc.info["cmdline"] or []):
                    proc.kill()
            except psutil.Error:
                pass
        uia.close()
        await http.aclose()
        memory.close()
    failed = [name for name, ok, _ in results if not ok]
    (WORK / "desktop_report.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nИТОГ: {len(results) - len(failed)} из {len(results)}" + (f", провалено: {failed}" if failed else ""), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.exit(asyncio.run(main()))
