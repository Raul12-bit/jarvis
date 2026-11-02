"""Команды без языковой модели. Срабатывают мгновенно, не тратят лимит и не могут ничего выдумать.

instant_answer вызывается первым на каждую реплику и понимает только короткие команды целиком:
«включи второе видео» да, а «расскажи, как включить второе видео» — нет.
local_answer работает лишь когда нет ключа, связи или кончился лимит.
"""
from __future__ import annotations

import re
import time
from datetime import datetime

from .humor import topic_of
from .persona import now_phrase
from .tools import CONFIRM_PREFIX, KNOWN_SITES, ToolBox

_NUMBERS = {
    "одну": 1, "одна": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7,
    "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "шестьдесят": 60, "полчаса": 30, "час": 60,
}
_ORDINALS = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "четвёрт": 4, "пят": 5, "шест": 6, "седьм": 7, "восьм": 8,
    "девят": 9, "десят": 10,
}
_UNITS = {"сек": 1, "мин": 60, "час": 3600}
_TIMER = re.compile(r"(?:таймер|напомни|будильник).*?(?:на|через)\s+(\d+|[а-я]+)\s*(сек|мин|час)", re.IGNORECASE)
_OPEN = re.compile(r"^(?:открой|запусти)\s+(?:сайт\s+|приложение\s+|программу\s+)?(.+?)[.!]*$", re.IGNORECASE)
_SITE = re.compile(r"^[\w-]+(?:\.[\w-]+)+(?:/\S*)?$")
_MEDIA = (
    (re.compile(r"\b(?:громче|прибавь звук)\b", re.I), "volume_up"),
    (re.compile(r"\b(?:тише|убавь звук)\b", re.I), "volume_down"),
    (re.compile(r"\bбез звука\b|\bвыключи звук\b", re.I), "mute"),
    (re.compile(r"\bпауз[ау]\b", re.I), "pause"),
    (re.compile(r"\bпродолжи музыку\b", re.I), "play"),
    (re.compile(r"\bследующ\w+\s+(?:трек|песн\w+)\b", re.I), "next"),
    (re.compile(r"\bпредыдущ\w+\s+(?:трек|песн\w+)\b", re.I), "previous"),
)

