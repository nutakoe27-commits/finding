'use strict';
let prefs = { theme: 'system', enter_sends: true, autoplay: true };
let authMode = 'login';

function applyPreferences(value) {
  prefs = value;
  document.documentElement.dataset.theme = value.theme;
}
function confirmDocuments() {
  if (!el('confirmDocs').open) {
    el('renew').reset();
    el('docsError').textContent = '';
    el('confirmDocs').showModal();
  }
}
function showAuth(next = 'login', callback = null, native = false) {
  authMode = next;
  const registering = next === 'register';
  el('auth-title').textContent = registering ? 'Вход и регистрация' : 'С возвращением';
  el('auth-description').textContent = registering
    ? 'Создайте аккаунт, чтобы общаться с Мирой.'
    : 'Войдите, чтобы продолжить переписку с Мирой.';
  el('auth-submit').textContent = registering ? 'Создать аккаунт' : 'Войти';
  el('auth-switch').textContent = registering
    ? 'Уже есть аккаунт? Войти'
    : 'Нет аккаунта? Зарегистрироваться';
  el('auth-legal').hidden = !registering;
  el('auth-legal').disabled = !registering;
  el('auth-form').reset();
  el('auth-form').hidden = el('auth-switch').hidden = !native;
  el('auth-native-toggle').textContent = native
    ? 'Скрыть форму'
    : registering
      ? 'Зарегистрироваться по логину'
      : 'Войти по логину и паролю';
  el('auth-native-toggle').setAttribute('aria-expanded', String(native));
  el('auth-password').autocomplete = registering ? 'new-password' : 'current-password';
  el('auth-password').type = 'password';
  el('auth-password-toggle').textContent = 'Показать';
  el('auth-password-toggle').setAttribute('aria-pressed', 'false');
  el('auth-error').textContent = '';
  window.SocialLogin?.show();
  if (!el('auth-dialog').open) el('auth-dialog').showModal();
  (native
    ? el('auth-username')
    : el('auth-dialog').querySelector('[data-social]:not([hidden])') || el('auth-native-toggle')
  ).focus();
}
el('auth-close').onclick = () => el('auth-dialog').close();
el('auth-dialog').addEventListener('close', () => el('auth-form').reset());
el('auth-native-toggle').onclick = () => showAuth(authMode, null, el('auth-form').hidden);
el('auth-switch').onclick = () => showAuth(authMode === 'login' ? 'register' : 'login', null, true);
el('auth-password-toggle').onclick = () => {
  const show = el('auth-password').type === 'password';
  el('auth-password').type = show ? 'text' : 'password';
  el('auth-password-toggle').setAttribute('aria-pressed', String(show));
  el('auth-password-toggle').textContent = show ? 'Скрыть' : 'Показать';
};
el('auth-form').onsubmit = guard(async () => {
  const data = Object.fromEntries(new FormData(el('auth-form')));
  if (authMode === 'register')
    Object.assign(data, {
      legal_version: session.legal_version,
      terms_accepted: el('auth-terms').checked,
      personal_data_consent: el('auth-personal-data').checked,
      age_confirmed: el('auth-age').checked,
    });
  el('auth-submit').disabled = true;
  try {
    await api('auth/' + authMode, 'POST', data);
    el('auth-dialog').close();
    await refresh();
  } finally {
    el('auth-submit').disabled = false;
  }
});
el('renew').onsubmit = guard(async () => {
  const fields = el('renew').elements;
  await api('auth/confirm-documents', 'POST', {
    legal_version: session.legal_version,
    terms_accepted: fields.terms.checked,
    personal_data_consent: fields.data.checked,
    age_confirmed: fields.age.checked,
  });
  el('confirmDocs').close();
  await refresh();
});

async function showPayments() {
  const box = el('payments');
  const wallet = await api('billing/wallet');
  box.replaceChildren();
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
    const row = document.createElement('p');
    const plan = sub.unlimited ? 'Безлимит' : 'Разборы · ' + sub.credits;
    row.textContent =
      plan +
      ' · ' +
      (sub.amount_kopecks / 100).toLocaleString('ru-RU') +
      ' ₽ · ' +
      (sub.auto_renew ? 'Автопродление включено' : 'Автопродление отключено');
    box.append(row);
    if (!sub.auto_renew) continue;
    const button = document.createElement('button');
    button.textContent = 'Отменить продление и отвязать карту';
    button.onclick = guard(async () => {
      if (!confirm('Отменить продление и отвязать способ оплаты?')) return;
      button.disabled = true;
      try {
        await api('billing/subscriptions/' + sub.id + '/cancel', 'POST', {});
        await showPayments();
        el('account-message').textContent = 'Продление отменено.';
      } finally {
        button.disabled = false;
      }
    });
    box.append(button);
  }
  if (!box.childElementCount) box.textContent = 'Платежей и действующих подписок пока нет.';
}

