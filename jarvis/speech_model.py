"""Модель Vosk: скачивание с проверкой целостности и загрузка в память."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import zipfile
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("jarvis.model")

MODEL_NAME = "vosk-model-small-ru-0.22"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
# Хеш архива зафиксирован при разработке: модель разбирает нативный код, подмена недопустима.
MODEL_SHA256 = "961d5ff98a17f4aa6de69864d0aa71fa5bac682301d2b5d17a3f24c5c99a46d4"


def _safe_extract(archive: Path, target: Path) -> None:
    root = target.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            dest = (target / member.filename).resolve()
            if root not in dest.parents and dest != root:
                raise ValueError(f"небезопасный путь в архиве: {member.filename}")
        zf.extractall(target)


class SpeechModel:
    """Отвечает на вопрос «готова ли модель» и сообщает о ходе загрузки."""

    def __init__(self, home: Path, on_status: Callable[[str, float], None]) -> None:
        self.dir = home / "models"
        self.path = self.dir / MODEL_NAME
        self.on_status = on_status
        self.model = None
        self._lock = asyncio.Lock()

    async def get(self):
        if self.model is not None:
            return self.model
        async with self._lock:
            if self.model is None:
                if not (self.path / "am" / "final.mdl").exists():
                    await self._download()
                self.on_status("loading", 1.0)
                self.model = await asyncio.to_thread(self._load)
                self.on_status("ready", 1.0)
        return self.model

    def unload(self) -> None:
        self.model = None
        self.on_status("idle", 0.0)

    def _load(self):
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        return Model(str(self.path))

    async def _download(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"{MODEL_NAME}.zip.part"
        digest = hashlib.sha256()
        self.on_status("downloading", 0.0)
        timeout = httpx.Timeout(connect=20, read=60, write=20, pool=20)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", MODEL_URL) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length") or 0)
                done = 0
                with tmp.open("wb") as fh:
                    async for part in resp.aiter_bytes(1 << 18):
                        fh.write(part)
                        digest.update(part)
                        done += len(part)
                        if total:
                            self.on_status("downloading", done / total)
        if digest.hexdigest() != MODEL_SHA256:
            tmp.unlink(missing_ok=True)
            raise ValueError("контрольная сумма модели не совпала")
        self.on_status("unpacking", 1.0)
        staging = self.dir / "staging"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            await asyncio.to_thread(_safe_extract, tmp, staging)
            if self.path.exists():
                shutil.rmtree(self.path)
            (staging / MODEL_NAME).replace(self.path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            tmp.unlink(missing_ok=True)
