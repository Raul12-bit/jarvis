"""Воспроизводимый замер нагрузки Джарвис с разбивкой по процессам.

Условия: окно Edge на экране, поддельный микрофон с тишиной (звуковой тракт браузера работает как обычно),
языковая модель заменена заглушкой, профиль браузера прогрет прошлым запуском, состояние «сон».
Запуск: python tests/e2e/measure_load.py <рабочая_папка>
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil
import websockets

PROJECT = Path(__file__).resolve().parents[2]
WORK = Path(sys.argv[1]).resolve()
HOME = WORK / "home-load"
PORT, CLOUD_PORT, DEBUG_PORT = 47398, 47399, 47400
CORES = psutil.cpu_count() or 1


def process_kind(proc: psutil.Process) -> str:
    line = " ".join(proc.cmdline())
    if "--type=" not in line:
        return "browser"
    kind = line.split("--type=")[1].split()[0]
    if kind == "utility":
        sub = line.split("--utility-sub-type=")[1].split()[0] if "--utility-sub-type=" in line else "utility"
        return sub.split(".")[-1]
    return kind


def snapshot(procs: list[psutil.Process]) -> dict[int, tuple[str, float]]:
    out = {}
    for proc in procs:
        try:
            out[proc.pid] = (process_kind(proc), sum(proc.cpu_times()[:2]))
        except psutil.Error:
            pass
    return out


def measure(server: psutil.Process, root: psutil.Process, seconds: float) -> dict:
    procs = [root, *root.children(recursive=True)]
    server_start = sum(server.cpu_times()[:2])
    start = snapshot(procs)
    time.sleep(seconds)
    end = snapshot(procs)
    by_kind: dict[str, float] = {}
    for pid, (kind, cpu) in end.items():
        if pid in start:
            by_kind[kind] = by_kind.get(kind, 0.0) + max(0.0, cpu - start[pid][1])
    server_cpu = sum(server.cpu_times()[:2]) - server_start
    memory = {"server_rss": server.memory_info().rss, "server_uss": server.memory_full_info().uss,
              "browser_private": 0, "browser_uss": 0}
    for proc in procs:
        try:
            info = proc.memory_full_info()
            memory["browser_private"] += info.private
            memory["browser_uss"] += info.uss
        except psutil.Error:
            pass
    total = server_cpu + sum(by_kind.values())
    return {
        "total_cpu_percent": round(total / seconds / CORES * 100, 2),
        "server_cpu_percent": round(server_cpu / seconds / CORES * 100, 2),
        "by_process_one_core": {k: round(v / seconds * 100, 1) for k, v in sorted(by_kind.items(), key=lambda x: -x[1])},
        "memory_mb": {k: round(v / 2**20) for k, v in memory.items()},
    }


async def js(ws, expr: str, counter=[0]):  # noqa: B006
    counter[0] += 1
    await ws.send(json.dumps({"id": counter[0], "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}}))
    while True:
        data = json.loads(await ws.recv())
        if data.get("id") == counter[0]:
            return data["result"]["result"].get("value")


async def cdp(ws, method: str, counter=[100000], **params):  # noqa: B006
    counter[0] += 1
    await ws.send(json.dumps({"id": counter[0], "method": method, "params": params}))
    while True:
        data = json.loads(await ws.recv())
        if data.get("id") == counter[0]:
            return data.get("result")


async def run_once(python: str, env: dict, warm_seconds: float, label: str) -> dict:
    cloud = subprocess.Popen([python, str(PROJECT / "tests" / "e2e" / "fake_cloud.py"), str(CLOUD_PORT), str(WORK / "cloud-load.jsonl")])
    jarvis = subprocess.Popen(
        [python, "-m", "jarvis", f"--port={PORT}", "--token=load",
         "--browser-arg=--use-fake-device-for-media-stream", "--browser-arg=--use-fake-ui-for-media-stream",
         f"--browser-arg=--use-file-for-fake-audio-capture={WORK / 'silence.wav'}",
         f"--browser-arg=--remote-debugging-port={DEBUG_PORT}"],
        cwd=PROJECT, env=env,
    )
    results: dict = {}
    try:
        target = None
        for _ in range(150):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json", timeout=1) as resp:
                    pages = [t for t in json.load(resp) if t["type"] == "page" and f":{PORT}" in t["url"]]
                if pages:
                    target = pages[0]
                    break
            except OSError:
                pass
            await asyncio.sleep(0.2)
        assert target, "окно не открылось"
        server = psutil.Process(json.loads((HOME / "runtime.json").read_text(encoding="utf-8"))["pid"])
        needle = str(HOME / "browser").lower()
        root = next(
            p for p in psutil.process_iter(["cmdline"])
            if needle in " ".join(p.info["cmdline"] or []).lower() and "--type=" not in " ".join(p.info["cmdline"] or [])
        )
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            await asyncio.sleep(warm_seconds)
            loop = asyncio.get_running_loop()

            async def segments(name: str, count: int = 3, seconds: float = 15.0) -> None:
                frames0 = await js(ws, "__jarvis.core.frameCount")
                rows = []
                for _ in range(count):
                    rows.append(await loop.run_in_executor(None, measure, server, root, seconds))
                frames = (await js(ws, "__jarvis.core.frameCount")) - frames0
                median = statistics.median(row["total_cpu_percent"] for row in rows)
                results[name] = {
                    "median_total_cpu_percent": median,
                    "all_segments": [row["total_cpu_percent"] for row in rows],
                    "fps": round(frames / (count * seconds), 1),
                    "detail": rows[-1],
                }
                print(label, name, json.dumps(results[name], ensure_ascii=False), flush=True)

            print(label, "состояние:", await js(ws, "__jarvis.ui.state"), "скрыто:", await js(ws, "document.hidden"), flush=True)
            await segments("idle_visible")
            await js(ws, "__jarvis.core.setPowerSaver(true)")
            await asyncio.sleep(2)
            await segments("idle_saver")
            await js(ws, "__jarvis.core.setPowerSaver(false)")
            window = await cdp(ws, "Browser.getWindowForTarget")
            await cdp(ws, "Browser.setWindowBounds", windowId=window["windowId"], bounds={"windowState": "minimized"})
            await asyncio.sleep(3)
            await segments("idle_minimized")
            await cdp(ws, "Browser.setWindowBounds", windowId=window["windowId"], bounds={"windowState": "normal"})
            await asyncio.sleep(3)
            await js(ws, """(() => { const b=new Uint8Array(64); let t=0;
                __jarvis.core.sampler=()=>{t+=.08; for(let i=0;i<64;i++) b[i]=Math.max(0,Math.min(255,235*Math.exp(-i/26)*(.55+.45*Math.sin(t*3+i*.7)))); return {level:.5+.22*Math.sin(t*5), bands:b}};
                __jarvis.core.setState('speaking'); })()""")
            await asyncio.sleep(1)
            await segments("speaking_animation")
            await js(ws, "__jarvis.core.sampler=null; __jarvis.core.setState('thinking')")
            await asyncio.sleep(1)
            await segments("thinking_animation", count=2)
            await cdp(ws, "Browser.close")
        jarvis.wait(timeout=15)
    finally:
        for proc in (jarvis, cloud):
            if proc.poll() is None:
                proc.kill()
    return results


async def main() -> None:
    import wave

    WORK.mkdir(parents=True, exist_ok=True)
    if HOME.exists():
        shutil.rmtree(HOME, ignore_errors=True)
    (HOME / "models").mkdir(parents=True)
    source = Path(os.environ["JARVIS_E2E_MODEL"])
    shutil.copytree(source, HOME / "models" / source.name)
    (HOME / "config.json").write_text(json.dumps({"onboarded": True, "mascot": False, "shortcut_created": True}), encoding="utf-8")
    with wave.open(str(WORK / "silence.wav"), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0" * 32000 * 60)
    env = {**os.environ, "JARVIS_HOME": str(HOME), "JARVIS_TEST_BASE": f"http://127.0.0.1:{CLOUD_PORT}/v1"}
    env.pop("GROQ_API_KEY", None)
    print("Первый запуск: свежий профиль браузера", flush=True)
    first = await run_once(sys.executable, env, 20, "свежий")
    print("Второй запуск: прогретый профиль", flush=True)
    second = await run_once(sys.executable, env, 25, "прогретый")
    (WORK / "load_report.json").write_text(json.dumps({"fresh": first, "warm": second}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(main())