# Вежливые слова в начале и конце не меняют смысл короткой команды.
_POLITE_HEAD = re.compile(
    r"^(?:(?:джарвис|джервис|джарвиз|сэр|пожалуйста|будь добр|а|ну|слушай|можешь|можете|ты можешь|вы можете|мог бы|ты мог бы|могли бы|вы могли бы|"
    r"давай|давайте)[\s,]+)+", re.I
)
_POLITE_TAIL = re.compile(r"[\s,]+(?:пожалуйста|джарвис|джервис|джарвиз|сэр)$", re.I)
_VERB = r"(?:включи(?:ть)?|открой|открыть|запусти(?:ть)?|поставь|поставить|нажми(?:\s+на)?|покажи|выбери|воспроизведи)"
_VIDEO_WORD = r"(?:видео|видос|ролик|клип)"
_ORDINAL_WORD = r"((?:перв|втор|трет|четв[её]рт|пят|шест|седьм|восьм|девят|десят)\w*|\d{1,2}(?:-?(?:е|й|ое|ой|ую|ый))?)"
_ORDINAL_VIDEO = re.compile(rf"^(?:{_VERB}\s+)?(?:видео\s+(?:номер\s+)?{_ORDINAL_WORD}|{_ORDINAL_WORD}\s+{_VIDEO_WORD})$", re.I)
_ORDINAL_BARE = re.compile(rf"^(?:{_VERB}\s+)?{_ORDINAL_WORD}$", re.I)
_VIDEO_MEMORY_SECONDS = 300
_PAUSE = re.compile(r"^(?:(?:поставь|поставить)\s+)?(?:на\s+)?пауз[ау](?:\s+(?:видео|музыку|фильм|песню))?$|^останови(?:ть)?\s+(?:видео|музыку|фильм|песню)$", re.I)
_RESUME = re.compile(r"^(?:продолжи(?:ть)?|сними с паузы|сними\s+паузу)(?:\s+(?:видео|музыку|фильм|песню))?$", re.I)
_BARE_MEDIA = re.compile(r"^(?:продолжи(?:ть)?|следующ\w+|предыдущ\w+)$", re.I)
_NEXT = re.compile(r"^следующ\w+(?:\s+(?:трек|песн\w+|видео|клип))?$", re.I)
_PREVIOUS = re.compile(r"^предыдущ\w+(?:\s+(?:трек|песн\w+|видео|клип))?$", re.I)
_LOUDER = re.compile(r"^(?:сделай\s+)?(?:громче|погромче)$|^прибавь(?:\s+звук)?$", re.I)
_QUIETER = re.compile(r"^(?:сделай\s+)?(?:тише|потише)$|^убавь(?:\s+звук)?$", re.I)
_SET_VOLUME = re.compile(r"^(?:сделай\s+|поставь\s+)?(?:громкость|звук)\s+(?:на\s+)?(\d{1,3})(?:\s*(?:процент\w*|%))?$", re.I)
_MUTE = re.compile(r"^выключи(?:ть)?\s+звук$|^без\s+звука$", re.I)
_UNMUTE = re.compile(r"^включи(?:ть)?\s+звук$", re.I)
_TIME = re.compile(r"^(?:(?:сколько|который|какое)\s+(?:сейчас\s+)?(?:времени|время|час)(?:\s+сейчас)?|сколько\s+сейчас\s+время)(?:\s+в\s+алмат\w+)?$", re.I)
_DATE = re.compile(r"^(?:какое\s+сегодня\s+число|какой\s+сегодня\s+день|какая\s+сегодня\s+дата|какое\s+число)$", re.I)
_EXTEND = re.compile(r"^(?:дай\s+|давай\s+|можно\s+)?ещ[её]\s+(?:на\s+)?(\d{1,3}|[а-я]+)\s*(?:минут\w*)?$", re.I)
_SCREEN_TIME = re.compile(r"^сколько\s+я\s+(?:сегодня\s+)?(?:играл\w*|смотрел\w*|работал\w*|сижу\s+за\s+ноутбуком)(?:\s+сегодня)?$", re.I)
_OPEN_KNOWN = re.compile(r"^(?:открой|открыть|запусти|запустить)\s+(?:сайт\s+)?(.+)$", re.I)
_ORDINAL_LINK = re.compile(rf"^(?:{_VERB}\s+)?{_ORDINAL_WORD}\s+(?:ссылк\w+|результат\w*|сайт\w*)$", re.I)
_MIC_OFF = re.compile(r"^выключи(?:ть)?\s+(?:свой\s+)?микрофон(?:\s+и\s+не\s+слушай(?:\s+меня)?)?$", re.I)
_SELF_TARGET = (r"(?:себя|джарвис\w*|джервис\w*|jarvis|сво[её]\s+(?:окно|приложение|лицо)|свой\s+интерфейс|свою\s+(?:программу|панель)|"
                r"тво[её]\s+(?:окно|приложение)|твой\s+интерфейс|окно\s+джарвис\w*|приложение\s+джарвис\w*|интерфейс\s+джарвис\w*|интерфейс)")
