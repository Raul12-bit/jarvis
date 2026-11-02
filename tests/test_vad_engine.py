"""Настоящая модель Silero: точность на простых сигналах и защита от возврата к высокой нагрузке."""
import math
import random
import struct
import time

import psutil

from jarvis.vad import FRAME_BYTES, SAMPLE_RATE, SileroVad


def pcm(samples: list[float]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *[max(-32768, min(32767, int(s))) for s in samples])


def frames_of(data: bytes) -> list[bytes]:
    return [data[i:i + FRAME_BYTES] for i in range(0, len(data) - FRAME_BYTES + 1, FRAME_BYTES)]


def test_silence_and_noise_are_not_speech() -> None:
    vad = SileroVad()
    assert max(vad.process_chunk(b"\0" * FRAME_BYTES) for _ in range(30)) < 0.2
    random.seed(1)
    noise = pcm([random.gauss(0, 800) for _ in range(SAMPLE_RATE * 2)])
    assert max(vad.process_chunk(frame) for frame in frames_of(noise)) < 0.5


def test_reset_clears_recurrent_state() -> None:
    vad = SileroVad()
    frame = pcm([3000 * math.sin(2 * math.pi * 200 * t / SAMPLE_RATE) for t in range(512)])
    first = vad.process_chunk(frame)
    for _ in range(10):
        vad.process_chunk(frame)
    vad.reset()
    assert abs(vad.process_chunk(frame) - first) < 1e-6


def test_cpu_load_stays_low() -> None:
    """Движок на ggml занимал почти три ядра, хотя время на один кадр выглядело ничтожным.

    Поэтому считается именно процессорное время всего процесса в реальном темпе работы.
    """
    vad = SileroVad()
    proc = psutil.Process()
    frame = b"\0" * FRAME_BYTES
    cpu_before = sum(proc.cpu_times()[:2])
    started = time.perf_counter()
    next_at = started
    while time.perf_counter() - started < 3.0:
        vad.process_chunk(frame)
        next_at += 0.032
        delay = next_at - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    load = (sum(proc.cpu_times()[:2]) - cpu_before) / (time.perf_counter() - started)
    assert load < 0.25, f"VAD занимает {load * 100:.0f} % одного ядра"