function updateIdentity() {
  const user = session.user;
  const name = user.display_name || user.username;
  el('identity').textContent = el('profileLabel').textContent = name;
  el('initials').textContent =
    name
      .split(/[\s_-]+/u)
      .filter(Boolean)
      .slice(0, 2)
      .map((part) => Array.from(part)[0])
      .join('')
      .toLocaleUpperCase('ru') || Array.from(name)[0];
  const methods = {
    local: 'Логин и пароль',
    yandex: 'Яндекс',
    vk: 'ВКонтакте',
    ok: 'Одноклассники',
    mail: 'Mail.ru',
  };
  el('loginMethod').textContent =
    '@' + user.username + ' · ' + (methods[user.login_provider] || 'Личный аккаунт');
}
function selectPanel(id) {
  for (const button of document.querySelectorAll('[data-panel]')) {
    const current = button.dataset.panel === id;
    button.setAttribute('aria-pressed', String(current));
    el(button.dataset.panel).hidden = !current;
  }
}
async function showProfile() {
  if (voice.busy || pending) return;
  if (!session?.user) {
    showAuth('login');
    return;
  }
  updateIdentity();
  el('displayName').value = session.user.display_name || session.user.username;
  el('theme').value = prefs.theme;
  el('enterSends').checked = prefs.enter_sends;
  el('autoplay').checked = prefs.autoplay;
  el('security').hidden = !session.user.local_login_available;
  el('account-message').textContent = '';
  selectPanel('settingsPane');
  if (!el('profileDialog').open) el('profileDialog').showModal();
  history.replaceState(null, '', '#account');
  await window.SocialLogin?.connections();
}
el('profileDialog').addEventListener('close', () => {
  el('passwordForm').reset();
  el('social-link-password').value = '';
  for (const details of el('profileDialog').querySelectorAll('details')) details.open = false;
  if (location.hash === '#account') history.replaceState(null, '', location.pathname + location.search);
});
for (const button of document.querySelectorAll('[data-panel]'))
  button.onclick = guard(async () => {
    selectPanel(button.dataset.panel);
    if (button.dataset.panel === 'billingPane') {
      el('payments').textContent = 'Загружаем…';
      try {
        await showPayments();
      } catch (error) {
        el('payments').textContent = error.message;
      }
    }
  });
el('profile').onclick = guard(showProfile);
el('profileSide').onclick = guard(showProfile);
el('preferences').onsubmit = guard(async () => {
  const button = el('preferences').querySelector('button');
  button.disabled = true;
  try {
    const { display_name, ...value } = await api('chat/preferences', 'PUT', {
      display_name: el('displayName').value,
      theme: el('theme').value,
      enter_sends: el('enterSends').checked,
      autoplay: el('autoplay').checked,
    });
    session.user.display_name = el('displayName').value = display_name;
    applyPreferences(value);
    updateIdentity();
    el('account-message').textContent = 'Настройки сохранены.';
  } finally {
    button.disabled = false;
  }
});
el('endSessions').onclick = guard(async () => {
  if (!confirm('Завершить все остальные сеансы этого аккаунта?')) return;
  await api('chat/sessions/revoke-others', 'POST', {});
  el('account-message').textContent = 'На других устройствах потребуется войти заново.';
});
el('passwordForm').onsubmit = guard(async () => {
  const form = el('passwordForm');
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const renewed = await api('auth/password', 'POST', Object.fromEntries(new FormData(form)));
    session.csrf = renewed.csrf;
    session.user = renewed.user;
    form.reset();
    el('security').open = false;
    el('account-message').textContent = 'Пароль изменён. Другие сеансы завершены.';
  } finally {
    button.disabled = false;
  }
});

// The original social-login component handles provider redirects, linking and registration consent.
window.Account = {
  api: (path, options = {}) =>
    api(path.slice('/api/'.length), options.method || 'GET', options.body && JSON.parse(options.body)),
  show: showAuth,
  refresh,
  renderPage: showProfile,
  get user() {
    return session?.user;
  },
  ready: new Promise((resolve) => document.addEventListener('DOMContentLoaded', resolve, { once: true }))
    .then(refresh)
    .catch((error) => {
      el('status').textContent = error.message;
    }),
};
window.Account.ready.then(() => {
  if (
    session?.accepted &&
    location.hash === '#account' &&
    !['success', 'complete'].includes(new URLSearchParams(location.search).get('social'))
  )
    guard(showProfile)();
});
