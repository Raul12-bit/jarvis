"""Проверка эха на настоящих динамиках и микрофоне: перебивает ли Джарвис сам себя.

Джарвис говорит вслух примерно минуту. С микрофона считается только уровень громкости. Распознавание
локальное, языковая модель заменена заглушкой, поэтому ни один звук не покидает компьютер.

Запуск: python tests/e2e/real_audio_check.py <рабочая_папка>
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

PROJECT = Path(__file__).resolve().parents[2]
WORK = Path(sys.argv[1]).resolve()
HOME = WORK / "home-real"
LOG = WORK / "cloud-real.jsonl"
PORT, CLOUD_PORT, DEBUG_PORT = 47395, 47396, 47397

VOLUME_SCRIPT = r"""
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume { int f(); int g(); int h(); int i(); int SetMasterVolumeLevelScalar(float l, System.Guid g); int j(); int GetMasterVolumeLevelScalar(out float l); int k(); int m(); int n(); int o(); int SetMute(bool b, System.Guid g); int GetMute(out bool b); }
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref System.Guid id, int ctx, int p, out IAudioEndpointVolume aev); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int f(); int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice d); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorCom { }
public class JarvisVol { public static string Read() {
  var e = (IMMDeviceEnumerator)new MMDeviceEnumeratorCom(); IMMDevice d; e.GetDefaultAudioEndpoint(0, 1, out d);
  var id = typeof(IAudioEndpointVolume).GUID; IAudioEndpointVolume v; d.Activate(ref id, 23, 0, out v);
  float l; bool m; v.GetMasterVolumeLevelScalar(out l); v.GetMute(out m); return (int)(l*100) + " " + m; } }
