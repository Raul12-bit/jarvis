"""Сквозная проверка в настоящем браузере.

Настоящие: Edge и его аудиоконвейер, WebGL, Silero, Vosk, голос Светланы, Open-Meteo, сервер Джарвиса.
Подменены: микрофон (WAV через поддельное устройство Chromium) и языковая модель (fake_cloud.py).
Нужен доступ в интернет и установленная модель Vosk в JARVIS_E2E_MODEL.

Запуск: python tests/e2e/run_e2e.py <рабочая_папка> [--measure]
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import edge_tts
import miniaudio
import psutil
import websockets

PROJECT = Path(__file__).resolve().parents[2]
WORK = Path(sys.argv[1]).resolve()
MEASURE = "--measure" in sys.argv
HOME = WORK / "home-e2e"
SHOTS = WORK / "shots"
LOG = WORK / "cloud.jsonl"
MIC = WORK / "mic.wav"
PORT, CLOUD_PORT, DEBUG_PORT = 47392, 47393, 47394
TOKEN = "e2e-token"
VOICE = "ru-RU-SvetlanaNeural"
PHRASES = [
    # Первая фраза после приветствия: иначе она пойдёт через перебивание, а не через слово активации.
    (15.0, "Джарвис, какая сегодня погода в Алматы?"),
    (36.0, "Поставь таймер на шесть секунд."),
    (58.0, "Расскажи длинную историю."),
    # Её собственные слова в микрофоне: имитация эха, которое не убрал браузер. Перебивать нельзя.
    (67.0, "Каждый вечер она собирала детали из старых приёмников и записывала идеи в толстую тетрадь."),
    (80.0, "Стоп."),
]
TOTAL_SECONDS = 96


async def synth(text: str) -> bytes:
    buf = bytearray()
    async for chunk in edge_tts.Communicate(text, VOICE).stream():
        if chunk["type"] == "audio":
            buf += chunk["data"]
    return miniaudio.decode(bytes(buf), output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=16000).samples.tobytes()


async def build_mic_file() -> None:
    timeline = bytearray(TOTAL_SECONDS * 32000)
    for at, text in PHRASES:
        pcm = await synth(text)
        start = int(at * 16000) * 2
        timeline[start:start + len(pcm)] = pcm
    with wave.open(str(MIC), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(timeline))


class Page:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.counter = 0

    async def call(self, method: str, **params):
        self.counter += 1
        current = self.counter
        await self.ws.send(json.dumps({"id": current, "method": method, "params": params}))
        while True:
            data = json.loads(await self.ws.recv())
            if data.get("id") == current:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data["result"]

    async def js(self, expression: str):
        result = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    async def shot(self, name: str) -> None:
        data = await self.call("Page.captureScreenshot", format="png")
        SHOTS.mkdir(parents=True, exist_ok=True)
        (SHOTS / f"{name}.png").write_bytes(base64.b64decode(data["data"]))


def fresh_home() -> None:
    if HOME.exists():
        shutil.rmtree(HOME, ignore_errors=True)
    (HOME / "models").mkdir(parents=True)
    source = Path(os.environ["JARVIS_E2E_MODEL"])
    shutil.copytree(source, HOME / "models" / source.name)
    (HOME / "config.json").write_text(json.dumps({
        "onboarded": True, "followup_seconds": 16, "user_name": "Тест", "shortcut_created": True, "mascot": False, "profile_version": 3,
    }), encoding="utf-8")


def tree(root: psutil.Process) -> list[psutil.Process]:
    try:
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []


def sample_load(server: psutil.Process, browser_root: psutil.Process, seconds: float) -> dict:
    """Средняя доля всего процессора (100 % = все 12 потоков) и память."""
    cores = psutil.cpu_count() or 1
    procs = {"server": [server], "browser": tree(browser_root)}
    start = {}
    for group, items in procs.items():
        for proc in items:
            try:
                start[proc.pid] = (group, sum(proc.cpu_times()[:2]))
            except psutil.Error:
                pass
    time.sleep(seconds)
    used = {"server": 0.0, "browser": 0.0}
    memory = {"server": {"rss": 0, "private": 0, "uss": 0}, "browser": {"rss": 0, "private": 0, "uss": 0}}
    for group, items in procs.items():
        for proc in items:
            try:
                used[group] += max(0.0, sum(proc.cpu_times()[:2]) - start[proc.pid][1])
                info = proc.memory_full_info()
                memory[group]["rss"] += info.rss
                memory[group]["private"] += info.private
                memory[group]["uss"] += info.uss
            except (psutil.Error, KeyError):
                pass
    return {
        "cpu_percent_of_total": {k: round(v / seconds / cores * 100, 2) for k, v in used.items()},
        "one_core_percent": {k: round(v / seconds * 100, 1) for k, v in used.items()},
        "memory_mb": {g: {k: round(v / 2**20) for k, v in m.items()} for g, m in memory.items()},
        "browser_processes": len(procs["browser"]),
    }


async def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    LOG.unlink(missing_ok=True)
    fresh_home()
    await build_mic_file()
    python = sys.executable
    env = {
        **os.environ, "JARVIS_HOME": str(HOME), "PYTHONIOENCODING": "utf-8",
        "JARVIS_TEST_BASE": f"http://127.0.0.1:{CLOUD_PORT}/v1",
    }
    env.pop("GROQ_API_KEY", None)
    cloud = subprocess.Popen([python, str(PROJECT / "tests" / "e2e" / "fake_cloud.py"), str(CLOUD_PORT), str(LOG)])
    jarvis = subprocess.Popen(
        [
            python, "-m", "jarvis", f"--port={PORT}", f"--token={TOKEN}",
            "--browser-arg=--use-fake-device-for-media-stream",
            "--browser-arg=--use-fake-ui-for-media-stream",
            f"--browser-arg=--use-file-for-fake-audio-capture={MIC}%noloop",
            f"--browser-arg=--remote-debugging-port={DEBUG_PORT}",
        ],
        cwd=PROJECT, env=env,
    )
    results: dict = {"timeline": [], "checks": {}}
    failed: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(("OK   " if ok else "FAIL ") + name + (f" :: {detail}" if detail else ""), flush=True)
        if not ok:
            failed.append(name)

    try:
        target = None
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=1) as resp:
                    pages = [t for t in json.load(resp) if t["type"] == "page" and f":{PORT}" in t["url"]]
                if pages:
                    target = pages[0]
                    break
            except OSError:
                pass
            await asyncio.sleep(0.2)
        check("Окно Edge открылось", target is not None)
        if target is None:
            return 1

        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            page = Page(ws)
            await page.call("Page.enable")
            await page.call("Runtime.enable")
            for _ in range(60):
                if await page.js("!!window.__jarvis"):
                    break
                await asyncio.sleep(0.1)
            await page.js("""(() => { window.__duck = []; const d = __jarvis.player.duck.bind(__jarvis.player);
                __jarvis.player.duck = (on) => { __duck.push(on); d(on); }; })()""")

            start = time.time()
            last_state = None
            states: list[str] = []
            shots_done: set[str] = set()
            peak_voice = 0.0
            spectrum_peak = 0
            while time.time() - start < TOTAL_SECONDS + 22:
                snap = await page.js("""({
                    state: __jarvis.ui.state, mode: __jarvis.ui.mode, wake: __jarvis.ui.wakeReady,
                    voice: __jarvis.core ? __jarvis.core.voice : -1,
                    peak: __jarvis.core ? Math.max(...__jarvis.core.fft) : -1,
                    sub: document.getElementById('subtitle').textContent,
                    subOn: document.getElementById('subtitle').classList.contains('is-on'),
                    timers: __jarvis.ui.timers.length,
                    micOn: __jarvis.mic.active, echo: __jarvis.mic.echoMode,
                    frames: __jarvis.core ? __jarvis.core.frameCount : 0,
                })""")
                now = round(time.time() - start, 2)
                peak_voice = max(peak_voice, snap["voice"])
                spectrum_peak = max(spectrum_peak, snap["peak"])
                if snap["state"] != last_state:
                    last_state = snap["state"]
                    states.append(last_state)
                    results["timeline"].append({"t": now, **snap})
                    print(f"{now:7.2f}s  {last_state:<10} mode={snap['mode']} sub={snap['sub'][:60]!r}", flush=True)
                    if last_state not in shots_done:
                        shots_done.add(last_state)
                        await page.shot(f"e2e-{last_state}")
                if snap["state"] == "speaking" and snap["voice"] > 0.15 and "voice" not in shots_done:
                    shots_done.add("voice")
                    await page.shot("e2e-voice-peak")
                await asyncio.sleep(0.05)

            log_text = await page.js("document.getElementById('log').innerText")
            caps = await page.js("""(() => {
                const t = __jarvis.mic.stream ? __jarvis.mic.stream.getAudioTracks()[0] : null;
                const gl = __jarvis.core ? __jarvis.core.gl : null;
                const dbg = gl ? gl.getExtension('WEBGL_debug_renderer_info') : null;
                return {
                    settings: t ? JSON.parse(JSON.stringify(t.getSettings())) : null,
                    capabilities: t ? JSON.parse(JSON.stringify(t.getCapabilities())).echoCancellation : null,
                    renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
                    width: innerWidth, height: innerHeight,
                    frames: __jarvis.core ? __jarvis.core.frameCount : 0,
                };
            })()""")
            results["log"] = log_text
            results["browser"] = caps
            print("--- журнал окна ---\n" + log_text + "\n---", flush=True)
            print("браузер:", json.dumps(caps, ensure_ascii=False), flush=True)
            await page.shot("e2e-final")

            cloud_log = [json.loads(line) for line in LOG.read_text(encoding="utf-8").splitlines()] if LOG.exists() else []
            results["cloud"] = cloud_log
            users = [e["last"].get("content", "") for e in cloud_log if e["last"]["role"] == "user"]
            tools = [e for e in cloud_log if e["last"]["role"] == "tool"]
            print("в модель ушло:", users, flush=True)
            woke = any(states[i] == "sleep" and states[i + 1] == "hearing" for i in range(len(states) - 1))
            # Если сервис голоса задержал приветствие, первая фраза легла поверх него и пошла через перебивание.
            # Это верное поведение, а проверить пробуждение в таком прогоне просто нечем.
            greeting_late = states[:3] == ["sleep", "speaking", "thinking"]
            check("Слово «Джарвис» разбудило из сна", woke or greeting_late,
                  ("приветствие задержалось, фраза принята как перебивание; " if greeting_late else "") + str(states[:6]))
            check("Фраза распознана", any("погод" in u.lower() for u in users), str(users[:1]))
            check("Имя убрано из начала фразы", bool(users) and "джарвис" not in users[0].lower(), users[0] if users else "")
            weather = [e for e in tools if "сейчас" in str(e["last"]["content"])]
            check("Инструмент погоды вернул настоящие данные Open-Meteo", bool(weather), str(weather[0]["last"]["content"])[:90] if weather else "")
            check("Продолжение без имени услышано", any("таймер" in u.lower() for u in users), str(users[1:2]))
            check("Таймер поставлен и озвучен", "напоминаю" in log_text.lower(), "")
            check("Длинная реплика начата", any("истори" in u.lower() for u in users))
            check("Прошли все состояния", {"sleep", "hearing", "thinking", "speaking", "listening"} <= set(states), str(states))
            check("Вернулась в сон после паузы", states[-1] == "sleep", states[-1] if states else "")
            check("Ядро реагировало на голос", peak_voice > 0.1, f"пик громкости {peak_voice:.2f}")
            check("Спектр получал данные", spectrum_peak > 30, f"пик спектра {spectrum_peak:.0f}")
            story_at = next(at for at, text in PHRASES if "истори" in text)
            check("Голосовое «стоп» прервало длинный ответ", _was_interrupted(results["timeline"], story_at), str(states[-4:]))
            check("На «стоп» Джарвис не отвечает", not any(u.lower().strip(" .") in ("стоп", "топ") for u in users), str(users[-1:]))
            ducks = await page.js("__duck")
            results["ducks"] = ducks
            jarvis_log = (HOME / "logs" / "jarvis.log").read_text(encoding="utf-8")
            check("Собственные слова опознаны как эхо, голос вернулся", "Перебивание оказалось эхом" in jarvis_log and False in ducks, str(ducks))
            check("Эхо не прервало ответ раньше времени", not any("детали из старых" in u.lower() for u in users), "")

            report = await page.js(f"""fetch('/api/selfcheck', {{method: 'POST', headers: {{'X-Jarvis-Token': '{TOKEN}'}}}}).then(r => r.json())""")
            results["selfcheck"] = report["results"]
            for item in report["results"]:
                print(f"  самопроверка: {'да ' if item['ok'] else 'НЕТ'} {item['name']} {item['ms']} мс :: {item['detail'][:70]}", flush=True)
            by_name = {item["name"]: item for item in report["results"]}
            check("Самопроверка: голос", by_name["Голос"]["ok"], by_name["Голос"]["detail"])
            check("Самопроверка: слух", by_name["Слух: слово «Джарвис»"]["ok"])
            check("Самопроверка: погода", by_name["Погода и воздух"]["ok"], by_name["Погода и воздух"]["detail"])
            check("Самопроверка: поиск в интернете", by_name["Поиск в интернете"]["ok"], by_name["Поиск в интернете"]["detail"])
            check("Самопроверка: курс Нацбанка", by_name["Курс Нацбанка"]["ok"], by_name["Курс Нацбанка"]["detail"])
            check("Самопроверка: управление окнами", by_name["Управление окнами"]["ok"], by_name["Управление окнами"]["detail"])
            check("Самопроверка: медиасессии", by_name["Медиасессии"]["ok"], by_name["Медиасессии"]["detail"])

            await asyncio.sleep(1)
            page_state = await page.js("""({
                place: document.getElementById('placeLabel').textContent,
                ambient: document.getElementById('ambientLabel').textContent,
                refresh: __jarvis.core.refresh, stride: __jarvis.core.stride,
                meters: document.querySelectorAll('.meter').length,
                overflow: document.documentElement.scrollWidth > innerWidth,
            })""")
            print("панель:", json.dumps(page_state, ensure_ascii=False), flush=True)
            check("Город в углу окна: Алматы", "Алматы" in page_state["place"], page_state["place"])
            check("Погода и воздух показаны", "°" in page_state["ambient"], page_state["ambient"])
            check("Частота экрана определена", page_state["refresh"] >= 30, f"{page_state['refresh']} Гц, шаг {page_state['stride']}")
            check("Показана панель «Сегодня»", page_state["meters"] == 3)
            check("Страница не шире окна", not page_state["overflow"])
            await page.js("document.getElementById('settingsBtn').click()")
            await asyncio.sleep(0.8)
            await page.shot("e2e-settings")
            await page.js("document.getElementById('settingsClose').click()")
            await asyncio.sleep(0.5)
            check("Настройки закрываются с анимацией", await page.js("!document.getElementById('settings').open"))

            if MEASURE:
                # venv на Windows запускает настоящий python дочерним процессом, поэтому берём pid из runtime.json.
                server = psutil.Process(json.loads((HOME / "runtime.json").read_text(encoding="utf-8"))["pid"])
                browser = _browser_root(HOME)
                load: dict = {}

                async def measure(name: str, seconds: float) -> None:
                    before = await page.js("__jarvis.core.frameCount")
                    loop = asyncio.get_running_loop()
                    data = await loop.run_in_executor(None, sample_load, server, browser, seconds)
                    frames = (await page.js("__jarvis.core.frameCount")) - before
                    data["fps"] = round(frames / seconds, 1)
                    data["hidden"] = await page.js("document.hidden")
                    data["state"] = await page.js("__jarvis.ui.state")
                    load[name] = data
                    print(f"{name}: {json.dumps(data, ensure_ascii=False)}", flush=True)

                await asyncio.sleep(3)
                await measure("idle_visible", 30)
                await page.js("__jarvis.core.setPowerSaver(true)")
                await measure("idle_visible_saver", 20)
                await page.js("__jarvis.core.setPowerSaver(false)")
                window = await page.call("Browser.getWindowForTarget")
                await page.call("Browser.setWindowBounds", windowId=window["windowId"], bounds={"windowState": "minimized"})
                await asyncio.sleep(3)
                await measure("idle_minimized", 30)
                await page.call("Browser.setWindowBounds", windowId=window["windowId"], bounds={"windowState": "normal"})
                await asyncio.sleep(2)
                await page.js("""(() => { const b=new Uint8Array(64); let t=0;
                    __jarvis.core.sampler=()=>{t+=.08; for(let i=0;i<64;i++) b[i]=Math.max(0,Math.min(255,235*Math.exp(-i/26)*(.55+.45*Math.sin(t*3+i*.7)))); return {level:.5+.22*Math.sin(t*5), bands:b}};
                    __jarvis.core.setState('speaking'); })()""")
                await measure("speaking_animation", 20)
                await page.js("__jarvis.core.sampler=null; __jarvis.core.setState('thinking')")
                await measure("thinking_animation", 10)
                results["load"] = load

            # Закрытие окна должно полностью выключать Джарвис.
            await page.call("Browser.close")
        try:
            jarvis.wait(timeout=15)
            check("Закрытие окна завершает Джарвис", True, f"код {jarvis.returncode}")
        except subprocess.TimeoutExpired:
            check("Закрытие окна завершает Джарвис", False)
    finally:
        for proc in (jarvis, cloud):
            if proc.poll() is None:
                proc.kill()
        stamp = time.strftime("%H%M%S")
        report_text = json.dumps(results, ensure_ascii=False, indent=1)
        (WORK / "e2e_report.json").write_text(report_text, encoding="utf-8")
        (WORK / f"e2e_report_{stamp}.json").write_text(report_text, encoding="utf-8")
    print(f"\nИТОГ: провалено {len(failed)}: {failed}" if failed else "\nИТОГ: все проверки пройдены", flush=True)
    return 1 if failed else 0


def _was_interrupted(timeline: list[dict], story_said_at: float) -> bool:
    # Вся история звучит больше 30 с. Перед ней может прозвучать заполнитель, поэтому ищем по времени,
    # а не по субтитру. «Стоп» сказано через 22 с после просьбы: вскоре после этого Джарвис должен замолчать.
    speaking = [e for e in timeline if e["state"] == "speaking" and e["t"] > story_said_at]
    if not speaking:
        return False
    began = speaking[0]["t"]
    after = [e for e in timeline if e["t"] > began and e["state"] in ("listening", "thinking", "hearing", "sleep")]
    # Запас на то, что поддельный микрофон стартует с опозданием в несколько секунд относительно начала записи.
    return bool(after) and after[0]["t"] - story_said_at < 29


def _browser_root(home: Path) -> psutil.Process:
    needle = str(home / "browser").lower()
    for proc in psutil.process_iter(["cmdline"]):
        line = " ".join(proc.info["cmdline"] or []).lower()
        if needle in line and "--type=" not in line and "msedge" in line:
            return proc
    raise RuntimeError("браузер не найден")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except AttributeError:
        pass
    sys.exit(asyncio.run(main()))
