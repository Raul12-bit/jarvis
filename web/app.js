import { MicCapture, VoicePlayer } from './audio.js';
import { ReactorCore } from './core.js';

const token = new URLSearchParams(location.search).get('t') ?? '';
const $ = (id) => document.getElementById(id);

const STATE_TEXT = {
  sleep: 'Ожидание',
  listening: 'Слушаю, сэр',
  hearing: 'Слышу вас',
  thinking: 'Обрабатываю',
  speaking: 'Говорю',
  muted: 'Микрофон выключен',
  error: 'Нет связи',
};
const TOOL_TEXT = {
  web_search: 'Поиск в интернете',
  read_page: 'Чтение страницы',
  get_weather: 'Погода и воздух',
  currency_rate: 'Курс Нацбанка',
  calculate: 'Расчёт',
  open_website: 'Открытие сайта',
  open_app: 'Запуск приложения',
  play_youtube: 'Поиск на YouTube',
  play_video: 'Видео на странице',
  screen_items: 'Чтение окна',
  click_item: 'Нажатие',
  type_text: 'Ввод текста',
  press_keys: 'Клавиши',
  read_screen_text: 'Чтение текста окна',
  look_at_screen: 'Взгляд на экран',
  window_control: 'Управление окнами',
  media_control: 'Звук и плеер',
  system_control: 'Система',
  files: 'Файлы',
  clipboard: 'Буфер обмена',
  timers: 'Таймеры',
  notes: 'Дела',
  time_control: 'Контроль времени',
  daily_briefing: 'Сводка за день',
  facts: 'Память',
  telegram: 'Telegram',
  news: 'Новости',
  open_link: 'Ссылка на странице',
  request_tools: 'Подбор инструментов',
  jarvis_window: 'Окно Джарвиса',
  click_text: 'Надпись на экране',
  selected_text: 'Выделенный текст',
  system_info: 'Сведения о ноутбуке',
  processes: 'Программы',
  radio: 'Wi-Fi и Bluetooth',
  run_command: 'Команда',
  speak_aloud: 'Голос на ноутбуке',
  send_to_phone: 'На телефон',
  look_at_camera: 'Камера',
  routines: 'Сценарии',
  dictation: 'Диктовка',
};
const BRAND_TEXT = {
  sleep: 'Системы в норме',
  listening: 'Канал открыт',
  hearing: 'Приём речи',
  thinking: 'Анализ запроса',
  speaking: 'Передача голоса',
  muted: 'Микрофон выключен',
  error: 'Связь потеряна',
};
const TYPE_RATE = 75; // знаков в секунду: текст появляется ровно, даже если приходит рывками

const ui = {
  state: 'sleep',
  mode: 'sleep',
  wakeReady: false,
  settings: null,
  model: { status: 'idle', progress: 0 },
  micWanted: false,
  micPending: false,
  micError: '',
  timers: [],
  timerTick: 0,
  focusLeft: 0,
  focusAt: 0,
  focusTick: 0,
  messages: new Map(),
  typing: new Set(),
  typingRaf: 0,
  typingLast: 0,
  subtitleTimer: 0,
  noticeTimer: 0,
  lookTimer: 0,
  stateTimer: 0,
  settingsSaveTimer: 0,
  ws: null,
  retry: 0,
  replaced: false,
  readySent: false,
  spaceDown: false,
  phone: null,
  net: { down: 0, up: 0 },
};

let core = null;
try {
  core = new ReactorCore($('core'), $('coreBox'), $('coreWrap'));
  core.start();
} catch (error) {
  console.warn('Ядро работает в упрощённом виде:', error);
  document.body.classList.add('no-webgl');
}

const player = new VoicePlayer({
  onTurnStart: (turn) => send({ type: 'playback', turn, event: 'start' }),
  onTurnDone: (turn) => send({ type: 'playback', turn, event: 'done' }),
  onChunkStart: (header, duration) => showSubtitle(header.text, duration),
  onIdle: () => hideSubtitle(),
});

if (core) {
  core.sampler = () => (player.playing ? player.sample() : null);
}

const mic = new MicCapture({
  onFrame: (pcm) => {
    if (ui.ws?.readyState === WebSocket.OPEN) ui.ws.send(pcm);
  },
  onLevel: (rms) => {
    core?.setMic(rms);
    renderMicLevel(rms);
  },
});

// ---------- Связь с сервером ----------

