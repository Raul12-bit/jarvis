"""Слово активации «Джарвис».

Vosk работает с ограниченной грамматикой. Само по себе слово в грамматике давало бы массу ложных срабатываний:
распознаватель притягивает к нему любую речь. Поэтому рядом стоят фонетические соседи и частые слова, это 2 % одного ядра
против 17–20 % у полного распознавания. «Джарвис» в словаре малой русской модели есть, поэтому обучать ничего не нужно.

Есть две ступени: уверенное попадание будит сразу, а похожее тихо проверяет Whisper. Ошибка здесь ничего не стоит: человек
её не заметит, а имя не потеряется. Цифры замера и способ повторить его: tests/e2e/wake_measure.py.
"""
from __future__ import annotations

import json
import re

from .textproc import tokens

# Все слова проверены: они есть в словаре модели, иначе распознаватель молча их игнорирует.
_NEIGHBORS = """джервис дарвин дарвина тревис сервис сервиса сервиз чарльз чарли шарль харви джордж джо джон джек джей джин джинн
джаз джем джерри джеймс джейсон джонс джокер джунгли жарить жарко жара жаркий арбуз марвин мервин ирвин ирвинг кевин девис дэвис
девид дэвид гарри гарик гарвард варвара шарик шарф дарья дж джа""".split()

_COMMON = """и в не на я быть он с что а по это она этот к но они мы как из у который то за свой весь год от
так о для ты же все тот мочь вы человек такой его сказать только или ещё бы себя один уже до время если сам
когда другой вот говорить наш мой знать стать при чтобы дело жизнь кто первый очень два день её новый рука
даже во со раз где там под можно ну какой после их работа без самый потом надо хотеть ли слово идти большой
должен место иметь ничего сейчас тут лицо каждый друг нет теперь ни глаз тоже тогда видеть вопрос через да
здесь дом потому сторона какая кажется хорошо спасибо пожалуйста привет пока завтра сегодня вчера погода дождь
книга проект код поезд автобус кот шкаф молоко хлеб сыр вечер кухня полка окно музыка включи открой поставь
таймер напомни найди скажи покажи сколько почему зачем сообщение письмо телефон компьютер интернет эй сэр""".split()

WAKE_WORD = "джарвис"
GRAMMAR = sorted({WAKE_WORD, *_NEIGHBORS, *_COMMON}) + ["[unk]"]
_PARTIAL_EVERY = 5  # разбор промежуточного результата раз в 160 мс: JSON на каждый кадр ни к чему
_ALTERNATIVES = 3  # замер на прежнем имени: три варианта вместо одного подняли полноту с 83 % до 96 %, а ложных срабатываний не прибавили
# Что распознаватель слышит вместо имени: самые частые замены из замера. «Дарвин» здесь нарочно нет: замер показал, что он
# не заменяет имя, а только отправлял на проверку любой разговор о Дарвине.
MAYBE_WORDS = frozenset("джервис джей джаз джем джин джинн джа дж тревис чарли".split())


def read_hearing(payload: str) -> tuple[bool, bool]:
    """Разбирает итог распознавателя: (имя стоит первым вариантом, имя или похожее есть среди вариантов)."""
    data = json.loads(payload)
    alternatives = data.get("alternatives")
    if alternatives is None:
        words = data.get("text", "").split()
        return WAKE_WORD in words, WAKE_WORD in words or bool(MAYBE_WORDS & set(words))
    all_words: set[str] = set()
    for alternative in alternatives:
        all_words.update(alternative.get("text", "").split())
    best = alternatives[0].get("text", "").split() if alternatives else []
    return WAKE_WORD in best, WAKE_WORD in all_words or bool(MAYBE_WORDS & all_words)


class WakeSpotter:
    def __init__(self, model) -> None:
        from vosk import KaldiRecognizer

        self._rec = KaldiRecognizer(model, 16000, json.dumps(GRAMMAR, ensure_ascii=False))
        self._rec.SetMaxAlternatives(_ALTERNATIVES)
        self._frames = 0
        self.maybe = False

    def begin(self, audio: bytes = b"") -> None:
        self._rec.Reset()
        self._frames = 0
        self.maybe = False
        if audio:
            self._rec.AcceptWaveform(audio)

    def _judge(self, payload: str) -> bool:
        sure, maybe = read_hearing(payload)
        self.maybe = self.maybe or maybe
        return sure

    def feed(self, frame: bytes) -> bool:
        if self._rec.AcceptWaveform(frame):
            return self._judge(self._rec.Result())
        self._frames += 1
        if self._frames % _PARTIAL_EVERY:
            return False
        return WAKE_WORD in json.loads(self._rec.PartialResult()).get("partial", "").split()

    def finish(self) -> bool:
        return self._judge(self._rec.FinalResult())