_SHOW_SELF = re.compile(rf"^(?:открой|покажи|запусти|разверни|верни|вызови)\s+{_SELF_TARGET}(?:\s+джарвис\w*)?$|^покажись$|^появись$", re.I)
_HIDE_SELF = re.compile(rf"^(?:спрячь|сверни|закрой|убери)\s+{_SELF_TARGET}(?:\s+джарвис\w*)?$|^спрячься$|^скройся$", re.I)
_QUIT_SELF = re.compile(r"^(?:выключись(?:\s+совсем)?|отключись\s+совсем|выключи\s+(?:себя|джарвис\w*)|заверши\s+работу)$", re.I)
_MASCOT_WORD = r"(?:талисман\w*|фигурк\w*|человечк\w*|мини-?реактор\w*|реактор\w*)"
_MASCOT_OFF = re.compile(rf"^(?:убери|спрячь|выключи|скрой)\s+{_MASCOT_WORD}$", re.I)
_MASCOT_ON = re.compile(rf"^(?:верни|покажи|включи)\s+{_MASCOT_WORD}$", re.I)
_SAY_ALOUD = re.compile(r"^(?:скажи|произнеси|прочитай)\s+(?:что-?\s?нибудь\s+|что-?\s?то\s+)?(?:вслух|на\s+ноут\w*|на\s+компьютер\w*|в\s+колонки|голосом)(?:\s*[,:]?\s*(.+))?$", re.I)
# «Скажи на ноуте что-нибудь приятное»: что именно сказать, решает модель. Произносить это буквально было бы глупо.
_VAGUE_SPEECH = re.compile(r"^(?:что-?\s?нибудь|что-?\s?то)\b", re.I)
_DICTATION_ON = re.compile(r"^(?:включи|начни|запусти)\s+(?:режим\s+)?диктовк\w+$|^режим\s+диктовки$", re.I)
_BROWSER_KEYS = (
    (re.compile(r"^(?:открой\s+)?(?:новую\s+)?вкладку$", re.I), "ctrl+t"),
    (re.compile(r"^закрой\s+(?:эту\s+|текущую\s+)?вкладку$", re.I), "ctrl+w"),
    (re.compile(r"^закрой\s+все\s+вкладки$", re.I), "ctrl+shift+w"),
    (re.compile(r"^(?:открой\s+)?следующ\w+\s+вкладк\w+$", re.I), "ctrl+tab"),
    (re.compile(r"^(?:открой\s+)?предыдущ\w+\s+вкладк\w+$", re.I), "ctrl+shift+tab"),
    (re.compile(r"^верни\s+(?:закрытую\s+)?вкладку$", re.I), "ctrl+shift+t"),
    (re.compile(r"^обнови\s+страницу$", re.I), "f5"),
    (re.compile(r"^(?:вернись\s+назад|страницу\s+назад|назад\s+на\s+страницу)$", re.I), "alt+left"),
)
_ROUTINE_PREFIX = re.compile(r"^(?:запусти|включи|выполни|начни)\s+(?:сценарий\s+)?|^сценарий\s+", re.I)
_JOKE = re.compile(r"^(?:(?:расскажи|скажи|давай|хочу)\s+)?(?:\w+\s+)?(?:шутк\w*|анекдот\w*)$|^пошути$|^рассмеши(?:\s+меня)?$|^(?:скажи\s+)?что-?(?:нибудь|то)\s+смешное$", re.I)
_JOKE_TOPIC = re.compile(r"^(?:(?:расскажи|скажи|давай|пошути|хочу)\s+)?(?:\w+\s+)?(?:шутк\w*|анекдот\w*\s+)?про\s+(.+)$", re.I)
_MORE_JOKE = re.compile(r"^(?:ещ[её]|еще\s+одну|ещ[её]\s+одну|ещ[её]\s+(?:шутк\w*|анекдот\w*))$", re.I)
_JOKE_MEMORY_SECONDS = 180
_MINIMIZE_ALL = re.compile(r"^сверни\s+(?:все|всё)(?:\s+окна)?$", re.I)
_MACHINE = r"(?:ноутбук|ноут|компьютер|комп|пк)"
_SHUTDOWN = re.compile(rf"^(?:выключи|выключить)\s+{_MACHINE}$", re.I)
_RESTART = re.compile(rf"^(?:перезагрузи|перезагрузить|перезапусти)\s+{_MACHINE}$", re.I)
_SLEEP = re.compile(rf"^(?:усыпи\s+{_MACHINE}|отправь\s+{_MACHINE}\s+в\s+сон|переведи\s+{_MACHINE}\s+в\s+сон)$", re.I)
_LOCK = re.compile(rf"^(?:заблокируй|заблокировать)\s+(?:экран|{_MACHINE})$", re.I)
_CANCEL_SHUTDOWN = re.compile(r"^(?:отмени|отменить)\s+(?:выключение|перезагрузку)$", re.I)


def _without_polite_head(text: str) -> str:
    """Как _simplify, только имя в конце остаётся: в «открой Джарвис» это не обращение, а то, что надо открыть."""
    cleaned = " ".join(text.lower().replace("ё", "е").split()).strip(" .,!?…")
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = _POLITE_HEAD.sub("", cleaned).strip(" .,!?…")
    return cleaned


def _simplify(text: str) -> str:
    cleaned = " ".join(text.lower().replace("ё", "е").split()).strip(" .,!?…")
    previous = None
    while cleaned != previous:
        previous = cleaned
        cleaned = _POLITE_HEAD.sub("", cleaned)
        cleaned = _POLITE_TAIL.sub("", cleaned).strip(" .,!?…")
    return cleaned


def _ordinal(word: str) -> int | None:
    digits = re.match(r"\d+", word)
    if digits:
        return int(digits[0])
    word = word.replace("ё", "е")
    for stem, value in _ORDINALS.items():
        if word.startswith(stem.replace("ё", "е")):
            return value
    return None


