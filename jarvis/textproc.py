"""Работа с текстом на пути к голосу: очистка, нарезка потока на фрагменты, сверка с эхом."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MD_MARKS = re.compile(r"[*_`#>~|]+")
_LIST_MARK = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+", re.MULTILINE)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0000200D\U00002B00-\U00002BFF]+"
)
_SPEAKABLE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]")


def clean_for_speech(text: str) -> str:
    """Убирает то, что нельзя произнести. Пустая строка значит, что озвучивать нечего:
    сервис синтеза на тексте из одних знаков возвращает пустоту (проверено замером)."""
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _LIST_MARK.sub("", text)
    text = _MD_MARKS.sub("", text)
    text = _EMOJI.sub("", text)
    text = " ".join(text.split())
    return text if _SPEAKABLE.search(text) else ""


_SENTENCE_END = re.compile(r"[.!?…]+[\"»)]*\s+(?=[А-ЯЁA-Z0-9«\"—–-])|[!?…]+[\"»)]*\s+|\n+")
_SOFT_BREAK = re.compile(r"[,;:]\s+|\s[—–-]\s+")


class SpeechChunker:
    """Режет поток токенов на фрагменты для синтеза.

    Замеры показали, что время до первого звука у облачного голоса растёт с длиной текста,
    поэтому самый первый фрагмент отсекаем по первой запятой, а дальше идём по предложениям.
    """

    def __init__(self, first_min: int = 16, soft_limit: int = 130, hard_limit: int = 190) -> None:
        self.first_min = first_min
        self.soft_limit = soft_limit
        self.hard_limit = hard_limit
        self._buf = ""
        self._first_done = False

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        out: list[str] = []
        while (cut := self._next_cut()) is not None:
            chunk, self._buf = self._buf[:cut], self._buf[cut:]
            if chunk.strip():
                out.append(chunk.strip())
                self._first_done = True
        return out

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []

    def _next_cut(self) -> int | None:
        hard = _SENTENCE_END.search(self._buf)
        if not self._first_done:
            for match in _SOFT_BREAK.finditer(self._buf):
                if match.start() >= self.first_min and (hard is None or match.start() < hard.start()):
                    return match.end()
        if hard is not None:
            return hard.end()
        if len(self._buf) >= self.soft_limit:
            breaks = list(_SOFT_BREAK.finditer(self._buf))
            if breaks and breaks[-1].start() >= self.first_min:
                return breaks[-1].end()
        if len(self._buf) >= self.hard_limit:
            space = self._buf.rfind(" ", 0, self.hard_limit)
            return space + 1 if space > 0 else self.hard_limit
        return None


class ThinkFilter:
    """Вырезает рассуждения <think>…</think>, если модель прислала их прямо в тексте."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self._inside = False
        self._tail = ""

    def feed(self, text: str) -> str:
        data = self._tail + text
        self._tail = ""
        out: list[str] = []
        while data:
            if self._inside:
                end = data.find(self.CLOSE)
                if end < 0:
                    self._tail = data[-(len(self.CLOSE) - 1):]
                    return "".join(out)
                data = data[end + len(self.CLOSE):]
                self._inside = False
            else:
                start = data.find(self.OPEN)
                if start >= 0:
                    out.append(data[:start])
                    data = data[start + len(self.OPEN):]
                    self._inside = True
                    continue
                # Хвост может оказаться началом тега, разорванного между чанками.
                keep = 0
                for size in range(min(len(self.OPEN) - 1, len(data)), 0, -1):
                    if self.OPEN.startswith(data[-size:]):
                        keep = size
                        break
                out.append(data[:-keep] if keep else data)
                self._tail = data[-keep:] if keep else ""
                return "".join(out)
        return "".join(out)

    def flush(self) -> str:
        tail, self._tail = self._tail, ""
        return "" if self._inside else tail


_WORD = re.compile(r"[0-9a-zа-яё]+")
_ECHO_STEM = 5
_ECHO_LONG_PHRASE = 8


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("ё", "е"))