'@
[JarvisVol]::Read()
"""


class Page:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.counter = 0

    async def js(self, expression: str):
        self.counter += 1
        current = self.counter
        await self.ws.send(json.dumps({"id": current, "method": "Runtime.evaluate", "params": {
            "expression": expression, "returnByValue": True, "awaitPromise": True}}))
        while True:
            data = json.loads(await self.ws.recv())
            if data.get("id") == current:
                if "exceptionDetails" in data.get("result", {}):
                    raise RuntimeError(data["result"]["exceptionDetails"])
                return data["result"]["result"].get("value")


HOOK = """(() => {
  window.__lv = []; window.__duck = []; window.__notices = [];
  const k = __jarvis;
  const level = k.mic.onLevel;
  k.mic.onLevel = (r) => { __lv.push([performance.now(), r, k.player.playing ? 1 : 0]); level(r); };
  const duck = k.player.duck.bind(k.player);
  k.player.duck = (on) => { __duck.push([performance.now(), on]); duck(on); };
  new MutationObserver(() => { const n = document.getElementById('notice'); if (!n.hidden) __notices.push(n.textContent); })
    .observe(document.getElementById('notice'), {attributes: true, childList: true, characterData: true, subtree: true});
  return true;
})()"""

STATS = """((rows) => {
  const db = (x) => x > 0 ? (20 * Math.log10(x)).toFixed(1) : 'тишина';
  const rms = (a) => a.length ? Math.sqrt(a.reduce((s, r) => s + r[1] * r[1], 0) / a.length) : 0;
  const on = rows.filter(r => r[2] === 1), off = rows.filter(r => r[2] === 0);
  const peak = on.length ? Math.max(...on.map(r => r[1])) : 0;
  return {playing_rms_db: db(rms(on)), silent_rms_db: db(rms(off)), playing_peak_db: db(peak), samples_on: on.length, samples_off: off.length,
          rms_on: rms(on), rms_off: rms(off)};
})"""


async def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    LOG.unlink(missing_ok=True)
    if HOME.exists():
        shutil.rmtree(HOME, ignore_errors=True)
    (HOME / "models").mkdir(parents=True)
    source = Path(os.environ["JARVIS_E2E_MODEL"])
    shutil.copytree(source, HOME / "models" / source.name)
    (HOME / "config.json").write_text(json.dumps({"onboarded": True, "mascot": False, "followup_seconds": 60, "shortcut_created": True}), encoding="utf-8")

    volume = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", VOLUME_SCRIPT], capture_output=True, text=True, timeout=30)
    print("Громкость системы и выключен ли звук:", volume.stdout.strip() or volume.stderr.strip()[:200], flush=True)

    python = sys.executable
    env = {**os.environ, "JARVIS_HOME": str(HOME), "PYTHONIOENCODING": "utf-8", "JARVIS_TEST_BASE": f"http://127.0.0.1:{CLOUD_PORT}/v1"}
    env.pop("GROQ_API_KEY", None)
    cloud = subprocess.Popen([python, str(PROJECT / "tests" / "e2e" / "fake_cloud.py"), str(CLOUD_PORT), str(LOG)])
    jarvis = subprocess.Popen(
        [python, "-m", "jarvis", f"--port={PORT}", "--token=real", "--browser-arg=--use-fake-ui-for-media-stream",
         f"--browser-arg=--remote-debugging-port={DEBUG_PORT}"],
        cwd=PROJECT, env=env,
    )
    report: dict = {"volume": volume.stdout.strip()}
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
        if target is None:
            print("Окно не открылось")
            return 1
        async with websockets.connect(target["webSocketDebuggerUrl"], max_size=None) as ws:
            page = Page(ws)
            for _ in range(80):
                if await page.js("!!window.__jarvis && __jarvis.mic.active"):
                    break
                await asyncio.sleep(0.1)
            report["mic"] = await page.js("(() => { const t = __jarvis.mic.stream.getAudioTracks()[0]; const s = t.getSettings(); return {label: t.label, echo: s.echoCancellation, ns: s.noiseSuppression, agc: s.autoGainControl}; })()")
            print("Микрофон:", report["mic"], flush=True)
            await page.js(HOOK)
            # Дождаться конца приветствия и замерить фон.
            for _ in range(200):
                if await page.js("__jarvis.ui.state") == "sleep" and not await page.js("__jarvis.player.playing"):
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(1)
            await page.js("__lv.length = 0")

            print("Фаза 1: длинный ответ вслух, эхоподавление включено...", flush=True)
            await page.js("__jarvis.player.resume(); document.getElementById('textInput').value = 'Расскажи длинную историю'; document.getElementById('composer').requestSubmit()")
            states = []
            spoke = False
            started = time.time()
            while time.time() - started < 50:
                state = await page.js("__jarvis.ui.state")
                if not states or states[-1] != state:
                    states.append(state)
                    print(f"  {time.time() - started:5.1f}s {state}", flush=True)
                spoke = spoke or state == "speaking"
                if spoke and state in ("listening", "sleep"):
                    break
                await asyncio.sleep(0.1)
            duration = time.time() - started
            report["phase1"] = {
                "seconds": round(duration, 1), "states": states,
                "stats": await page.js(f"{STATS}(__lv)"),
                "ducks": await page.js("__duck.map(d => d[1])"),
                "notices": await page.js("__notices"),
                "log": await page.js("document.getElementById('log').innerText"),
            }
            print("  Итог фазы 1:", json.dumps({k: report["phase1"][k] for k in ("seconds", "states", "stats", "ducks", "notices")}, ensure_ascii=False), flush=True)

            print("Фаза 2: тот же голос без эхоподавления, только уровень...", flush=True)
            await page.js("""(async () => {
              await __jarvis.mic.stop();
              const st = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false}});
              const c = new AudioContext(); const a = c.createAnalyser(); a.fftSize = 2048;
              c.createMediaStreamSource(st).connect(a); await c.resume();
              const buf = new Float32Array(2048);
              window.__raw = [];
              window.__rawTimer = setInterval(() => { a.getFloatTimeDomainData(buf); let e = 0; for (const v of buf) e += v * v;
                __raw.push([performance.now(), Math.sqrt(e / buf.length), __jarvis.player.playing ? 1 : 0]); }, 50);
              window.__rawStream = st;
              return true;
            })()""")
            await asyncio.sleep(2)
            await page.js("""fetch('/api/voice/preview', {method: 'POST', headers: {'X-Jarvis-Token': 'real', 'Content-Type': 'application/json'}, body: JSON.stringify({voice: 'ru-RU-SvetlanaNeural', rate: 0})})""")
            await asyncio.sleep(7)
            report["phase2"] = {"stats": await page.js(f"{STATS}(__raw)")}
            print("  Итог фазы 2:", json.dumps(report["phase2"], ensure_ascii=False), flush=True)
            await page.js("clearInterval(__rawTimer); __rawStream.getTracks().forEach(t => t.stop())")

            on = report["phase1"]["stats"]
            off = report["phase2"]["stats"]
            print(f"\nВ микрофоне во время речи Джарвиса: с эхоподавлением {on['playing_rms_db']} дБ (фон {on['silent_rms_db']}), "
                  f"без него {off['playing_rms_db']} дБ (фон {off['silent_rms_db']})", flush=True)
            interrupted = any(s in ("hearing", "thinking") for s in states[states.index("speaking") + 1:]) if "speaking" in states else None
            ducks = report["phase1"]["ducks"]
            print(f"Срабатываний «человек заговорил» во время речи: {sum(1 for d in ducks if d)}", flush=True)
            print(f"Сама себя перебила: {interrupted}", flush=True)
            await page.js("window.close()")
    finally:
        for proc in (jarvis, cloud):
            if proc.poll() is None:
                proc.kill()
        subprocess.run(["taskkill", "/F", "/IM", "nothing.exe"], capture_output=True)
        (WORK / "real_audio_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.exit(asyncio.run(main()))
