'use strict';
const el = (id) => document.getElementById(id);
let session,
  revision = 0,
  mode = '',
  pending = false,
  retry = null;
async function api(path, method = 'GET', body) {
  const options = {
    method,
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session?.csrf || '' },
  };
  if (body !== undefined) options.body = JSON.stringify(body);
  const response = await fetch('/api/' + path, options);
  const data = await response.json();
  if (!response.ok)
    throw Object.assign(new Error(data.detail || 'Запрос не выполнен.'), { status: response.status });
  return data;
}
function openSheet(title, box) {
  el('sheetTitle').textContent = title;
  for (const id of ['account', 'loginBox', 'register']) el(id).hidden = id !== box;
  el('notice').textContent = '';
  if (!el('sheet').open) el('sheet').showModal();
}
function agreements(next) {
  mode = next;
  openSheet(next === 'register' ? 'Создать аккаунт' : 'Условия общения', 'register');
  el('register').reset();
  el('credentials').hidden = next !== 'register';
  for (const input of el('credentials').querySelectorAll('input')) input.disabled = next !== 'register';
  el('agree').textContent = next === 'register' ? 'Создать аккаунт' : 'Продолжить';
}
function bubble(content, mine, stamp, turn) {
  const item = document.createElement('div');
  item.className = mine ? 'bubble mine' : 'bubble';
  const text = document.createElement('p');
  text.textContent = content;
  item.append(text);
  const meta = document.createElement('div');
  meta.className = 'meta';
  if (mine && turn.channel === 'voice') meta.append('Голосом');
  if (!mine) {
    const listen = document.createElement('button');
    listen.className = 'listen';
    listen.textContent = 'Озвучить';
    listen.onclick = guard(() => voice.play(turn, listen));
    meta.append(listen);
  }
  if (stamp) {
    const time = document.createElement('time');
    time.dateTime = stamp;
    time.textContent = new Date(stamp).toLocaleTimeString('ru-RU', {
      hour: '2-digit',
      minute: '2-digit',
    });
    meta.append(time);
  }
  item.append(meta);
  return item;
}
function scrollEnd() {
  el('thread').scrollTop = el('thread').scrollHeight;
}
const messenger = document.querySelector('.messenger');
el('backChats').onclick = () => {
  messenger.dataset.pane = 'list';
  el('openChat').focus();
};
el('openChat').onclick = () => {
  messenger.dataset.pane = 'chat';
  el(matchMedia('(max-width: 700px)').matches ? 'backChats' : 'message').focus();
};