def echo_like(heard: str, spoken: str, threshold: float = 0.6, long_threshold: float = 0.5) -> bool:
    """Похоже ли услышанное на собственную речь Джарвиса, попавшую обратно в микрофон.

    Сравниваем основы слов: офлайн-распознавание путает окончания («деталей» вместо «детали»). Настоящая реплика
    человека редко совпадает с речью Джарвиса на половину длинной фразы, поэтому для длинных порог ниже."""
    heard_tokens = tokens(heard)
    spoken_tokens = tokens(spoken)
    if not heard_tokens or not spoken_tokens:
        return False
    # Длинный ответ не должен «поглощать» короткую реплику человека из одного частого слова.
    if len(heard_tokens) <= 2 and heard_tokens[0] in STOP_WORDS:
        return False
    heard_stems = [word[:_ECHO_STEM] for word in heard_tokens]
    spoken_stems = [word[:_ECHO_STEM] for word in spoken_tokens]
    matcher = SequenceMatcher(None, heard_stems, spoken_stems, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    needed = long_threshold if len(heard_tokens) >= _ECHO_LONG_PHRASE else threshold
    return matched / len(heard_tokens) >= needed


# «Топ»: так офлайн-распознавание услышало «стоп» в сквозном тесте.
STOP_WORDS = {"стоп", "топ", "хватит", "подожди", "погоди", "замолчи", "тихо", "стой", "прекрати", "достаточно"}
# Простое «спасибо» разговор не заканчивает: человек часто продолжает. Конец — только явный.
_GOODBYE = re.compile(
    r"^(?:(?:спасибо|благодарю)[,.!\s]*)?(?:это\s+)?(?:вс[её]|пока|отбой|до\s+связи|можешь\s+отдыхать|свободен|можешь\s+идти"
    r"|не\s+слушай(?:\s+меня)?|перестань\s+слушать|отдыхай|спи)[.!\s]*$",
    re.IGNORECASE,
)


def is_stop_command(text: str) -> bool:
    words = tokens(text)
    return 0 < len(words) <= 2 and words[0] in STOP_WORDS


def is_goodbye(text: str) -> bool:
    return bool(_GOODBYE.match(text.strip()))


# ---------- Подтверждение голосом ----------

_YES = {"да", "давай", "подтверждаю", "подтверди", "выполняй", "конечно", "ага", "угу", "точно", "можно", "хорошо", "ок",
        "окей", "yes", "действуй", "жми", "нажимай", "закрывай", "выключай", "запускай", "открывай", "верно",
        "отправляй", "отправь", "отправить", "звони", "позвони", "звонить", "пиши", "напиши", "выполни", "ладно", "yeah", "yep"}
_NO = {"нет", "не", "неа", "отмена", "отмени", "отменить", "стоп", "хватит", "передумал", "передумала", "no", "погоди"}


def _answer_words(text: str) -> list[str]:
    return [word for word in tokens(text) if word not in ("джарвис", "джервис", "джарвиз", "пожалуйста", "сэр")]


def is_yes(text: str) -> bool:
    """Короткое согласие. Длинная фраза с «да» внутри — это уже другой разговор, а не подтверждение."""
    words = _answer_words(text)
    return 0 < len(words) <= 5 and words[0] in _YES and "нет" not in words and "не" not in words


def is_no(text: str) -> bool:
    words = _answer_words(text)
    return 0 < len(words) <= 6 and words[0] in _NO


# ---------- Просьба о действии и честность ответа ----------

_ACTION_REQUEST = re.compile(
    r"^(?:(?:джарвис|джервис|джарвиз|сэр|пожалуйста|будь добр|а|ну|слушай|можешь|можете|ты можешь|вы можете|мог бы|ты мог бы|"
    r"могли бы|вы могли бы|давай|давайте|сейчас)[\s,]+)*"
    r"(?:включи|выключи|открой|закрой|запусти|останови|поставь|сделай|нажми|кликни|напиши|напечатай|сверни|разверни|"
    r"переключи|перемотай|сохрани|удали|скопируй|вставь|заблокируй|перезагрузи|убавь|прибавь|отмени|продолжи|запомни|"
    r"напомни|позвони|набери|отправь|перешли|включить|выключить|открыть|закрыть|запустить|остановить|поставить|сделать|"
    r"нажать|кликнуть|напечатать|свернуть|развернуть|переключить|перемотать|сохранить|удалить|скопировать|вставить|"
    r"заблокировать|перезагрузить|продолжить|запомнить|напомнить|позвонить|набрать|отправить|написать)\b",
    re.IGNORECASE,
)
_CLAIMS_DONE = re.compile(
    r"(?<!не )\b(?:включил\w?|выключил\w?|открыл\w?|закрыл\w?|запустил\w?|останови\w?л\w?|поставил\w?|нажал\w?|сделал\w?|"
    r"свернул\w?|развернул\w?|переключил\w?|сохранил\w?|удалил\w?|скопировал\w?|напечатал\w?|запомнил\w?|"
    r"отправил\w?|написал\w?|позвонил\w?|звоню|"
    r"включен\w?|включён\w?|открыт\w?|запущен\w?|выключен\w?|закрыт\w?|поставлен\w?|готово|сделано|выполнено)\b",
    re.IGNORECASE,
)


def looks_like_action(text: str) -> bool:
    return bool(_ACTION_REQUEST.match(text.strip()))


def claims_done(text: str) -> bool:
    return bool(_CLAIMS_DONE.search(text.lower().replace("ё", "е")))


# Изредка модель начинает второе сообщение прямо за первым, и в потоке они слипаются:
# «Записала: купить мололь? (Oops).Записала: купить молоко.» Настоящий текст всегда идёт последним.
_MESSAGE_SPLICE = re.compile(r"[.!?…»)](?=[А-ЯЁ])")
_SENTENCE_GAP = re.compile(r"(?<=[.!?…])\s+")


def final_message(text: str) -> str:
    """Оставляет последний вариант ответа и убирает дословный повтор предложения."""
    splices = list(_MESSAGE_SPLICE.finditer(text))
    if splices:
        text = text[splices[-1].end():]
    sentences = _SENTENCE_GAP.split(text.strip())
    kept: list[str] = []
    for sentence in sentences:
        if not kept or tokens(sentence) != tokens(kept[-1]):
            kept.append(sentence)
    return " ".join(kept)


# ---------- Мужской род ----------

_MASCULINE = {
    "проверила": "проверил", "открыла": "открыл", "закрыла": "закрыл", "включила": "включил", "выключила": "выключил",
    "запустила": "запустил", "остановила": "остановил", "нашла": "нашёл", "сделала": "сделал",
    "поняла": "понял", "услышала": "услышал", "посмотрела": "посмотрел", "поставила": "поставил", "запомнила": "запомнил",
    "забыла": "забыл", "удалила": "удалил", "добавила": "добавил", "отправила": "отправил", "написала": "написал",
    "напечатала": "напечатал", "нажала": "нажал", "сохранила": "сохранил", "скопировала": "скопировал", "узнала": "узнал",
    "свернула": "свернул", "развернула": "развернул", "переключила": "переключил", "отменила": "отменил", "подумала": "подумал",
    "прочитала": "прочитал", "увидела": "увидел", "посчитала": "посчитал", "отметила": "отметил", "записала": "записал",
    "обновила": "обновил", "установила": "установил", "выбрала": "выбрал", "перезапустила": "перезапустил",
}
_MASCULINE_AFTER_I = {
    **_MASCULINE,
    "смогла": "смог", "могла": "мог", "пришла": "пришёл", "ушла": "ушёл", "пошла": "пошёл", "шла": "шёл", "ошиблась": "ошибся",
    "дала": "дал", "взяла": "взял", "была": "был", "начала": "начал", "стала": "стал", "сняла": "снял", "знала": "знал",
    "легла": "лёг", "вела": "вёл", "несла": "нёс", "везла": "вёз", "готова": "готов", "рада": "рад", "уверена": "уверен",
    "должна": "должен", "согласна": "согласен", "занята": "занят", "свободна": "свободен", "сама": "сам", "счастлива": "счастлив",
    "обязана": "обязан", "намерена": "намерен", "способна": "способен", "благодарна": "благодарен",
}
_BRIDGE_WORDS = {"не", "уже", "только", "что", "сейчас", "бы", "тоже", "ещё", "еще", "пока", "же", "всё", "все", "лишь",
                 "точно", "просто", "сам", "наконец", "снова", "опять", "вот", "и", "ведь"}
_CLAUSE_WORDS = {"но", "и", "а", "зато", "поэтому", "потом", "затем", "уже", "сразу"}
_AND_WORDS = {"и", "а", "но", "потом", "затем"}
_WORD_OR_GAP = re.compile(r"\w+|\W+")
_OPEN_WORD = re.compile(r"\w+$")
_SENTENCE_MARK = re.compile(r"[.!?…\n]")


class GenderFilter:
    """Модель иногда забывает, что Джарвис мужчина. Правим только надёжные случаи:
    слово сразу после «я» и глагол из списка в самом начале предложения. «Ты сделал» не трогаем.
    Работает на потоке: незаконченное слово ждёт следующего куска."""

    def __init__(self, own_voice: bool = True) -> None:
        self.own_voice = own_voice  # в истории глагол без «я» может быть про героя: «Открыл дверь и вошёл»
        self.after_comma = False  # после действий модель отчитывается за себя: «Spotify нет, открыл поиск»
        self._tail = ""
        self._after_i = False
        self._sentence_start = True
        self._i_said = False  # в этом предложении уже было «я»: «я открыла файл и нашла ошибку»
        self._previous = ""

    def feed(self, delta: str) -> str:
        data = self._tail + delta
        match = _OPEN_WORD.search(data)
        if match:
            self._tail = data[match.start():]
            data = data[:match.start()]
        else:
            self._tail = ""
        return self._process(data)

    def flush(self) -> str:
        data, self._tail = self._tail, ""
        return self._process(data)

    def _process(self, text: str) -> str:
        out: list[str] = []
        for token in _WORD_OR_GAP.findall(text):
            if token[0].isalnum() or token[0] == "_":
                out.append(self._word(token))
                # «Spotify нет, но открыл поиск»: союз не отменяет начало части предложения.
                self._sentence_start = self.after_comma and self._sentence_start and token.lower() in _CLAUSE_WORDS
            else:
                if _SENTENCE_MARK.search(token):
                    self._sentence_start = True
                    self._after_i = False
                    self._i_said = False
                    self._previous = ""
                elif not token.isspace() and "-" not in token:
                    self._after_i = False
                    if self.after_comma and "," in token:
                        self._sentence_start = True
                out.append(token)
        return "".join(out)

    def _word(self, word: str) -> str:
        lower = word.lower()
        replacement: str | None = None
        if self._after_i:
            replacement = _MASCULINE_AFTER_I.get(lower)
            if replacement is None and len(lower) >= 5:
                if lower.endswith("лась"):
                    replacement = lower[:-3] + "ся"
                elif lower.endswith("ла"):
                    replacement = lower[:-1]
        elif self._sentence_start and self.own_voice:
            replacement = _MASCULINE_AFTER_I.get(lower)
        elif self._i_said and self.own_voice and self._previous in _AND_WORDS:
            # «Я проверила почту и готова помогать»: тот же субъект, а «она нашла» сюда не попадёт, перед глаголом нет союза.
            replacement = _MASCULINE_AFTER_I.get(lower)
        if lower == "я":
            self._after_i = True
            self._i_said = True
        elif self._after_i and lower not in _BRIDGE_WORDS:
            self._after_i = False
        self._previous = lower
        if replacement is None:
            return word
        return replacement.capitalize() if word[0].isupper() else replacement
