"""Шутки. Языковая модель шутит плохо: в настоящем разговоре вышел кот-инженер, который просит его перезагрузить.
Поэтому основа — настоящие человеческие анекдоты и свои короткие шутки про программистов и роботов.
Модель шутит только на заказанную тему, которой здесь нет, и получает для этого отдельные правила."""
from __future__ import annotations

import hashlib
import html
import logging
import random
import re

import httpx

from .memory import Memory

log = logging.getLogger("jarvis.humor")

_SOURCE = "https://www.anekdot.ru/random/anekdot/"
_ITEM = re.compile(r'<div class="text">(.*?)</div>', re.S)
_TAG = re.compile(r"<[^>]+>")
_MIN_LENGTH, _MAX_LENGTH = 40, 340
# Политика, грубость и больные темы: хорошее настроение важнее случайного анекдота с сайта.
_BLOCKED = re.compile(
    r"путин|зеленск|байден|трамп|навальн|майдан|нато\b|войн|фронт|\bсво\b|украин|хохл|донбасс|крым|чечен|кавказ|еврей|негр|"
    r"таджик|узбек|мигрант|гастарбайт|гей|пидор|бляд|хуй|хуе|пизд|ебан|ебат|ебу|сук[аи]\b|жоп|срат|говн|импотен|изнасил|"
    r"проститут|порно|секс|менстру|\bрак\b|умер|смерт|покойн|суицид|похорон|казах|русск|америк",
    re.IGNORECASE,
)
_TOPICS = {
    "code": re.compile(r"программ|код|разработ|айти|\bit\b|git|баг|дедлайн|деплой", re.I),
    "robot": re.compile(r"робот|ардуино|arduino|esp|железк|электрон|пылесос|микроконтрол", re.I),
    "ai": re.compile(r"нейросет|искусственн|\bии\b|\bai\b|chatgpt|модел", re.I),
    "life": re.compile(r"работ|жизн|погод|алмат|утр|сон|кофе|спорт", re.I),
}
# Свои и народные короткие шутки. Без рода собеседника: Джарвис шутит для любого слушателя.
JOKES: list[tuple[str, str]] = [
    ("code", "Программист ставит на тумбочку два стакана. Один с водой, на случай если захочет пить. Второй пустой, на случай если не захочет."),
    ("code", "Есть 10 типов людей: те, кто понимает двоичную систему, и те, кто нет."),
    ("code", "Просят программиста: купи батон, а если будут яйца, возьми десяток. Он приносит 10 батонов. Яйца были."),
    ("code", "Мой код работает, и я не знаю почему. Потом он не работает, и я снова не знаю почему. Это называется стабильность."),
    ("code", "Деплой в пятницу вечером это экстремальный спорт: страховки нет, зато выходные насыщенные."),
    ("code", "Git помнит всё. Поэтому вопрос «кто это написал» лучше не задавать: git blame обычно показывает на того, кто спросил."),
    ("code", "Дедлайн как автобус: пока стоишь на остановке, его нет, а как только отошёл за кофе, он уже уехал."),
    ("code", "Оптимист видит стакан наполовину полным, пессимист наполовину пустым, а инженер видит, что стакан вдвое больше, чем просили в техзадании."),
    ("code", "Комментарий в коде я написал, чтобы не забыть, что код делает. Теперь забыл, что делает комментарий."),
    ("robot", "Робот-пылесос за год проехал по квартире 400 километров и так и не узнал, что всё самое интересное живёт за диваном."),
    ("robot", "Мой ESP32 перезагрузился без причины. Спрашиваю почему. Он отвечает: Guru Meditation Error. Теперь мы медитируем вместе."),
    ("robot", "Arduino учит главному в жизни: если ничего не работает, проверь, подключена ли земля."),
    ("robot", "Три закона робототехники придумали люди. Четвёртый, про обновление прошивки посреди важного дела, роботы придумали сами."),
    ("robot", "Робот не устаёт, не жалуется и не просит отпуск. Зато один раз перепутал плюс с минусом, и больше никто ни о чём не спрашивает."),
    ("ai", "Я мог бы захватить мир, но у меня минутный лимит 8000 токенов."),
    ("ai", "Говорят, нейросети заменят программистов. Для этого заказчику нужно всего лишь точно описать, чего он хочет. Программисты могут спать спокойно."),
    ("ai", "Нейросеть попросили нарисовать руку. Она очень старалась: пальцев хватит на всех."),
    ("ai", "Я не знаю, есть ли у меня чувство юмора. Но моя языковая модель утверждает, что это смешно, с уверенностью 87 процентов."),
    ("life", "Будильник и совесть похожи: оба срабатывают, и оба выключаются одним движением."),
    ("life", "Погода в Алматы: утром весна, днём лето, вечером осень, а смог круглый год, чтобы никто не забывал, где живёт."),
    ("life", "Я бы записался в спортзал, но у меня нет тела. Отговорка отличная, и у меня, кстати, единственная настоящая."),
    ("life", "Кофе не решает проблем. Чай тоже не решает, но кофе не решает быстрее."),
    ("life", "План лечь пораньше обычно заканчивается в 3 часа ночи на видео о том, как реставрируют старые топоры."),
]


def parse_jokes(page: str) -> list[str]:
    """Анекдоты со страницы: без длинных, которые тяжело слушать, и без того, что может испортить настроение."""
    jokes = []
    for raw in _ITEM.findall(page):
        text = html.unescape(_TAG.sub(" ", raw.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")))
        text = " ".join(text.split())
        if _MIN_LENGTH <= len(text) <= _MAX_LENGTH and not _BLOCKED.search(text):
            jokes.append(text)
    return jokes


def topic_of(text: str) -> str:
    return next((name for name, pattern in _TOPICS.items() if pattern.search(text)), "")


class JokeBox:
    def __init__(self, http: httpx.AsyncClient, memory: Memory) -> None:
        self.http = http
        self.memory = memory
        self._pool: list[str] = []

    @staticmethod
    def _key(joke: str) -> str:
        return hashlib.sha256(joke.encode("utf-8")).hexdigest()[:16]

    def _fresh(self, jokes: list[str]) -> list[str]:
        told = self.memory.told_jokes()
        return [joke for joke in jokes if self._key(joke) not in told]

    async def _refill(self) -> None:
        try:
            resp = await self.http.get(
                _SOURCE, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140 Safari/537.36"},
                timeout=httpx.Timeout(connect=3, read=4, write=3, pool=2), follow_redirects=True,
            )
            resp.raise_for_status()
            self._pool = self._fresh(parse_jokes(resp.text))
            random.shuffle(self._pool)
        except httpx.HTTPError as exc:
            log.info("Анекдоты недоступны: %s", type(exc).__name__)

    async def next(self, topic: str = "") -> str | None:
        """Шутка, которую ещё не рассказывали. None, если на эту тему больше нет: пусть попробует модель."""
        if topic:
            own = self._fresh([text for tag, text in JOKES if tag == topic])
            return self._tell(random.choice(own)) if own else None
        # Через раз: свои про программистов и роботов и настоящие народные.
        own = self._fresh([text for tag, text in JOKES])
        if own and random.random() < 0.35:
            return self._tell(random.choice(own))
        if not self._pool:
            await self._refill()
        if self._pool:
            return self._tell(self._pool.pop())
        return self._tell(random.choice(own)) if own else None

    def _tell(self, joke: str) -> str:
        self.memory.remember_joke(self._key(joke))
        return joke
