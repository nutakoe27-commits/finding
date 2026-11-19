'use strict';
const get = (id) => document.getElementById(id);
const node = (tag, text, cls) => {
  const e = document.createElement(tag);
  e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
const when = (stamp) =>
  stamp
    ? new Date(stamp).toLocaleString('ru-RU', {
        day: 'numeric',
        month: 'short',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—';
const methods = {
  local: 'Логин и пароль',
  yandex: 'Яндекс',
  vk: 'ВКонтакте',
  ok: 'Одноклассники',
  mail: 'Mail.ru',
};
const actions = {
  call_turn: 'Обменялся голосовой репликой с Мирой',
  call_turn_failed: 'Ошибка голосового звонка',
  opened: 'Открыл чат',
  cleared: 'Удалил переписку',
  text_turn: 'Отправил текст и получил ответ',
  voice_turn: 'Отправил распознанную фразу и получил ответ',
  transcribed: 'Распознал голосовую запись',
  synthesized: 'Запросил озвучку ответа',
  reply_failed: 'Ошибка ответа Миры',
  transcribed_failed: 'Ошибка распознавания',
  synthesized_failed: 'Ошибка озвучки',
  login: 'Вошёл в аккаунт',
  registered: 'Создал аккаунт',
  logout: 'Вышел из аккаунта',
};
let offset = 0,
  term = '',
  selection = null,
  card = null,
  tab = 'chat',
  version = 0;
async function load(path) {
  const r = await fetch('/api/admin/companion/users' + path);
  if (!r.ok)
    throw new Error(
      r.status === 401
        ? 'Сессия закончилась. Войдите через чат.'
        : r.status === 403
          ? 'Нужен аккаунт администратора.'
          : 'Не удалось загрузить данные.',
    );
  return r.json();
}
function report(e) {
  get('feedback').textContent = e.message;
}
function render() {
  get('tabChat').setAttribute('aria-pressed', tab === 'chat');
  get('tabEvents').setAttribute('aria-pressed', tab === 'events');
  const entries = get('entries');
  entries.replaceChildren();
  if (!card) return;
  if (tab === 'events')
    for (const e of card.events) {
      const row = node('div', '', 'event');
      row.append(node('span', actions[e.action] || e.action), node('small', when(e.at)));
      entries.append(row);
    }
  else
    for (const t of card.turns) {
      for (const [name, text, at, mine] of [
        ['Пользователь', t.user, t.sent_at, true],
        ['Мира', t.assistant, t.answered_at, false],
      ]) {
        const row = node('div', '', mine ? 'bubble mine' : 'bubble');
        row.append(
          node('small', name + (mine && t.channel === 'voice' ? ' · голосом' : '')),
          node('p', text),
          node('small', when(at)),
        );
        entries.append(row);
      }
    }
  if (!entries.childElementCount)
    entries.append(
      node('p', tab === 'chat' ? 'Сохранённых реплик для просмотра пока нет.' : 'Действий пока нет.'),
    );
  entries.scrollTop = tab === 'chat' ? entries.scrollHeight : 0;
}
async function select(user) {
  selection = user.id;
  card = null;
  get('card').hidden = true;
  get('name').textContent = user.display_name || user.username;
  get('hint').textContent = 'Загружаем…';
  get('feedback').textContent = '';
  for (const b of get('people').children) b.setAttribute('aria-pressed', b.dataset.id === user.id);
  if (matchMedia('(max-width:720px)').matches) get('detail').scrollIntoView({ block: 'start' });
  const data = await load('/' + encodeURIComponent(user.id));
  if (selection !== user.id) return;
  card = data;
  get('card').hidden = false;
  get('hint').textContent = '@' + user.username;
  const facts = get('facts');
  facts.replaceChildren();
  for (const [k, v] of [
    ['Регистрация', when(user.created_at)],
    ['Последний вход', when(user.last_login_at)],
    ['Способ входа', methods[user.last_login_provider] || user.last_login_provider],
    [
      'Доступ',
      user.enabled ? (user.role === 'admin' ? 'Администратор' : 'Пользователь') : 'Аккаунт отключён',
    ],
    ['Пар реплик', data.turns.length],
  ]) {
    const item = node('div', '');
    item.append(node('dt', k), node('dd', v));
    facts.append(item);
  }
  render();
}
async function directory() {
  const n = ++version;
  selection = null;
  card = null;
  get('card').hidden = true;
  get('name').textContent = 'Карточка пользователя';
  get('hint').textContent = 'Выберите аккаунт.';
  get('feedback').textContent = '';
  get('next').disabled = get('prev').disabled = true;
  get('people').replaceChildren();
  const data = await load('?' + new URLSearchParams({ search: term, offset }));
  if (n !== version) return;
  for (const u of data.users) {
    const b = node('button', '', 'person');
    b.dataset.id = u.id;
    b.setAttribute('aria-pressed', 'false');
    b.append(
      node('strong', u.display_name || u.username),
      node('small', '@' + u.username + ' · ' + when(u.last_activity || u.last_login_at)),
    );
    b.onclick = () => select(u).catch(report);
    get('people').append(b);
  }
  if (!data.users.length) get('people').append(node('p', 'Пользователи не найдены.'));
  get('prev').disabled = !offset;
  get('next').disabled = !data.has_more;
}
get('filter').onsubmit = (e) => {
  e.preventDefault();
  term = get('search').value.trim();
  offset = 0;
  directory().catch(report);
};
get('prev').onclick = () => {
  offset -= 30;
  directory().catch(report);
};
get('next').onclick = () => {
  offset += 30;
  directory().catch(report);
};
get('tabChat').onclick = () => {
  tab = 'chat';
  render();
};
get('tabEvents').onclick = () => {
  tab = 'events';
  render();
};
directory().catch(report);
