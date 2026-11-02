"""Текст с экрана средствами самой Windows: бесплатно, без сети и без токенов модели.

UI Automation читает только то, о чём программа рассказывает сама, а модель зрения стоит 2500 токенов за снимок и ошибается
в координатах. Распознавание Windows видит любой текст на экране и точно знает, где он стоит: так Джарвис может нажать
надпись в игре, в лаунчере или на картинке, где кнопок для программ чтения с экрана нет.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("jarvis.ocr")

# Мелкий текст на экране 96–120 dpi распознаётся намного хуже, чем при двойном увеличении.
_UPSCALE_BELOW = 2600
_CYRILLIC = re.compile(r"[а-яё]", re.I)
_LATIN = re.compile(r"[a-z]", re.I)
_CLEAN = re.compile(r"[^\w]+", re.UNICODE)
# Похожие буквы: распознавание и человек могут написать одно слово разными алфавитами.
_LOOKALIKE = str.maketrans("аеорсухАВЕКМНОРСТХ", "aeopcyxABEKMHOPCTX")


@dataclass
class Word:
    text: str
    left: float
    top: float
    right: float
    bottom: float


@dataclass
class Line:
    text: str
    words: list[Word]


@dataclass
class Found:
    text: str
    center: tuple[int, int]
    score: float


class OcrUnavailable(Exception):
    """Нет пакета распознавания или языка в Windows."""


def _engine(language: str) -> Any:
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    return OcrEngine.try_create_from_language(Language(language))


def _software_bitmap(image: Any) -> Any:
    from winrt.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import Buffer

    raw = image.convert("RGBA").tobytes("raw", "BGRA")
    buffer = Buffer(len(raw))
    buffer.length = len(raw)
    memoryview(buffer)[: len(raw)] = raw
    return SoftwareBitmap.create_copy_from_buffer(buffer, BitmapPixelFormat.BGRA8, image.width, image.height, BitmapAlphaMode.PREMULTIPLIED)


async def _recognize(image: Any, language: str, scale: float) -> list[Line]:
    engine = _engine(language)
    if engine is None:
        raise OcrUnavailable(f"нет языка {language}")
    result = await engine.recognize_async(_software_bitmap(image))
    lines = []
    for line in result.lines:
        words = []
        for word in line.words:
            box = word.bounding_rect
            words.append(Word(word.text, box.x / scale, box.y / scale, (box.x + box.width) / scale, (box.y + box.height) / scale))
        lines.append(Line(line.text, words))
    return lines


async def read_image(image: Any, languages: tuple[str, ...] = ("ru", "en-US")) -> dict[str, list[Line]]:
    """Распознаёт картинку на каждом языке отдельно: движок одного языка искажает слова другого."""
    from PIL import Image

    scale = 2.0 if max(image.size) < _UPSCALE_BELOW else 1.0
    prepared = image.resize((image.width * 2, image.height * 2), Image.Resampling.BICUBIC) if scale > 1 else image
    results: dict[str, list[Line]] = {}
    try:
        for language in languages:
            results[language] = await _recognize(prepared, language, scale)
    except ImportError as exc:
        raise OcrUnavailable("нет пакета распознавания") from exc
    except OSError as exc:
        log.info("Распознавание текста не сработало: %s", type(exc).__name__)
        raise OcrUnavailable(type(exc).__name__) from exc
    return results


def _line_kind(text: str) -> str:
    cyrillic, latin = len(_CYRILLIC.findall(text)), len(_LATIN.findall(text))
    return "ru" if cyrillic >= latin else "en-US"


def plain_text(results: dict[str, list[Line]], limit: int = 6000) -> str:
    """Текст сверху вниз. Для каждой строки берём движок того алфавита, которого в ней больше."""
    russian = results.get("ru", [])
    english = results.get("en-US", [])
    if not russian:
        lines = [line.text for line in english]
    else:
        lines = []
        for line in russian:
            top = line.words[0].top if line.words else 0
            left = line.words[0].left if line.words else 0
            twin = next((other for other in english if other.words and abs(other.words[0].top - top) < 6
                         and abs(other.words[0].left - left) < 30), None)
            # Русский движок читает латиницу почти без ошибок, а вот обратное неверно.
            better = twin.text if twin is not None and _line_kind(line.text) == "en-US" else line.text
            lines.append(better)
    joined = "\n".join(lines)
    return joined[:limit]


def _normal(text: str) -> str:
    return _CLEAN.sub(" ", text.lower().replace("ё", "е")).strip()


def _key(text: str) -> str:
    return _normal(text).translate(_LOOKALIKE).replace(" ", "")


def find_text(results: dict[str, list[Line]], query: str, cutoff: float = 0.8) -> list[Found]:
    """Где на картинке написано query. Ищем и целые строки, и несколько слов подряд внутри строки."""
    wanted = _key(query)
    if not wanted:
        return []
    found: list[Found] = []
    size = len(_normal(query).split())
    for lines in results.values():
        for line in lines:
            words = line.words
            for count in {size, max(1, size - 1), size + 1}:
                for start in range(0, max(0, len(words) - count) + 1):
                    piece = words[start:start + count]
                    if not piece:
                        continue
                    text = " ".join(word.text for word in piece)
                    key = _key(text)
                    if not key:
                        continue
                    score = difflib.SequenceMatcher(None, wanted, key).ratio()
                    if key == wanted:
                        score = 1.0
                    if score >= cutoff:
                        left = min(word.left for word in piece)
                        right = max(word.right for word in piece)
                        top = min(word.top for word in piece)
                        bottom = max(word.bottom for word in piece)
                        found.append(Found(text, (round((left + right) / 2), round((top + bottom) / 2)), score))
    return _unique(found)


def _unique(found: list[Found]) -> list[Found]:
    """Один и тот же текст находят оба движка: оставляем лучший, а порядок сверху вниз сохраняем."""
    kept: list[Found] = []
    for item in sorted(found, key=lambda entry: -entry.score):
        if all(abs(item.center[0] - other.center[0]) > 12 or abs(item.center[1] - other.center[1]) > 10 for other in kept):
            kept.append(item)
    best = kept[0].score if kept else 0.0
    close = [item for item in kept if item.score >= best - 0.08]
    return sorted(close, key=lambda entry: (entry.center[1], entry.center[0]))