function send(message) {
  if (ui.ws?.readyState === WebSocket.OPEN) ui.ws.send(JSON.stringify(message));
}

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws?t=${encodeURIComponent(token)}`);
  ws.binaryType = 'arraybuffer';
  ui.ws = ws;
  ws.onopen = () => {
    ui.retry = 0;
    ui.readySent = false;
  };
  ws.onmessage = ({ data }) => {
    if (typeof data === 'string') {
      handle(JSON.parse(data));
    } else {
      handleAudio(data);
    }
  };
  ws.onclose = (event) => {
    if (ui.ws !== ws) return;
    if (event.code === 4000) {
      // Джарвис запустили ещё раз: новое окно уже работает, это больше не нужно.
      ui.replaced = true;
      setState('error');
      setHint('Джарвис открыта в другом окне.');
      mic.stop();
      window.close();
      return;
    }
    setState('error');
    setHint('Переподключаюсь…');
    player.stop();
    // Скрытое окно без Джарвиса незачем держать: у него включён микрофон, а показать его теперь некому.
    if (document.hidden && ui.retry >= 5) {
      mic.stop();
      window.close();
      return;
    }
    ui.retry = Math.min(ui.retry + 1, 6);
    setTimeout(connect, 400 * 2 ** ui.retry);
  };
}

function handleAudio(buffer) {
  const view = new DataView(buffer);
  const size = view.getUint32(0, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, size)));
  player.resume();
  player.feed(header, buffer.slice(4 + size));
}

function handle(msg) {
  switch (msg.type) {
    case 'hello':
      onHello(msg);
      break;
    case 'state':
      ui.mode = msg.mode;
      ui.wakeReady = msg.wake_ready;
      setState(msg.state);
      renderProtocols();
      break;
    case 'user_text':
      addUser(msg.turn, msg.text, msg.source);
      break;
    case 'mic_set':
      enableMic(msg.on);
      break;
    case 'camera':
      grabCamera(msg.id);
      break;
    case 'phone':
      renderPhone(msg);
      renderProtocols();
      break;
    case 'user_retract':
      removeMessage(`u${msg.turn}`);
      removeMessage(`k${msg.turn}`);
      break;
    case 'assistant_start':
      addJarvis(msg.turn);
      break;
    case 'assistant_delta':
      appendJarvis(msg.turn, msg.text);
      break;
    case 'assistant_end':
      endJarvis(msg.turn, msg);
      break;
    case 'tool':
      onTool(msg);
      break;
    case 'looking':
      showLookBadge();
      break;
    case 'audio_done':
      player.finish(msg.turn, msg.count);
      break;
    case 'audio_stop':
      player.stop();
      break;
    case 'duck':
      player.duck(msg.on);
      break;
    case 'chime':
      player.resume();
      player.chime(msg.kind);
      break;
    case 'timers':
      setTimers(msg.items);
      break;
    case 'notes':
      renderNotes(msg.items);
      break;
    case 'activity':
      renderActivity(msg);
      break;
    case 'ambient':
      renderAmbient(msg);
      break;
    case 'stats':
      $('loadLabel').textContent = `${msg.cpu.toLocaleString('ru', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} % · ${msg.ram_mb} МБ`;
      if (msg.system) renderSystem(msg.system);
      break;
    case 'metrics':
      if (msg.total_ms) $('latencyLabel').textContent = `${(msg.total_ms / 1000).toLocaleString('ru', { minimumFractionDigits: 1, maximumFractionDigits: 1 })} с`;
      break;
    case 'brain':
      $('brainLabel').textContent = msg.label;
      break;
    case 'brain_down':
      if (msg.kind === 'no_key' || msg.kind === 'bad_key') openSettings();
      break;
    case 'model_status':
      ui.model = msg;
      renderHint();
      renderProtocols();
      break;
    case 'notice':
      showNotice(msg.text);
      break;
    case 'error':
      showNotice(msg.text, true);
      break;
    case 'mascot':
      ui.mascotAlive = msg.alive;
      renderProtocols();
      break;
    default:
      break;
  }
}

function onHello(msg) {
  ui.settings = msg.settings;
  ui.wakeReady = msg.wake_ready;
  ui.mode = msg.mode;
  ui.model = msg.model;
  ui.micRemembered = msg.mic_wanted;
  setTimers(msg.timers);
  renderNotes(msg.notes ?? []);
  if (msg.phone) renderPhone(msg.phone);
  if (msg.activity) renderActivity(msg.activity);
  renderAmbient(msg.ambient ?? {});
  $('brainLabel').textContent = msg.brain || (msg.needs_key ? 'нет ключа' : '—');
  ui.needsKey = msg.needs_key;
  ui.mascotAlive = msg.mascot_alive;
  core?.setPowerSaver(ui.settings.power_saver);
  player.setEffect(ui.settings.voice_fx);
  fillSettings();
  finishBoot();
  renderProtocols();
  if (!ui.settings.onboarded) {
    if (!$('onboarding').open) $('onboarding').showModal();
    setState(msg.state);
    return;
  }
  if (!ui.readySent) {
    ui.readySent = true;
    startup();
  }
}

// Интерфейс проявляется, как только есть первые настоящие данные: ждать конца анимации никто не должен.
function finishBoot() {
  if (!document.body.classList.contains('is-booting')) return;
  document.body.classList.remove('is-booting');
  core?.surge();
}

async function startup() {
  // Скрытая страница открывается сама: выключенный человеком микрофон не должен включиться за его спиной.
  await enableMic(ui.micRemembered !== false);
  send({ type: 'ready', mic: mic.active, visible: !document.hidden });
}

// ---------- Микрофон ----------

async function enableMic(on) {
  ui.micWanted = on;
  if (on) {
    ui.micPending = true;
    renderHint();
    try {
      await mic.start(ui.settings?.mic_device ?? '');
      ui.micError = '';
      refreshDevices();
    } catch (error) {
      ui.micError = error.name || 'Error';
      console.warn('Микрофон недоступен:', error);
    } finally {
      ui.micPending = false;
    }
  } else {
    await mic.stop();
    ui.micError = '';
    renderMicLevel(0);
  }
  renderMic();
  send({ type: 'mic', on: mic.active, wanted: on });
  renderHint();
  renderProtocols();
}

function renderMic() {
  const btn = $('micBtn');
  btn.setAttribute('aria-pressed', String(mic.active));
  btn.setAttribute('aria-label', mic.active ? 'Выключить микрофон' : 'Включить микрофон');
}

function renderMicLevel(rms) {
  const level = Math.min(1, rms * 9);
  const ring = $('micRing');
  ring.style.opacity = String(level * 0.9);
  ring.style.transform = `scale(${1 + level * 0.4})`;
}

async function refreshDevices() {
  const select = $('micSelect');
  const devices = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'audioinput');
  select.replaceChildren(new Option('Системный по умолчанию', ''));
  for (const device of devices) {
    if (device.deviceId === 'default' || device.deviceId === 'communications') continue;
    select.add(new Option(device.label || 'Микрофон', device.deviceId));
  }
  select.value = ui.settings?.mic_device ?? '';
}

// ---------- Состояние и подсказки ----------

function setState(state) {
  const changed = ui.state !== state;
  ui.state = state;
  document.body.dataset.state = state;
  core?.setState(state);
  $('brandState').textContent = BRAND_TEXT[state] ?? '';
  $('coreBtn').setAttribute(
    'aria-label',
    state === 'speaking' || state === 'thinking' ? 'Остановить Джарвиса' : ui.mode === 'active' ? 'Отпустить Джарвиса' : 'Активировать Джарвиса',
  );
  const label = $('stateLabel');
  clearTimeout(ui.stateTimer);
  if (changed && label.textContent !== 'Запуск') {
    // Подпись меняем через короткое затухание, чтобы текст не прыгал.
    label.classList.add('is-changing');
    ui.stateTimer = setTimeout(() => {
      label.textContent = STATE_TEXT[ui.state] ?? ui.state;
      label.classList.remove('is-changing');
    }, 130);
  } else {
    label.textContent = STATE_TEXT[state] ?? state;
    label.classList.remove('is-changing');
  }
  renderHint();
}

function setHint(text) {
  $('stateHint').replaceChildren(text);
}

function renderHint() {
  const { state, model, micError } = ui;
  if (ui.micPending) {
    setHint('Разрешите доступ к микрофону в запросе вверху окна');
    return;
  }
  if (micError && (state === 'sleep' || state === 'muted')) {
    const fix = document.createElement('button');
    fix.type = 'button';
    fix.textContent = 'Повторить';
    fix.onclick = () => enableMic(true);
    const reason = micError === 'NotAllowedError'
      ? 'Нет доступа к микрофону. Разрешите его браузеру и в параметрах конфиденциальности Windows. '
      : micError === 'NotFoundError'
        ? 'Микрофон не найден. '
        : 'Микрофон не запустился. ';
    $('stateHint').replaceChildren(reason, fix);
    return;
  }
  const hints = {
    listening: 'Говорите, имя повторять не нужно',
    hearing: '',
    thinking: '',
    speaking: 'Можно перебить голосом или нажать Esc',
    muted: 'Можно писать текстом',
    error: '',
  };
  if (state === 'sleep') {
    if (ui.settings?.wake_word && !ui.wakeReady) {
      const percent = Math.round((model.progress ?? 0) * 100);
      if (model.status === 'downloading') setHint(`Загружаю модуль слуха: ${percent} %. Пока удерживайте пробел`);
      else if (model.status === 'failed') setHint('Модуль слуха не загрузился. Удерживайте пробел и говорите');
      else setHint('Готовлю слух. Пока удерживайте пробел');
    } else if (ui.wakeReady) {
      setHint('Скажите «Джарвис» или удерживайте пробел');
    } else {
      setHint('Нажмите на реактор или удерживайте пробел');
    }
    return;
  }
  if (state in hints) setHint(hints[state]);
}

function showSubtitle(text, duration = 0) {
  clearTimeout(ui.subtitleTimer);
  const el = $('subtitle');
  const words = text.split(/\s+/).filter(Boolean);
  const total = words.join(' ').length || 1;
  const reveal = duration > 0 ? duration * 0.92 : Math.min(3, text.length * 0.05);
  let passed = 0;
  // Слово появляется примерно тогда, когда его произносят: задержка пропорциональна числу знаков до него.
  const spans = words.flatMap((word, index) => {
    const span = document.createElement('span');
    span.textContent = word;
    span.style.animationDelay = `${((passed / total) * reveal).toFixed(3)}s`;
    passed += word.length + 1;
    return index < words.length - 1 ? [span, ' '] : [span];
  });
  el.replaceChildren(...spans);
  el.classList.add('is-on');
}

function hideSubtitle() {
  clearTimeout(ui.subtitleTimer);
  ui.subtitleTimer = setTimeout(() => $('subtitle').classList.remove('is-on'), 900);
}

function showNotice(text, isError = false) {
  const el = $('notice');
  el.textContent = text;
  el.hidden = false;
  el.classList.toggle('is-error', isError);
  clearTimeout(ui.noticeTimer);
  ui.noticeTimer = setTimeout(() => {
    el.hidden = true;
  }, 9000);
}

function showLookBadge() {
  const badge = $('lookBadge');
  badge.hidden = false;
  clearTimeout(ui.lookTimer);
  ui.lookTimer = setTimeout(() => {
    badge.hidden = true;
  }, 4000);
}

function renderAmbient(data) {
  if (data.city) $('placeLabel').textContent = data.city;
  else if (ui.settings?.city) $('placeLabel').textContent = ui.settings.city;
  if (data.temp === undefined) return;
  const air = data.aqi ? ` · воздух ${data.aqi}` : '';
  $('ambientLabel').textContent = `${data.temp}° ${data.sky ?? ''}${air}`.trim();
}

// ---------- Журнал разговора ----------

function logEl() {
  return $('log');
}

function stickToBottom(action) {
  const log = logEl();
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  action();
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function makeMessage(key, who, css) {
  $('logEmpty').style.display = 'none';
  const article = document.createElement('article');
  article.className = `msg ${css}`;
  const label = document.createElement('span');
  label.className = 'msg__who';
  const name = document.createElement('span');
  name.textContent = who;
  const when = document.createElement('span');
  when.className = 'msg__time';
  when.textContent = new Date().toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' });
  label.append(name, when);
  const tools = document.createElement('ul');
  tools.className = 'msg__tools';
  tools.hidden = true;
  const text = document.createElement('p');
  text.className = 'msg__text';
  article.append(label, tools, text);
  const entry = { article, text, tools, toolItems: new Map(), pending: '', ended: false };
  ui.messages.set(key, entry);
  stickToBottom(() => logEl().append(article));
  return entry;
}

function addUser(turn, text, source) {
  const key = turn ? `u${turn}` : `u0-${Date.now()}`;
  makeMessage(key, source === 'phone' ? 'Сэр, с телефона' : 'Сэр', 'msg--user').text.textContent = text;
}

function addJarvis(turn) {
  if (!ui.messages.has(`k${turn}`)) makeMessage(`k${turn}`, 'J.A.R.V.I.S.', 'msg--jarvis');
}

function appendJarvis(turn, delta) {
  addJarvis(turn);
  const entry = ui.messages.get(`k${turn}`);
  entry.pending += delta;
  ui.typing.add(entry);
  startTyping();
}

function startTyping() {
  if (ui.typingRaf) return;
  ui.typingLast = performance.now();
  const step = (now) => {
    const budget = Math.max(1, Math.round(((now - ui.typingLast) / 1000) * TYPE_RATE));
    ui.typingLast = now;
    stickToBottom(() => {
      for (const entry of ui.typing) {
        // Если очередь выросла, ускоряемся: текст не должен сильно отставать от голоса.
        const speedUp = Math.ceil(entry.pending.length / 45);
        const part = entry.pending.slice(0, budget + speedUp);
        entry.pending = entry.pending.slice(part.length);
        entry.text.textContent += part;
        if (!entry.pending) ui.typing.delete(entry);
      }
    });
    ui.typingRaf = ui.typing.size && !document.hidden ? requestAnimationFrame(step) : 0;
    if (ui.typing.size && document.hidden) flushTyping();
  };
  if (document.hidden) {
    flushTyping();
    return;
  }
  ui.typingRaf = requestAnimationFrame(step);
}

function flushTyping() {
  // В скрытом окне кадров нет: дописываем сразу, чтобы ничего не потерять.
  for (const entry of ui.typing) {
    entry.text.textContent += entry.pending;
    entry.pending = '';
  }
  ui.typing.clear();
}

function endJarvis(turn, msg) {
  const entry = ui.messages.get(`k${turn}`);
  if (!entry) return;
  const empty = !entry.text.textContent.trim() && !entry.pending.trim() && entry.toolItems.size === 0;
  if (msg.discard || empty) removeMessage(`k${turn}`);
}

function removeMessage(key) {
  const entry = ui.messages.get(key);
  if (entry) {
    ui.typing.delete(entry);
    entry.article.remove();
    ui.messages.delete(key);
  }
}

function onTool(msg) {
  addJarvis(msg.turn);
  const entry = ui.messages.get(`k${msg.turn}`);
  let item = entry.toolItems.get(msg.name);
  if (!item) {
    item = document.createElement('li');
    item.className = 'msg__tool';
    item.textContent = TOOL_TEXT[msg.name] ?? msg.name;
    entry.toolItems.set(msg.name, item);
    stickToBottom(() => {
      entry.tools.append(item);
      entry.tools.hidden = false;
    });
  }
  if (msg.status === 'start') item.classList.remove('is-done', 'is-failed');
  if (msg.status === 'end') item.classList.add(msg.ok ? 'is-done' : 'is-failed');
}

// ---------- Таймеры ----------

function setTimers(items) {
  ui.timers = items ?? [];
  clearInterval(ui.timerTick);
  $('timersPanel').hidden = ui.timers.length === 0;
  renderTimers(true);
  if (ui.timers.length) ui.timerTick = setInterval(() => renderTimers(false), 1000);
}

function renderTimers(rebuild) {
  const now = Date.now() / 1000;
  const list = $('timers');
  if (!rebuild) {
    // Каждую секунду меняем только цифры: пересборка списка заново запускала бы анимацию появления.
    for (const row of list.children) {
      const timer = ui.timers.find((item) => String(item.id) === row.dataset.id);
      if (timer) row.querySelector('.timer__time').textContent = formatLeft(Math.max(0, Math.round(timer.due - now)));
    }
    return;
  }
  const nodes = ui.timers.map((timer) => {
    const li = document.createElement('li');
    li.className = 'timer';
    li.dataset.id = timer.id;
    const left = Math.max(0, Math.round(timer.due - now));
    const time = document.createElement('span');
    time.className = 'timer__time';
    time.textContent = formatLeft(left);
    const label = document.createElement('span');
    label.className = 'timer__label';
    label.textContent = timer.label;
    label.title = timer.label;
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.setAttribute('aria-label', `Отменить таймер: ${timer.label}`);
    cancel.innerHTML = '<svg class="icon"><use href="#i-close"/></svg>';
    cancel.onclick = () => api(`/api/timers/${timer.id}`, { method: 'DELETE' });
    li.append(time, label, cancel);
    return li;
  });
  list.replaceChildren(...nodes);
}

function formatLeft(seconds) {
  const days = Math.floor(seconds / 86400);
  if (days) return `${days} д ${Math.floor((seconds % 86400) / 3600)} ч`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  const mm = String(minutes).padStart(2, '0');
  const ss = String(secs).padStart(2, '0');
  return hours ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

// ---------- Дела ----------

function renderNotes(items) {
  const list = $('notes');
  $('notesEmpty').hidden = items.length > 0;
  const known = new Map([...list.children].map((row) => [row.dataset.id, row]));
  const rows = items.map((note) => {
    const existing = known.get(String(note.id));
    if (existing) return existing;
    const li = document.createElement('li');
    li.className = 'note';
    li.dataset.id = note.id;
    const check = document.createElement('button');
    check.type = 'button';
    check.className = 'note__check';
    check.setAttribute('aria-label', `Сделано: ${note.text}`);
    check.innerHTML = '<svg class="icon"><use href="#i-check"/></svg>';
    check.onclick = () => finishNote(li, `/api/notes/${note.id}/done`, 'POST');
    const text = document.createElement('span');
    text.className = 'note__text';
    text.textContent = note.text;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'note__remove';
    remove.setAttribute('aria-label', `Удалить: ${note.text}`);
    remove.innerHTML = '<svg class="icon"><use href="#i-close"/></svg>';
    remove.onclick = () => finishNote(li, `/api/notes/${note.id}`, 'DELETE');
    li.append(check, text, remove);
    return li;
  });
  list.replaceChildren(...rows);
}

async function finishNote(row, path, method) {
  row.classList.add('is-leaving');
  try {
    const [{ items }] = await Promise.all([api(path, { method }), new Promise((done) => { setTimeout(done, 260); })]);
    renderNotes(items);
  } catch (error) {
    row.classList.remove('is-leaving');
    showNotice(error.message, true);
  }
}

// ---------- Контроль времени ----------

function formatSpent(seconds) {
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
}

function renderActivity(data) {
  const rows = [['game', 'Game'], ['video', 'Video'], ['work', 'Work']];
  for (const [kind, suffix] of rows) {
    const spent = data.totals?.[kind] ?? 0;
    const limit = (data.limits?.[kind] ?? 0) * 60;
    const session = data.sessions?.[kind] ?? 0;
    $(`meter${suffix}`).textContent = formatSpent(spent);
    const share = limit > 0 ? Math.min(1, session / limit) : 0;
    $(`fill${suffix}`).style.transform = `scaleX(${share})`;
    $(`meter${suffix}`).closest('.meter').classList.toggle('is-over', limit > 0 && session >= limit);
  }
  const now = data.current ?? {};
  const doing = { game: 'Сейчас игра', video: 'Сейчас видео', work: 'Сейчас работа' }[now.kind];
  $('nowDoing').textContent = doing && now.app ? `${doing}: ${now.app}` : '';
  setFocus(data.focus_left ?? 0);
}

function setFocus(left) {
  ui.focusLeft = left;
  ui.focusAt = performance.now();
  clearInterval(ui.focusTick);
  renderFocus();
  if (left > 0) ui.focusTick = setInterval(renderFocus, 1000);
}

function renderFocus() {
  const left = Math.max(0, Math.round(ui.focusLeft - (performance.now() - ui.focusAt) / 1000));
  const button = $('focusBtn');
  const on = left > 0;
  button.parentElement.classList.toggle('is-on', on);
  button.textContent = on ? `Фокус: ${formatLeft(left)}. Закончить` : 'Режим фокуса на 25 минут';
  if (!on) clearInterval(ui.focusTick);
}

// ---------- Камера и голос для талисмана ----------

async function grabCamera(id) {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 } } });
    try {
      const video = document.createElement('video');
      video.muted = true;
      video.playsInline = true;
      video.srcObject = stream;
      await video.play();
      // Первые кадры у камер тёмные: ей нужно время, чтобы подстроить экспозицию.
      await new Promise((resolve) => setTimeout(resolve, 900));
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      canvas.getContext('2d').drawImage(video, 0, 0);
      send({ type: 'camera_frame', id, data: canvas.toDataURL('image/jpeg', 0.85).split(',')[1] });
    } finally {
      stream.getTracks().forEach((track) => track.stop());
    }
  } catch (error) {
    send({ type: 'camera_frame', id, error: error.name });
  }
}

function startLevelReports() {
  // Талисман двигает ртом по настоящей громкости голоса. Когда окно скрыто, он обходится ритмом речи сам.
  setInterval(() => {
    if (!player.playing || document.hidden || !ui.settings?.mascot) return;
    const sample = player.sample();
    if (sample) send({ type: 'level', v: Number(sample.level.toFixed(2)) });
  }, 90);
}

// ---------- Телефон ----------

function renderPhone(data) {
  ui.phone = data;
  const code = $('phoneCode');
  code.hidden = !data.code;
  code.textContent = data.code ? `Отправьте боту ${data.bot ? `@${data.bot} ` : ''}код: ${data.code}` : '';
  const state = $('phoneState');
  state.className = 'form-note';
  if (data.error) {
    state.textContent = data.error;
    state.classList.add('is-bad');
  } else if (data.paired) {
    state.textContent = `Телефон подключён${data.bot ? `: пишите боту @${data.bot}` : ''}.`;
    state.classList.add('is-ok');
  } else {
    state.textContent = '';
  }
  $('phoneSteps').hidden = data.configured;
  $('phoneForm').hidden = data.configured;
  $('phoneForget').hidden = !data.configured;
}

function bindPhone() {
  $('phoneForm').onsubmit = async (event) => {
    event.preventDefault();
    const input = $('phoneToken');
    const note = $('phoneNote');
    const token = input.value.trim();
    note.className = 'form-note';
    if (!token) {
      note.textContent = 'Сначала вставьте токен.';
      note.classList.add('is-bad');
      return;
    }
    note.textContent = 'Проверяю…';
    try {
      const result = await api('/api/phone/token', { method: 'POST', body: JSON.stringify({ token }) });
      note.textContent = result.ok ? '' : result.message;
      note.classList.toggle('is-bad', !result.ok);
      if (result.ok) input.value = '';
      renderPhone(result.phone);
    } catch (error) {
      note.textContent = /422/.test(error.message) || /string|pattern/i.test(error.message)
        ? 'Это не похоже на токен: скопируйте его целиком из сообщения BotFather.'
        : error.message;
      note.classList.add('is-bad');
    }
  };
  $('phoneForget').onclick = async () => {
    renderPhone(await api('/api/phone', { method: 'DELETE' }));
    renderProtocols();
  };
  $('awakeToggle').onchange = (e) => saveSettings({ keep_awake: e.target.checked });
  $('wakeCheckToggle').onchange = (e) => saveSettings({ wake_cloud_check: e.target.checked });
  $('shellToggle').onchange = (e) => saveSettings({ phone_shell: e.target.checked });
  $('cameraToggle').onchange = (e) => saveSettings({ allow_camera: e.target.checked });
  $('anyPathToggle').onchange = (e) => saveSettings({ phone_any_path: e.target.checked });
  $('alertsToggle').onchange = (e) => saveSettings({ phone_alerts: e.target.checked });
  $('voiceFxToggle').onchange = (e) => {
    player.setEffect(e.target.checked);
    saveSettings({ voice_fx: e.target.checked });
  };
  $('pinForm').onsubmit = async (event) => {
    event.preventDefault();
    const input = $('pinInput');
    const note = $('pinNote');
    note.className = 'form-note';
    const pin = input.value.trim();
    if (pin && !/^\d{4,32}$/.test(pin)) {
      note.textContent = 'Нужны только цифры, не меньше четырёх.';
      note.classList.add('is-bad');
      return;
    }
    try {
      ui.settings = await api('/api/phone/pin', { method: 'POST', body: JSON.stringify({ pin }) });
      input.value = '';
      note.textContent = pin ? 'ПИН сохранён. На телефоне: /unlock ПИН.' : 'ПИН снят.';
      note.classList.add('is-ok');
      fillSettings();
    } catch (error) {
      note.textContent = error.message;
      note.classList.add('is-bad');
    }
  };
  $('mascotToggle').onchange = (e) => {
    saveSettings({ mascot: e.target.checked });
  };
  $('captionsToggle').onchange = (e) => saveSettings({ mascot_captions: e.target.checked });
  $('mascotFullscreenToggle').onchange = (e) => saveSettings({ mascot_hide_fullscreen: e.target.checked });
  $('proactiveToggle').onchange = (e) => saveSettings({ proactive: e.target.checked });
  $('mascotSize').onchange = (e) => saveSettings({ mascot_size: e.target.value });
  $('shutdownBtn').onclick = async () => {
    await api('/api/shutdown', { method: 'POST' });
    window.close();
  };
}

// ---------- Приборы ----------

function setGauge(id, share, value, warm = 0.75, hot = 0.92) {
  const gauge = $(id);
  gauge.querySelector('[data-value]').textContent = value;
  gauge.querySelector('[data-fill]').style.transform = `scaleX(${Math.min(1, Math.max(0, share))})`;
  gauge.classList.toggle('is-warm', share >= warm && share < hot);
  gauge.classList.toggle('is-hot', share >= hot);
}

function formatRate(bytes) {
  if (bytes >= 2 ** 20) return `${(bytes / 2 ** 20).toLocaleString('ru', { maximumFractionDigits: 1 })} МБ/с`;
  return `${Math.round(bytes / 1024)} КБ/с`;
}

// Только то, что прислал сервер, а сервер только то, что измерил: выдуманных цифр на этой панели нет.
function renderSystem(data) {
  setGauge('gaugeCpu', data.cpu / 100, `${Math.round(data.cpu)} %`);
  setGauge('gaugeRam', data.ram_used / data.ram_total, `${(data.ram_used / 2 ** 30).toFixed(1)} / ${(data.ram_total / 2 ** 30).toFixed(1)} ГБ`, 0.9, 0.97);
  setGauge('gaugeDisk', 1 - data.disk_free / data.disk_total, `своб. ${(data.disk_free / 2 ** 30).toFixed(0)} ГБ`, 0.9, 0.97);
  const battery = $('gaugeBattery');
  battery.hidden = data.battery === null;
  if (data.battery !== null) {
    battery.querySelector('[data-name]').textContent = data.plugged ? 'Батарея, заряжается' : 'Батарея';
    setGauge('gaugeBattery', data.battery / 100, `${Math.round(data.battery)} %`, 2, 2);
    battery.classList.toggle('is-warm', !data.plugged && data.battery <= 30 && data.battery > 15);
    battery.classList.toggle('is-hot', !data.plugged && data.battery <= 15);
  }
  const line = $('netLine');
  // Скорость первой: имя сети длинное и обрезается, а цифры должны быть видны всегда. Целиком имя в подсказке.
  line.textContent = data.internet === false
    ? 'Интернета нет'
    : `↓ ${formatRate(data.net_down)} ↑ ${formatRate(data.net_up)} ${data.wifi ?? ''}`.trim();
  line.title = data.wifi ? `Сеть ${data.wifi}` : 'Скорость сети прямо сейчас';
}

function setProtocol(id, kind, text) {
  const row = $(id);
  row.classList.remove('is-on', 'is-wait', 'is-bad');
  if (kind) row.classList.add(`is-${kind}`);
  row.querySelector('[data-state]').textContent = text;
}

function renderProtocols() {
  const s = ui.settings;
  if (!s) return;
  if (ui.needsKey) setProtocol('protoBrain', 'bad', 'нет ключа Groq');
  else setProtocol('protoBrain', 'on', s.groq_key_set ? 'Groq подключён' : 'Gemini подключён');

  if (!s.wake_word) setProtocol('protoEar', '', 'только пробел');
  else if (ui.wakeReady) setProtocol('protoEar', 'on', 'слышу «Джарвис»');
  else if (ui.model?.status === 'failed') setProtocol('protoEar', 'bad', 'не загрузился');
  else if (ui.model?.status === 'downloading') setProtocol('protoEar', 'wait', `загрузка ${Math.round((ui.model.progress ?? 0) * 100)} %`);
  else setProtocol('protoEar', 'wait', 'готовлю');

  if (ui.micPending) setProtocol('protoMic', 'wait', 'ждёт разрешения');
  else if (ui.micError) setProtocol('protoMic', 'bad', 'нет доступа');
  else setProtocol('protoMic', mic.active ? 'on' : '', mic.active ? 'включён' : 'выключен');

  if (!s.mascot) setProtocol('protoMascot', '', 'выключен');
  else if (ui.mascotAlive === false) setProtocol('protoMascot', 'bad', 'не запустился');
  else if (ui.mascotAlive === true) setProtocol('protoMascot', 'on', 'на экране');
  else setProtocol('protoMascot', 'wait', 'запускаю');

  const phone = ui.phone;
  if (phone?.error) setProtocol('protoPhone', 'bad', 'ошибка токена');
  else if (phone?.paired) setProtocol('protoPhone', 'on', phone.bot ? `@${phone.bot}` : 'подключён');
  else if (phone?.configured) setProtocol('protoPhone', 'wait', 'ждёт кода');
  else setProtocol('protoPhone', '', 'не подключён');
}

function startClock() {
  const clock = $('clock');
  const tick = () => {
    clock.textContent = new Date().toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };
  tick();
  setInterval(tick, 1000);
}

// ---------- REST ----------

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { 'X-Jarvis-Token': token, 'Content-Type': 'application/json', ...(options.headers ?? {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : `Код ответа ${response.status}`);
  }
  return response.json();
}

// ---------- Настройки ----------

function fillSettings() {
  const s = ui.settings;
  const voice = $('voiceSelect');
  if (!voice.options.length) {
    for (const item of s.voices) voice.add(new Option(item.label, item.id));
  }
  voice.value = s.voice;
  $('voiceFxToggle').checked = s.voice_fx;
  $('anyPathToggle').checked = s.phone_any_path;
  $('alertsToggle').checked = s.phone_alerts;
  $('pinInput').placeholder = s.phone_pin_set ? 'ПИН задан. Пусто и Сохранить: снять' : 'Цифры, не меньше четырёх';
  $('rateRange').value = s.speech_rate;
  $('rateValue').textContent = `${s.speech_rate > 0 ? '+' : ''}${s.speech_rate} %`;
  $('wakeToggle').checked = s.wake_word;
  $('bargeToggle').checked = s.barge_in;
  $('followRange').value = s.followup_seconds;
  $('followValue').textContent = `${s.followup_seconds} с`;
  $('powerToggle').checked = s.power_saver;
  $('screenToggle').checked = s.allow_screen;
  $('trackToggle').checked = s.track_time;
  $('closeGameToggle').checked = s.game_action === 'close';
  $('autostartToggle').checked = s.autostart;
  $('awakeToggle').checked = s.keep_awake;
  $('wakeCheckToggle').checked = s.wake_cloud_check;
  $('shellToggle').checked = s.phone_shell;
  $('cameraToggle').checked = s.allow_camera;
  $('mascotToggle').checked = s.mascot;
  $('captionsToggle').checked = s.mascot_captions;
  $('mascotFullscreenToggle').checked = s.mascot_hide_fullscreen;
  $('proactiveToggle').checked = s.proactive;
  $('mascotSize').value = s.mascot_size;
  for (const [id, field] of [['limitGame', 'limit_game_minutes'], ['limitVideo', 'limit_video_minutes'], ['limitWork', 'limit_work_minutes']]) {
    $(id).value = s[field];
    $(`${id}Value`).textContent = limitText(s[field]);
  }
  if (document.activeElement !== $('nameInput')) $('nameInput').value = s.user_name;
  if (document.activeElement !== $('cityInput')) $('cityInput').value = s.city;
  for (const provider of ['groq', 'gemini']) {
    const input = $(`${provider}Key`);
    input.placeholder = s[`${provider}_key_set`] ? `Сохранён: ${s[`${provider}_key_hint`]}` : 'Вставьте ключ';
  }
}

function limitText(minutes) {
  return Number(minutes) === 0 ? 'выключено' : `${minutes} мин`;
}

function saveSettings(patch, delay = 0) {
  clearTimeout(ui.settingsSaveTimer);
  ui.settingsSaveTimer = setTimeout(async () => {
    try {
      ui.settings = await api('/api/settings', { method: 'POST', body: JSON.stringify(patch) });
      renderProtocols();
    } catch (error) {
      showNotice(`Настройка не сохранилась: ${error.message}`, true);
    }
  }, delay);
}

async function openSettings() {
  if ($('settings').open) return;
  if (ui.settings?.onboarded === false) return;
  $('settings').showModal();
  await loadFacts();
}

function closeSheet(sheet) {
  if (!sheet.open || sheet.classList.contains('is-closing')) return;
  // У <dialog> нет анимации закрытия: сначала проигрываем её сами, потом закрываем по-настоящему.
  sheet.classList.add('is-closing');
  setTimeout(() => {
    sheet.classList.remove('is-closing');
    sheet.close();
  }, 210);
}

async function loadFacts() {
  try {
    const { facts } = await api('/api/memory');
    renderFacts(facts);
  } catch (error) {
    showNotice(error.message, true);
  }
}

function renderFacts(facts) {
  $('factsEmpty').hidden = facts.length > 0;
  $('factsEmpty').style.display = facts.length ? 'none' : '';
  $('factsList').replaceChildren(...facts.map((fact) => {
    const li = document.createElement('li');
    li.className = 'fact';
    const text = document.createElement('span');
    text.textContent = fact.text;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.setAttribute('aria-label', `Забыть: ${fact.text}`);
    remove.innerHTML = '<svg class="icon"><use href="#i-close"/></svg>';
    remove.onclick = async () => renderFacts((await api(`/api/memory/${fact.id}`, { method: 'DELETE' })).facts);
    li.append(text, remove);
    return li;
  }));
}

async function saveKey(provider, inputId, noteId) {
  const input = $(inputId);
  const note = $(noteId);
  const key = input.value.trim();
  note.className = 'form-note';
  if (!key) {
    note.textContent = 'Сначала вставьте ключ.';
    note.classList.add('is-bad');
    return false;
  }
  note.textContent = 'Проверяю…';
  try {
    const result = await api(`/api/keys/${provider}`, { method: 'POST', body: JSON.stringify({ key }) });
    note.textContent = result.ok ? `${result.message}: ответ за ${result.ms} мс` : result.message;
    note.classList.add(result.ok ? 'is-ok' : 'is-bad');
    if (result.ok) input.value = '';
    return result.ok;
  } catch (error) {
    note.textContent = /string|pattern|length/i.test(error.message) || /422/.test(error.message)
      ? 'Это не похоже на ключ: скопируйте его целиком, без пробелов.'
      : error.message;
    note.classList.add('is-bad');
    return false;
  }
}

function bindSettings() {
  $('settingsBtn').onclick = openSettings;
  $('settingsClose').onclick = () => closeSheet($('settings'));
  $('settings').addEventListener('click', (event) => {
    if (event.target === $('settings')) closeSheet($('settings'));
  });
  $('settings').addEventListener('cancel', (event) => {
    event.preventDefault();
    closeSheet($('settings'));
  });

  $('voiceSelect').onchange = (e) => saveSettings({ voice: e.target.value });
  $('voicePreview').onclick = () => api('/api/voice/preview', {
    method: 'POST',
    body: JSON.stringify({ voice: $('voiceSelect').value, rate: Number($('rateRange').value) }),
  }).catch((error) => showNotice(error.message, true));
  $('rateRange').oninput = (e) => {
    const value = Number(e.target.value);
    $('rateValue').textContent = `${value > 0 ? '+' : ''}${value} %`;
    saveSettings({ speech_rate: value }, 300);
  };
  $('followRange').oninput = (e) => {
    $('followValue').textContent = `${e.target.value} с`;
    saveSettings({ followup_seconds: Number(e.target.value) }, 300);
  };
  $('wakeToggle').onchange = (e) => saveSettings({ wake_word: e.target.checked });
  $('bargeToggle').onchange = (e) => saveSettings({ barge_in: e.target.checked });
  $('screenToggle').onchange = (e) => saveSettings({ allow_screen: e.target.checked });
  $('trackToggle').onchange = (e) => saveSettings({ track_time: e.target.checked });
  $('closeGameToggle').onchange = (e) => saveSettings({ game_action: e.target.checked ? 'close' : 'warn' });
  $('autostartToggle').onchange = (e) => saveSettings({ autostart: e.target.checked });
  for (const [id, field] of [['limitGame', 'limit_game_minutes'], ['limitVideo', 'limit_video_minutes'], ['limitWork', 'limit_work_minutes']]) {
    $(id).oninput = (e) => {
      $(`${id}Value`).textContent = limitText(e.target.value);
      saveSettings({ [field]: Number(e.target.value) }, 350);
    };
  }
  $('powerToggle').onchange = (e) => {
    core?.setPowerSaver(e.target.checked);
    saveSettings({ power_saver: e.target.checked });
  };
  $('nameInput').oninput = (e) => saveSettings({ user_name: e.target.value }, 500);
  $('cityInput').oninput = (e) => {
    $('placeLabel').textContent = e.target.value || '—';
    saveSettings({ city: e.target.value }, 500);
  };
  $('micSelect').onchange = async (e) => {
    saveSettings({ mic_device: e.target.value });
    if (ui.settings) ui.settings.mic_device = e.target.value;
    await enableMic(true);
  };

  $('groqForm').onsubmit = async (e) => {
    e.preventDefault();
    await saveKey('groq', 'groqKey', 'groqNote');
  };
  $('geminiForm').onsubmit = async (e) => {
    e.preventDefault();
    await saveKey('gemini', 'geminiKey', 'geminiNote');
  };
  for (const button of document.querySelectorAll('[data-open]')) {
    button.onclick = () => api(`/api/open/${button.dataset.open}`, { method: 'POST' });
  }
  for (const button of document.querySelectorAll('[data-delete-key]')) {
    button.onclick = async () => {
      ui.settings = await api(`/api/keys/${button.dataset.deleteKey}`, { method: 'DELETE' });
      fillSettings();
    };
  }
  $('clearHistory').onclick = async () => {
    await api('/api/history/clear', { method: 'POST' });
    showNotice('История разговора очищена.');
  };
  $('selfCheck').onclick = runSelfCheck;
}

async function runSelfCheck() {
  const button = $('selfCheck');
  const list = $('checkList');
  button.disabled = true;
  button.textContent = 'Проверяю, это до минуты…';
  list.replaceChildren();
  try {
    const { results } = await api('/api/selfcheck', { method: 'POST' });
    list.replaceChildren(...results.map((item) => {
      const li = document.createElement('li');
      li.className = `check${item.ok ? '' : ' is-bad'}`;
      const name = document.createElement('span');
      name.className = 'check__name';
      name.textContent = item.name;
      const ms = document.createElement('span');
      ms.className = 'check__ms';
      ms.textContent = item.ms ? `${item.ms} мс` : '';
      const detail = document.createElement('span');
      detail.className = 'check__detail';
      detail.textContent = item.detail;
      li.append(name, ms, detail);
      return li;
    }));
  } catch (error) {
    showNotice(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = 'Проверить все сервисы';
  }
}

function bindOnboarding() {
  const showStepTwo = () => {
    $('onbStepKey').hidden = true;
    $('onbStepYou').hidden = false;
    $('onbName').value = ui.settings?.user_name ?? '';
    $('onbCity').value = ui.settings?.city ?? '';
    $('onbName').focus();
  };
  $('onbOpenGroq').onclick = () => api('/api/open/groq', { method: 'POST' });
  $('onbKeyForm').onsubmit = async (e) => {
    e.preventDefault();
    if (await saveKey('groq', 'onbKey', 'onbKeyResult')) setTimeout(showStepTwo, 700);
  };
  $('onbSkip').onclick = showStepTwo;
  $('onbFinish').onclick = async () => {
    try {
      ui.settings = await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({ user_name: $('onbName').value, city: $('onbCity').value, onboarded: true }),
      });
    } catch (error) {
      showNotice(error.message, true);
      return;
    }
    closeSheet($('onboarding'));
    await player.resume();
    await enableMic(true);
    ui.readySent = true;
    send({ type: 'ready', mic: mic.active, visible: !document.hidden });
    send({ type: 'onboarded' });
  };
  $('onboarding').addEventListener('cancel', (event) => event.preventDefault());
}

// ---------- Управление ----------

function isTyping(target) {
  return target instanceof HTMLElement && target.closest('input, textarea, select, dialog, button');
}

function setTab(tab) {
  document.body.dataset.tab = tab;
  $('tabTalk').setAttribute('aria-selected', String(tab === 'talk'));
  $('tabToday').setAttribute('aria-selected', String(tab === 'today'));
}

function bindControls() {
  $('tabTalk').onclick = () => setTab('talk');
  $('tabToday').onclick = () => setTab('today');
  $('micBtn').onclick = (event) => {
    enableMic(!mic.active);
    if (event.detail > 0) event.currentTarget.blur();
  };
  $('coreBtn').onclick = (event) => {
    player.resume();
    send({ type: 'activate' });
    if (event.detail > 0) event.currentTarget.blur();
  };
  $('composer').onsubmit = (event) => {
    event.preventDefault();
    const input = $('textInput');
    const text = input.value.trim();
    if (!text) return;
    player.resume();
    send({ type: 'text', text });
    input.value = '';
  };
  $('noteForm').onsubmit = async (event) => {
    event.preventDefault();
    const input = $('noteInput');
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    try {
      const { items } = await api('/api/notes', { method: 'POST', body: JSON.stringify({ text }) });
      renderNotes(items);
    } catch (error) {
      showNotice(error.message, true);
    }
  };
  $('focusBtn').onclick = async (event) => {
    const minutes = ui.focusLeft > 0 && event.currentTarget.parentElement.classList.contains('is-on') ? 0 : 25;
    try {
      const { activity } = await api('/api/focus', { method: 'POST', body: JSON.stringify({ minutes }) });
      renderActivity(activity);
    } catch (error) {
      showNotice(error.message, true);
    }
    if (event.detail > 0) $('focusBtn').blur();
  };

  document.addEventListener('keydown', (event) => {
    if (event.key === ',' && event.ctrlKey) {
      event.preventDefault();
      openSettings();
      return;
    }
    if (document.querySelector('dialog[open]')) return;
    if (event.key === 'Escape') {
      send({ type: 'stop' });
      return;
    }
    if (isTyping(event.target)) return;
    if (event.code === 'Space') {
      event.preventDefault();
      if (!event.repeat && !ui.spaceDown && mic.active) {
        ui.spaceDown = true;
        player.resume();
        send({ type: 'ptt', down: true });
      }
    } else if (event.key === '/') {
      event.preventDefault();
      $('textInput').focus();
    } else if (event.code === 'KeyM') {
      enableMic(!mic.active);
    }
  });
  document.addEventListener('keyup', (event) => {
    if (event.code === 'Space' && ui.spaceDown) {
      ui.spaceDown = false;
      send({ type: 'ptt', down: false });
    }
  });
  window.addEventListener('blur', () => {
    if (ui.spaceDown) {
      ui.spaceDown = false;
      send({ type: 'ptt', down: false });
    }
  });
  document.addEventListener('visibilitychange', () => {
    send({ type: 'visibility', visible: !document.hidden });
    if (document.hidden) flushTyping();
  });
  // Браузер мог не разрешить звук до первого действия: возобновляем при любом клике.
  document.addEventListener('pointerdown', () => player.resume());
}

bindSettings();
bindPhone();
startClock();
startLevelReports();
bindOnboarding();
bindControls();
renderHint();
connect();
setTimeout(finishBoot, 2500);

// Отладочный доступ для проверок из инструментов разработчика.
window.__jarvis = { core, player, mic, ui };