let matches = [];
let matchIndex = -1;
function showMatch(index) {
  matches[matchIndex]?.classList.remove('match-current');
  matchIndex = matches.length ? (index + matches.length) % matches.length : -1;
  const hit = matches[matchIndex];
  if (hit) {
    hit.classList.add('match-current');
    hit.scrollIntoView({ block: 'nearest' });
  }
  el('found').textContent = !el('query').value.trim()
    ? ''
    : hit
      ? matchIndex + 1 + ' из ' + matches.length
      : 'Не найдено';
  el('findNext').disabled = el('findPrev').disabled = matches.length < 2;
}
function searchMessages() {
  matches[matchIndex]?.classList.remove('match-current');
  const query = el('query').value.trim().toLocaleLowerCase('ru');
  matches = query
    ? Array.from(el('messages').querySelectorAll('.bubble')).filter((node) =>
        node.querySelector('p').textContent.toLocaleLowerCase('ru').includes(query),
      )
    : [];
  matchIndex = -1;
  showMatch(matches.length - 1);
}
function toggleFinder(open) {
  el('finder').hidden = !open;
  el('find').setAttribute('aria-expanded', String(open));
  el('query').value = '';
  searchMessages();
  if (open) el('query').focus();
  else {
    el('find').focus();
    scrollEnd();
  }
}
el('find').onclick = () => toggleFinder(el('finder').hidden);
el('endFind').onclick = () => toggleFinder(false);
el('query').oninput = searchMessages;
el('findPrev').onclick = () => showMatch(matchIndex - 1);
el('finder').onsubmit = (event) => {
  event.preventDefault();
  showMatch(matchIndex + 1);
};
el('finder').onkeydown = (event) => {
  if (event.key === 'Escape') {
    event.preventDefault();
    toggleFinder(false);
  }
};
function draw(data) {
  revision = data.revision;
  const list = el('messages');
  list.replaceChildren();
  let previousDay = '';
  for (const turn of data.turns) {
    const day =
      turn.sent_at &&
      new Date(turn.sent_at).toLocaleDateString('ru-RU', {
        day: 'numeric',
        month: 'long',
        year: 'numeric',
      });
    if (day && day !== previousDay) {
      const heading = document.createElement('p');
      heading.className = 'day';
      heading.textContent = day;
      list.append(heading);
      previousDay = day;
    }
    list.append(
      bubble(turn.user, true, turn.sent_at, turn),
      bubble(turn.assistant, false, turn.answered_at, turn),
    );
  }
  el('preview').textContent = data.turns.at(-1)?.assistant || 'Начать разговор';
  el('welcome').hidden = data.turns.length > 0;
  scrollEnd();
  if (!el('finder').hidden) searchMessages();
}
function canChat() {
  if (!session?.user) openSheet('Войти в чат', 'loginBox');
  else if (!session.accepted) agreements('confirm');
  else return true;
  return false;
}
async function refresh() {
  session = await api('auth/me');
  el('profile').textContent = session.user ? 'Профиль' : 'Войти';
  el('profileLabel').textContent =
    session.user?.display_name || session.user?.username || 'Войти в аккаунт';
  for (const id of ['adminLink', 'adminMobile']) el(id).hidden = session.user?.role !== 'admin';
  if (session.user) {
    const data = await api('chat');
    draw(data);
    session.accepted = data.accepted;
    if (!data.accepted) agreements('confirm');
  } else {
    draw({ revision: 0, turns: [] });
    const providers = el('providers');
    providers.replaceChildren();
    for (const p of session.providers.filter((x) => x !== 'local')) {
      const button = document.createElement('button');
      button.textContent = { yandex: 'Яндекс', vk: 'ВКонтакте', ok: 'Одноклассники', mail: 'Mail.ru' }[
        p
      ];
      button.onclick = guard(async () => {
        const result = await api('auth/social/' + p + '/start', 'POST', { action: 'login' });
        location.assign(result.authorization_url);
      });
      providers.append(button);
    }
  }
}
function guard(fn) {
  return async (e) => {
    e?.preventDefault();
    try {
      await fn(e);
    } catch (err) {
      el(el('sheet').open ? 'notice' : 'status').textContent = err.message;
    }
  };
}
el('profile').onclick = guard(async () => {
  if (voice.busy || pending) return;
  if (!session?.user) {
    openSheet('Войти в чат', 'loginBox');
    return;
  }
  openSheet('Профиль', 'account');
  el('identity').textContent = session.user.display_name || session.user.username;
  const box = el('payments');
  box.replaceChildren();
  const wallet = await api('billing/wallet');
  if (wallet.balance) {
    const p = document.createElement('p');
    p.textContent = 'Баланс прежнего сервиса: ' + wallet.balance;
    box.append(p);
  }
  if (wallet.orders.length) {
    const details = document.createElement('details'),
      title = document.createElement('summary');
    title.textContent = 'История платежей';
    details.append(title);
    const states = {
      succeeded: 'Оплачен',
      pending: 'Ожидает оплаты',
      refunded: 'Возврат',
      canceled: 'Отменён',
    };
    for (const order of wallet.orders) {
      const p = document.createElement('p');
      p.textContent =
        new Date(order.created_at).toLocaleDateString('ru-RU') +
        ' · ' +
        (order.amount_kopecks / 100).toLocaleString('ru-RU') +
        ' ₽ · ' +
        (states[order.state] || 'В обработке');
      details.append(p);
    }
    box.append(details);
  }
  for (const sub of wallet.subscription?.history || []) {
    if (!sub.auto_renew) continue;
    const button = document.createElement('button');
    button.textContent = 'Отвязать карту подписки ' + sub.plan_id;
    button.onclick = guard(async () => {
      if (confirm('Отменить продление и отвязать способ оплаты?')) {
        await api('billing/subscriptions/' + sub.id + '/cancel', 'POST', {});
        button.remove();
        el('notice').textContent = 'Продление отменено.';
      }
    });
    box.append(button);
  }
});
el('profileSide').onclick = () => el('profile').click();
el('registerToggle').onclick = () => agreements('register');
el('login').onsubmit = guard(async () => {
  await api('auth/login', 'POST', Object.fromEntries(new FormData(el('login'))));
  el('login').reset();
  el('sheet').close();
  await refresh();
});
el('register').onsubmit = guard(async () => {
  const path = {
    confirm: 'auth/confirm-documents',
    social: 'auth/social/complete',
    register: 'auth/register',
  }[mode];
  await api(path, 'POST', {
    ...Object.fromEntries(new FormData(el('register'))),
    age_confirmed: true,
    terms_accepted: true,
    personal_data_consent: true,
    legal_version: session.legal_version,
  });
  el('register').reset();
  el('sheet').close();
  await refresh();
});
el('logout').onclick = guard(async () => {
  await api('auth/logout', 'POST', {});
  location.replace('/');
});
el('clear').onclick = guard(async () => {
  if (confirm('Удалить всю историю общения с Мирой?')) {
    await api('chat', 'DELETE');
    voice.stopPlayback();
    voice.draft = false;
    el('message').value = '';
    el('sheet').close();
    retry = null;
    await refresh();
  }
});
el('compose').onsubmit = guard(async () => {
  if (!canChat()) return;
  if (pending || voice.busy) return;
  const message = el('message').value.trim();
  if (!message) return;
  const channel = voice.draft ? 'voice' : 'text';
  if (!retry || retry.message !== message || retry.revision !== revision)
    retry = { message, revision, id: crypto.randomUUID(), channel };
  pending = true;
  el('send').disabled = true;
  el('message').readOnly = true;
  el('profile').disabled = true;
  el('mic').disabled = true;
  el('status').textContent = 'Мира печатает…';
  el('welcome').hidden = true;
  const preview = bubble(message, true, null, retry);
  preview.classList.add('queued');
  el('messages').append(preview);
  scrollEnd();
  let reply;
  try {
    const data = await api('chat', 'POST', retry);
    reply = data.turns.find((t) => t.id === retry.id);
    draw(data);
    retry = null;
    el('message').value = '';
    el('message').rows = 1;
    el('status').textContent = '';
    voice.draft = false;
  } catch (err) {
    preview.remove();
    el('welcome').hidden = el('messages').childElementCount > 0;
    if ([401, 403, 409].includes(err.status)) await refresh();
    throw err;
  } finally {
    pending = false;
    el('send').disabled = false;
    el('message').readOnly = false;
    el('profile').disabled = false;
    el('mic').disabled = false;
  }
  if (channel === 'voice' && reply)
    await voice.play(reply, el('messages').lastElementChild?.querySelector('.listen'));
});
el('message').oninput = () => {
  el('message').rows = Math.min(4, el('message').value.split('\n').length);
  if (!el('message').value.trim()) voice.draft = false;
};
el('message').onkeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    el('compose').requestSubmit();
  }
};
for (const button of el('starters').children)
  button.onclick = () => {
    if (pending || voice.busy) return;
    voice.draft = false;
    el('message').value = button.textContent.trim();
    el('message').focus();
  };
guard(async () => {
  await refresh();
  const social = new URLSearchParams(location.search).get('social');
  if (social === 'complete') {
    await api('auth/social/pending');
    agreements('social');
  } else if (social && social !== 'success')
    el('status').textContent = 'Вход не завершён. Попробуйте снова.';
  if (social) history.replaceState(null, '', '/');
})();
