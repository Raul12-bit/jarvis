"""Выделение речи из потока микрофона на основе Silero VAD.

Модель запускается через onnxruntime в один поток без активного ожидания. Это принципиально:
пакет pysilero-vad с той же моделью занимал 298 % одного ядра (потоки ggml крутятся вхолостую),
а здесь около 2 % при той же точности. Обе цифры измерены на 31 кадре в секунду.
"""
from __future__ import annotations

from collections import deque
from enum import Enum

import numpy as np
import onnxruntime as ort

from .config import ASSETS_DIR

FRAME_BYTES = 1024  # 512 отсчётов PCM16, 16 кГц: 32 мс, ровно столько требует Silero
FRAME_SECONDS = 0.032
SAMPLE_RATE = 16000
_CONTEXT = 64  # модели нужен хвост предыдущего кадра


class SileroVad:
    def __init__(self) -> None:
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_cpu_mem_arena = False
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._session = ort.InferenceSession(
            str(ASSETS_DIR / "silero_vad_16k_op15.onnx"), options, providers=["CPUExecutionProvider"]
        )
        self._rate = np.array(SAMPLE_RATE, np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), np.float32)
        self._context = np.zeros((1, _CONTEXT), np.float32)

    def process_chunk(self, frame: bytes) -> float:
        """Вероятность речи в кадре из 512 отсчётов."""
        samples = np.frombuffer(frame, np.int16).astype(np.float32) / 32768.0
        window = np.concatenate([self._context, samples[None, :]], axis=1)
        output, self._state = self._session.run(
            None, {"input": window, "state": self._state, "sr": self._rate}
        )
        self._context = window[:, -_CONTEXT:]
        return float(output[0][0])


class VadEvent(Enum):
    START = "start"
    END = "end"
    CANCEL = "cancel"


class Segmenter:
    """Конечный автомат: тишина → речь → тишина.

    Пока Джарвис говорит, включается строгий порог: через эхоподавление может просочиться
    её собственный голос, и перебивать себя она не должна.
    """

    def __init__(
        self,
        on_prob: float = 0.5,
        off_prob: float = 0.35,
        start_frames: int = 3,
        # 480 мс тишины считаем концом фразы. Ошибиться не страшно: если человек продолжит,
        # пока Джарвис думает, реплики склеятся (см. Assistant._merge_into_pending).
        hangover_frames: int = 15,
        # 512 мс предзаписи: при строгом пороге речь опознаётся с опозданием, и без этого теряется начало.
        preroll_frames: int = 16,
        strict_prob: float = 0.75,
        strict_frames: int = 8,
        min_speech_frames: int = 6,
        max_seconds: float = 30.0,
    ) -> None:
        self.on_prob = on_prob
        self.off_prob = off_prob
        self.start_frames = start_frames
        self.hangover_frames = hangover_frames
        self.strict_prob = strict_prob
        self.strict_frames = strict_frames
        self.min_speech_frames = min_speech_frames
        self.max_frames = int(max_seconds / FRAME_SECONDS)
        self._preroll: deque[bytes] = deque(maxlen=preroll_frames)
        self.in_speech = False
        self._frames: list[bytes] = []
        self._run = 0
        self._silence = 0
        self._voiced = 0
        self._done_audio = b""

    def reset(self) -> None:
        self.in_speech = False
        self._frames = []
        self._run = 0
        self._silence = 0
        self._voiced = 0
        self._preroll.clear()

    def push(self, frame: bytes, prob: float, strict: bool = False) -> VadEvent | None:
        if not self.in_speech:
            threshold = self.strict_prob if strict else self.on_prob
            needed = self.strict_frames if strict else self.start_frames
            self._run = self._run + 1 if prob >= threshold else 0
            self._preroll.append(frame)
            if self._run < needed:
                return None
            self.in_speech = True
            self._frames = list(self._preroll)
            self._preroll.clear()
            self._voiced = self._run
            self._silence = 0
            self._run = 0
            return VadEvent.START

        self._frames.append(frame)
        if prob >= self.off_prob:
            self._silence = 0
            self._voiced += 1
        else:
            self._silence += 1
        if self._silence < self.hangover_frames and len(self._frames) < self.max_frames:
            return None
        return self._finish()

    def _finish(self) -> VadEvent:
        # Хвост тишины не нужен распознаванию: оставляем от него примерно 200 мс.
        trim = max(0, self._silence - 6)
        frames = self._frames[: len(self._frames) - trim] if trim else self._frames
        enough = self._voiced >= self.min_speech_frames
        self._done_audio = b"".join(frames) if enough else b""
        self.in_speech = False
        self._frames = []
        self._silence = 0
        self._voiced = 0
        return VadEvent.END if enough else VadEvent.CANCEL

    def current_audio(self) -> bytes:
        return b"".join(self._frames)

    def take_audio(self) -> bytes:
        audio, self._done_audio = self._done_audio, b""
        return audio