def _distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


_LATIN = {"jarvis", "jarviss", "jarvice", "jarves", "jervis", "jarwis", "charvis", "garvis", "harvis", "darvis", "jarwiss", "javis"}
# Настоящие слова рядом с именем: они не обращение, даже если отличаются всего на две буквы.
_NOT_NAME = {"дарвин", "дарвина", "дарвиш", "джаррис", "сервис", "сервиз", "чарльз", "джервин", "травис", "тревис", "харви", "марвин", "дарвины"}
_GREETINGS = {"эй", "привет", "слушай", "окей", "ok", "ау", "алло"}
_VERBS_ABOUT_JARVIS = {"открой", "покажи", "запусти", "разверни", "вызови", "верни", "закрой", "сверни", "спрячь", "выключи", "приложение", "окно"}
_NAME_WORD = re.compile(r"[0-9a-zа-я]+")
_SENTENCES = re.compile(r"(?<=[.!?…])\s+")
# Шум часто съедает «дж»: «Жарвис», «Чарвис», «Гарвис», «Арвис». Держимся за «...рв...» и паузу после слова.
# «Дарвин написал» сюда не подходит: после слова нет паузы. «Жарко, правда?» тоже: в слове нет «рв».
_GARBLED_CALL = re.compile(r"^\s*(?:(?:эй|слушай|ау|привет)[\s,]+)?(?:дж|ж|ч|ш|г|х|д)?[аэе]р?в[а-я]{0,3}(?:[,.!?…:]+|\s*$)")


def _is_name(word: str) -> bool:
    if word in _LATIN:
        return True
    return word not in _NOT_NAME and _distance(word, WAKE_WORD) <= 2 and word.endswith(("с", "з", "ис", "из"))


def _capitalized(text: str) -> str:
    text = text.strip(" ,.:;—–-\n\t")
    return text[:1].upper() + text[1:] if text else ""


def _garbled_call(lowered: str) -> re.Match[str] | None:
    """«Арвис, какая погода»: шум исказил имя, но обращение на месте, первым словом и с паузой после него."""
    return _GARBLED_CALL.match(lowered)


def check_addressed(text: str, lenient: bool = False) -> tuple[bool, str]:
    """Вторая ступень после Vosk: Whisper должен подтвердить, что имя действительно прозвучало.

    Возвращает (обращаются ли к Джарвису, сама просьба). Всё, что сказано до обращения, отбрасывается: рядом могут
    разговаривать. Настоящий случай: «Ты ему базовые функции дал? Угу. Смотри. Джарвис, включи фильм».

    lenient: фразу уже отметил распознаватель на ноутбуке, поэтому искажённое имя в начале тоже считается обращением.
    """
    lowered = text.lower().replace("ё", "е")
    found = [match for match in _NAME_WORD.finditer(lowered) if _is_name(match[0])]
    if not found:
        garbled = _garbled_call(lowered) if lenient else None
        if garbled is None:
            return False, ""
        return True, _capitalized(text[garbled.end():].lstrip(" ,.!?:;—–-\n\t"))
    words = tokens(text)
    if len(words) == 2 and words[0] in _GREETINGS:
        return True, ""
    after_last = text[found[-1].end():]
    # «Открой Джарвиса»: здесь имя и есть то, что надо открыть, стирать его нельзя.
    before_words = tokens(text[:found[-1].start()])
    if not tokens(after_last) and before_words and before_words[-1] in _VERBS_ABOUT_JARVIS:
        return True, _capitalized(text[found[0].end():] if len(found) > 1 else text)
    if tokens(after_last):
        # «Джарвис, какая погода?» → «Какая погода?»
        return True, _capitalized(after_last.lstrip(" ,.!?:;—–-\n\t"))
    if len(found) > 1:
        # «Джарвис, позвони Джарвису»: последнее имя и есть содержание просьбы.
        return True, _capitalized(text[found[0].end():].lstrip(" ,.!?:;—–-\n\t"))
    # Имя в самом конце: «Сколько времени, Джарвис?» Берём только последнюю фразу перед ним.
    before = _SENTENCES.split(text[:found[-1].start()].strip())[-1]
    return True, _capitalized(before)
