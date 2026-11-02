"""Источники фактов без ключей и без оплаты: курс Нацбанка, воздух, текст страниц, поиск по YouTube, калькулятор."""
from __future__ import annotations

import ast
import datetime
import json
import logging
import math
import operator
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("jarvis.webinfo")

# ---------- Курс валют ----------

_RATE_ITEM = re.compile(r"<title>([A-Z]{3})</title>.*?<description>([\d.,]+)</description>.*?<quant>(\d+)</quant>", re.S)
_RATE_DATE = re.compile(r"<pubDate>([^<]+)</pubDate>")
CURRENCY_ALIASES = {
    "доллар": "USD", "доллара": "USD", "долларов": "USD", "бакс": "USD", "евро": "EUR", "рубль": "RUB", "рубля": "RUB",
    "рублей": "RUB", "юань": "CNY", "юаня": "CNY", "юаней": "CNY", "фунт": "GBP", "тенге": "KZT", "лира": "TRY",
    "сом": "KGS", "сум": "UZS", "дирхам": "AED", "иена": "JPY", "йена": "JPY", "вона": "KRW",
}


def parse_rates(xml_text: str) -> tuple[str, dict[str, float]]:
    """Официальные курсы Национального банка Казахстана: сколько тенге за одну единицу валюты."""
    rates: dict[str, float] = {}
    for code, value, quant in _RATE_ITEM.findall(xml_text):
        rates[code] = float(value.replace(",", ".")) / max(int(quant), 1)
    date = _RATE_DATE.search(xml_text)
    return (date[1] if date else ""), rates


# ---------- Воздух ----------

_AQI_LEVELS = (
    (50, "хороший"), (100, "умеренный"), (150, "вредный для чувствительных людей"),
    (200, "вредный"), (300, "очень вредный"), (10_000, "опасный"),
)


def air_phrase(current: dict[str, Any]) -> str:
    aqi = current.get("us_aqi")
    pm25 = current.get("pm2_5")
    if aqi is None:
        return ""
    level = next(text for limit, text in _AQI_LEVELS if aqi <= limit)
    return f"Воздух: индекс {round(aqi)}, {level}, PM2.5 {round(pm25) if pm25 is not None else 'нет данных'} мкг/м³."


# ---------- Текст страницы ----------

_SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "header", "template", "iframe"}
_BLOCK_TAGS = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr", "section", "article", "blockquote"}
PAGE_TEXT_LIMIT = 3500
PAGE_BYTES_LIMIT = 2_000_000


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str, limit: int = PAGE_TEXT_LIMIT) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(html)
    raw = "".join(parser.parts)
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    # Короткие строки на страницах почти всегда меню и кнопки, а не текст статьи.
    body = [line for line in lines if len(line) > 40]
    text = "\n".join(body or [line for line in lines if line])
    return " ".join(parser.title.split())[:150], text[:limit]


# ---------- YouTube ----------


@dataclass
class YouTubeHit:
    video_id: str
    title: str
    length: str


_INITIAL_DATA = re.compile(r"ytInitialData\s*=\s*(\{.*?\});</script>", re.S)


def parse_youtube_results(html: str, limit: int = 8) -> list[YouTubeHit]:
    match = _INITIAL_DATA.search(html)
    if not match:
        return []
    try:
        data = json.loads(match[1])
    except ValueError:
        return []
    hits: list[YouTubeHit] = []
    stack: list[Any] = [data]
    while stack and len(hits) < limit * 3:
        node = stack.pop()
        if isinstance(node, dict):
            video = node.get("videoRenderer")
            if isinstance(video, dict) and video.get("videoId"):
                runs = (video.get("title") or {}).get("runs") or [{}]
                hits.append(YouTubeHit(
                    str(video["videoId"]), "".join(str(run.get("text", "")) for run in runs)[:110],
                    str((video.get("lengthText") or {}).get("simpleText") or ""),
                ))
                continue
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    # Без длительности бывают только прямые эфиры и подборки: как первый результат они подходят хуже.
    return hits[:limit]


async def youtube_search(http: httpx.AsyncClient, query: str) -> list[YouTubeHit]:
    resp = await http.get(
        "https://www.youtube.com/results",
        params={"search_query": query, "hl": "ru", "gl": "KZ"},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
                 "Accept-Language": "ru"},
        timeout=httpx.Timeout(connect=3.5, read=6, write=4, pool=2),
        follow_redirects=True,
    )
    resp.raise_for_status()
    return parse_youtube_results(resp.text)


def is_public_host(url: str) -> bool:
    return bool(urlsplit(url).hostname)


# ---------- Калькулятор ----------

_BINARY: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}
_UNARY: dict[type, Callable[[Any], Any]] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS: dict[str, Callable[..., float]] = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "ln": math.log, "log10": math.log10, "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
}
_CONSTANTS = {"pi": math.pi, "e": math.e}
_MAX_POWER = 1000
_MAX_MAGNITUDE = 1e60


def _date_ordinal(node: ast.Call) -> float | None:
    """date('2027-01-01') и today() дают номер дня: разность таких значений и есть число дней между датами."""
    name = node.func.id if isinstance(node.func, ast.Name) else ""
    if name == "today" and not node.args:
        return datetime.date.today().toordinal()
    if name == "date" and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return datetime.date.fromisoformat(node.args[0].value).toordinal()
    return None


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    # Модели часто пишут как в Python: (date(...) - date(...)).days. Атрибут ничего не меняет.
    if isinstance(node, ast.Attribute) and node.attr == "days":
        return _evaluate(node.value)
    if isinstance(node, ast.Call):
        ordinal = _date_ordinal(node)
        if ordinal is not None:
            return ordinal
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTANTS:
        return _CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow):
            # Степени считаем с ограничением: 9**9**9 иначе займёт всю память.
            if abs(right) > _MAX_POWER or abs(left) > _MAX_MAGNITUDE:
                raise ValueError("слишком большая степень")
            return float(left) ** right
        if type(node.op) in _BINARY:
            return _BINARY[type(node.op)](left, right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and not node.keywords:
        return _FUNCTIONS[node.func.id](*[_evaluate(arg) for arg in node.args])
    raise ValueError("недопустимое выражение")


def calculate(expression: str) -> float:
    """Настоящие вычисления вместо устного счёта модели. Никакого eval: разбираем только арифметику."""
    # Запятая здесь десятичная только внутри чисел: «3,5». Отдельно стоящую трогать незачем.
    text = re.sub(r"(?<=\d),(?=\d)", ".", str(expression))
    text = text.replace("^", "**").replace("×", "*").replace("÷", "/").replace("%", "/100")
    if len(text) > 200:
        raise ValueError("слишком длинное выражение")
    result = _evaluate(ast.parse(text, mode="eval"))
    if isinstance(result, complex) or abs(result) > _MAX_MAGNITUDE or math.isnan(result):
        raise ValueError("результат вне диапазона")
    return result


def format_number(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.4f}".rstrip("0").rstrip(".").replace(",", " ").replace(".", ",")