async def instant_answer(text: str, tools: ToolBox) -> str | None:
    """Ответ на короткую команду или None, если её должна разбирать модель."""
    self_answer = await _self_answer(_without_polite_head(text), tools)
    if self_answer is not None:
        return self_answer
    command = _simplify(text)
    if not command or len(command) > 60:
        return None
    now = datetime.now()

    if _TIME.match(command):
        return f"Сейчас {now:%H:%M}."
    if _DATE.match(command):
        return f"Сегодня {now_phrase(now).rsplit(',', 1)[0]}."

    if _MIC_OFF.match(command):
        tools.on_ui("mic_off")
        return "Выключил микрофон. Включить можно кнопкой или клавишей M."
    answer = await _joke_answer(command, tools)
    if answer is not None:
        return answer

    answer = await _routine_answer(command, tools)
    if answer is not None:
        return answer

    answer = await _browser_answer(command, tools)
    if answer is not None:
        return answer

    if (match := _ORDINAL_LINK.match(command)) is not None:
        position = _ordinal(match[1])
        if position is not None:
            result = await tools.run("open_link", position=position)
            return None if result.startswith("Ошибка") else result
    if (match := _ORDINAL_VIDEO.match(command)) is not None:
        return await _play_ordinal(match[1] or match[2], tools)
    # Просто «второе» имеет смысл, только если только что шла речь о видео.
    desktop = tools.desktop
    if (match := _ORDINAL_BARE.match(command)) is not None and desktop is not None:
        if time.monotonic() - desktop.videos_at < _VIDEO_MEMORY_SECONDS:
            return await _play_ordinal(match[1], tools)

    # Просто «продолжи» или «следующий» про плеер, только если он есть: иначе это про разговор.
    if _BARE_MEDIA.match(command) and (tools.media is None or not await tools.media.current()):
        return None
    for pattern, action in ((_PAUSE, "pause"), (_RESUME, "play"), (_NEXT, "next"), (_PREVIOUS, "previous"),
                            (_LOUDER, "volume_up"), (_QUIETER, "volume_down"), (_MUTE, "mute"), (_UNMUTE, "unmute")):
        if pattern.match(command):
            result = await tools.run("media_control", action=action)
            return None if result.startswith("Ошибка") else result
    if (match := _SET_VOLUME.match(command)) is not None:
        result = await tools.run("media_control", action="set_volume", value=int(match[1]))
        return None if result.startswith("Ошибка") else result

    if _MINIMIZE_ALL.match(command):
        result = await tools.run("window_control", action="minimize_all")
        return None if result.startswith("Ошибка") else result

    if (match := _EXTEND.match(command)) is not None and tools.tracker is not None:
        amount = int(match[1]) if match[1].isdigit() else _NUMBERS.get(match[1])
        if amount:
            result = await tools.run("time_control", action="extend", minutes=amount)
            return None if result.startswith(("Ошибка", "Сейчас")) else result

    if _SCREEN_TIME.match(command) and tools.tracker is not None:
        return await tools.run("time_control", action="report")

    for pattern, action in ((_SHUTDOWN, "shutdown"), (_RESTART, "restart"), (_SLEEP, "sleep"), (_LOCK, "lock"), (_CANCEL_SHUTDOWN, "cancel_shutdown")):
        if pattern.match(command):
            result = await tools.run("system_control", action=action)
            # Опасное подтверждает человек: спрашиваем сразу, без модели, которая мог бы спросить мимо инструмента.
            if result.startswith(CONFIRM_PREFIX) and tools.pending is not None:
                return tools.pending.question
            return result

    if (match := _OPEN_KNOWN.match(command)) is not None:
        target = match[1].strip()
        if target in KNOWN_SITES:
            result = await tools.run("open_app", name=target)
            if not result.startswith("Ошибка") and not result.startswith("Нужно"):
                if tools.desktop is not None:
                    tools.desktop.videos_at = time.monotonic()
                return f"Открыл {target}."
    return None


