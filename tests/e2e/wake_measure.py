"""Замер слова активации: слышит ли локальный распознаватель «Джарвис» на разных голосах, с шумом и эхом.

Речь синтетическая: честная оценка полноты по голосам, а не по живому человеку. Настоящий журнал покажет остальное.
Записи кладутся в рабочую папку, повторный запуск в сервис синтеза не ходит.

Запуск: python tests/e2e/wake_measure.py <рабочая_папка> <папка_модели_vosk>
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import sys
from pathlib import Path

import edge_tts
import miniaudio
import numpy as np
from vosk import KaldiRecognizer, Model, SetLogLevel

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from jarvis.vad import SAMPLE_RATE  # noqa: E402
from jarvis.wake import GRAMMAR, MAYBE_WORDS, WAKE_WORD, read_hearing  # noqa: E402

VOICES = [
    "ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural", "en-US-BrianMultilingualNeural", "en-US-AvaMultilingualNeural",
    "en-US-EmmaMultilingualNeural", "de-DE-FlorianMultilingualNeural", "de-DE-SeraphinaMultilingualNeural",
    "fr-FR-RemyMultilingualNeural", "kk-KZ-DauletNeural", "kk-KZ-AigulNeural",
]
CALLS = ["Джарвис.", "Джарвис, какая сегодня погода?", "Эй, Джарвис, открой ютуб.", "Сколько времени, Джарвис?"]
OTHERS = [
    "Какая сегодня хорошая погода.", "Открой, пожалуйста, окно на кухне.", "Давай посмотрим этот фильм вечером.",
    "Чарльз Дарвин написал знаменитую книгу.", "Этот сервис сегодня работает плохо.", "Сегодня на улице очень жарко.",
    "Джордж купил новую машину в Лондоне.", "Я слушаю джаз по вечерам.", "Джон и Джек пошли в кино.",
    "У нас в офисе новый кофейный автомат.", "Завтра мне надо рано вставать на работу.", "Позвони маме, она ждёт.",
    "Этот проект нужно закончить к пятнице.", "Кот снова спит на моей клавиатуре.", "Сколько стоит этот телефон?",
    "Дарья приготовила очень вкусный суп.", "Мы поедем на море следующим летом.", "Купи хлеба и молока по дороге домой.",
    "Гарри Поттер снова спасает школу.", "В Джакарте сегодня сильный дождь.", "Варвара читает книгу на диване.",
    "Новая серия вышла вчера вечером.", "Давай закажем пиццу и посидим дома.", "Он починил кран и ушёл.",
    "Этот сервиз подарила нам бабушка.",
]
NOISE_DB = {"тихо": None, "комната": 15.0, "шумно": 8.0, "очень шумно": 3.0}


def speech_file(folder: Path, voice: str, text: str) -> Path:
    digest = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()[:16]
    return folder / f"{digest}.mp3"


async def synthesize(folder: Path) -> list[str]:
    missing = []
    for voice in VOICES:
        lines = CALLS + (OTHERS if voice in VOICES[:3] else OTHERS[:6])
        for text in lines:
            path = speech_file(folder, voice, text)
            if path.exists():
                continue
            audio = bytearray()
            for attempt in range(3):
                try:
                    async with asyncio.timeout(15):
                        async for chunk in edge_tts.Communicate(text, voice).stream():
                            if chunk["type"] == "audio":
                                audio += chunk["data"]
                    if audio:
                        break
                except Exception:  # noqa: BLE001 — сервис иногда не отвечает отдельному голосу
                    await asyncio.sleep(1.5 * (attempt + 1))
            if audio:
                path.write_bytes(audio)
            else:
                missing.append(f"{voice}: {text}")
            await asyncio.sleep(0.25)
    return missing


def load_pcm(path: Path) -> np.ndarray:
    decoded = miniaudio.mp3_read_s16(path.read_bytes()) if False else miniaudio.decode(path.read_bytes(), nchannels=1, sample_rate=SAMPLE_RATE)
    return np.frombuffer(decoded.samples, dtype=np.int16).astype(np.float32) / 32768


def room(pcm: np.ndarray, snr: float | None, rng: np.random.Generator) -> bytes:
    audio = pcm
    if snr is not None:
        # Отражения маленькой комнаты и шум, похожий на вентилятор и улицу: низкие частоты сильнее высоких.
        echo = np.zeros_like(audio)
        for delay_ms, gain in ((18, 0.35), (37, 0.22), (71, 0.12)):
            shift = int(SAMPLE_RATE * delay_ms / 1000)
            echo[shift:] += audio[:-shift] * gain
        audio = audio + echo
        noise = rng.standard_normal(len(audio))
        spectrum = np.fft.rfft(noise) / np.sqrt(np.arange(1, len(noise) // 2 + 2))
        noise = np.fft.irfft(spectrum, len(noise))
        speech_power = np.mean(audio ** 2) + 1e-9
        noise *= np.sqrt(speech_power / (np.mean(noise ** 2) + 1e-12) / 10 ** (snr / 10))
        audio = audio + noise
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()


def hear(model: Model, grammar: list[str], audio: bytes) -> tuple[bool, bool, set[str]]:
    rec = KaldiRecognizer(model, SAMPLE_RATE, json.dumps(grammar, ensure_ascii=False))
    rec.SetMaxAlternatives(3)
    sure = maybe = False
    words: set[str] = set()
    for start in range(0, len(audio), 1024):
        if rec.AcceptWaveform(audio[start:start + 1024]):
            payload = rec.Result()
            one, two = read_hearing(payload)
            sure, maybe = sure or one, maybe or two
            words |= _words(payload)
    payload = rec.FinalResult()
    one, two = read_hearing(payload)
    words |= _words(payload)
    return sure or one, maybe or two, words


def _words(payload: str) -> set[str]:
    data = json.loads(payload)
    found: set[str] = set(data.get("text", "").split())
    for alternative in data.get("alternatives") or []:
        found.update(alternative.get("text", "").split())
    return found


def main() -> None:
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    missing = asyncio.run(synthesize(work))
    print("синтез не удался:", len(missing), missing[:6])
    SetLogLevel(-1)
    model = Model(sys.argv[2])
    rng = np.random.default_rng(7)
    heard: collections.Counter[str] = collections.Counter()
    print(f"слов в грамматике: {len(GRAMMAR)}, похожие: {sorted(MAYBE_WORDS)}")
    for name, snr in NOISE_DB.items():
        total = sure = maybe = 0
        by_voice: dict[str, list[int]] = {}
        for voice in VOICES:
            for text in CALLS:
                path = speech_file(work, voice, text)
                if not path.exists():
                    continue
                one, two, words = hear(model, GRAMMAR, room(load_pcm(path), snr, rng))
                total += 1
                sure += one
                maybe += one or two
                by_voice.setdefault(voice, []).append(int(one or two))
                if not one:
                    heard.update(words)
        rows = ", ".join(f"{voice.split('-')[2][:6]} {sum(v)}/{len(v)}" for voice, v in by_voice.items())
        print(f"{name:12s} сразу {sure}/{total} = {sure / max(total, 1):.0%}, с проверкой {maybe}/{total} = {maybe / max(total, 1):.0%} | {rows}")
    print("что слышно вместо имени:", heard.most_common(40))
    for name, snr in (("тихо", None), ("комната", 15.0)):
        total = sure = maybe = 0
        wrong: collections.Counter[str] = collections.Counter()
        for voice in VOICES:
            for text in OTHERS:
                path = speech_file(work, voice, text)
                if not path.exists():
                    continue
                one, two, words = hear(model, GRAMMAR, room(load_pcm(path), snr, rng))
                total += 1
                sure += one
                maybe += one or two
                if one or two:
                    wrong[text] += 1
        print(f"чужие фразы, {name}: будят {sure}/{total} = {sure / max(total, 1):.1%}, уходят на проверку {maybe}/{total} = {maybe / max(total, 1):.1%}", wrong.most_common(6))


if __name__ == "__main__":
    main()