async def _self_answer(command: str, tools: ToolBox) -> str | None:
    """Всё, что Джарвис делает с самой собой. Настоящий случай: «открой себя» и ответ «у меня нет интерфейса»."""
    if not command:
        return None
    if _SHOW_SELF.match(command):
        return await tools.run("jarvis_window", action="show")
    if _HIDE_SELF.match(command):
        return await tools.run("jarvis_window", action="hide")
    if _MASCOT_OFF.match(command):
        return await tools.run("jarvis_window", action="mascot_off")
    if _MASCOT_ON.match(command):
        return await tools.run("jarvis_window", action="mascot_on")
    if _QUIT_SELF.match(command) and tools.on_self is not None:
        tools._confirm("Выключить меня совсем, сэр? Я перестану слышать вас и отвечать с телефона.", lambda: tools.on_self("quit"))  # type: ignore[misc]  # noqa: SLF001
        return tools.pending.question if tools.pending is not None else None
    if _DICTATION_ON.match(command):
        return await tools.run("dictation", on=True)
    match = _SAY_ALOUD.match(command)
    if match is not None and match[1] and not _VAGUE_SPEECH.match(match[1].strip()):
        return await tools.run("speak_aloud", text=re.sub(r"^что\s+", "", match[1].strip()))
    return None


async def _routine_answer(command: str, tools: ToolBox) -> str | None:
    """Сценарий по названию: «рабочий режим», «запусти сценарий рабочий режим»."""
    names = {item["name"] for item in tools.memory.routines()}
    if not names:
        return None
    for candidate in (command, _ROUTINE_PREFIX.sub("", command)):
        if candidate in names:
            return await tools.run("routines", action="run", name=candidate)
    return None


async def _browser_answer(command: str, tools: ToolBox) -> str | None:
    """Вкладки: только если сверху браузер, иначе решит модель."""
    if tools.desktop is None:
        return None
    for pattern, combo in _BROWSER_KEYS:
        if not pattern.match(command):
            continue
        window = tools.desktop.target()
        if window is None or not window.is_browser:
            return None
        result = await tools.run("press_keys", keys=combo)
        if result.startswith(CONFIRM_PREFIX) and tools.pending is not None:
            return tools.pending.question
        return None if result.startswith("Ошибка") else result
    return None


async def _joke_answer(command: str, tools: ToolBox) -> str | None:
    """Шутка без языковой модели. На тему, которой нет в запасе, отвечает модель: None."""
    if _JOKE.match(command) or (_MORE_JOKE.match(command) and time.monotonic() - tools.last_joke_at < _JOKE_MEMORY_SECONDS):
        return await tools.tell_joke()
    match = _JOKE_TOPIC.match(command)
    if match is not None and re.search(r"шутк|анекдот|пошути", command, re.I):
        topic = topic_of(match[1])
        return await tools.tell_joke(topic) if topic else None
    return None


async def _play_ordinal(word: str, tools: ToolBox) -> str | None:
    position = _ordinal(word)
    if position is None:
        return None
    result = await tools.run("play_video", position=position)
    # Не вышло: пусть попробует модель, у неё есть другие способы посмотреть на экран.
    return None if result.startswith("Ошибка") else result


async def local_answer(text: str, tools: ToolBox) -> str | None:
    """Реплика, если запрос удалось выполнить локально, иначе None."""
    lowered = " ".join(text.lower().split())
    now = datetime.now()

    if re.search(r"котор\w+ час|сколько времени|сколько сейчас времени|сколько сейчас время", lowered):
        return f"Сейчас {now:%H:%M}."
    if re.search(r"како[йе] сегодня (?:день|число)|какая сегодня дата|какое число", lowered):
        return f"Сегодня {now_phrase(now).rsplit(',', 1)[0]}."

    match = _TIMER.search(lowered)
    if match:
        raw = match[1]
        amount = int(raw) if raw.isdigit() else _NUMBERS.get(raw)
        if amount:
            result = await tools.set_timer(label="таймер", seconds=amount * _UNITS[match[2]])
            return result.split(",")[0] + "." if not result.startswith("Ошибка") else None

    for pattern, action in _MEDIA:
        if pattern.search(lowered):
            result = await tools.media_control(action)
            return result if not result.startswith("Ошибка") else "Не получилось."

    match = _OPEN.match(lowered)
    if match:
        target = match[1].strip()
        result = await (tools.open_website(target) if _SITE.match(target) else tools.open_app(target))
        return "Открываю." if not result.startswith(("Ошибка", "Приложение", "Нужно")) else "Не нашёл, что открыть."

    if "погод" in lowered:
        place = re.search(r"\bв\s+([а-яё-]{3,})", lowered)
        result = await tools.get_weather(place[1] if place else "")
        return result.split(" Сегодня:")[0] if not result.startswith(("Город", "Ошибка")) else None

    if re.search(r"заряд|батаре|загрузк\w+ (?:процессора|компьютера|ноутбука)", lowered):
        return await tools.system_status()
    return None
