'use strict';
const sceneLabels = {
  selfie: 'Селфи у окна',
  cafe: 'В кафе',
  boudoir: 'В белье · 18+',
  nude: 'Ню · 18+',
};
const photos = {
  items: [],
  enabled: false,
  version: 0,
  revealed: new Set(),
  requestedScene(text) {
    const phrase = text
      .toLocaleLowerCase('ru')
      .replace(/ё/g, 'е')
      .trim()
      .replace(/^мира[,!]?\s+/, '')
      .replace(
        /^(?:пришли|покажи|скинь|отправь|сделай|можно)\s+(?:(?:мне|свое|свою|твое|твою|пожалуйста)\s+)*/,
        '',
      )
      .replace(/[?!., )]+$/, '');
    if (/^(?:интимное фото|фото ню|ню)$/.test(phrase)) return 'nude';
    if (/^(?:фото|фотку|селфи) в белье$/.test(phrase)) return 'boudoir';
    if (/^(?:фото|фотку|селфи) (?:в|из) кафе$/.test(phrase)) return 'cafe';
    return /^(?:\/?фото|фотку|селфи|фотографию)(?: миры| у окна)?$/.test(phrase) ? 'selfie' : null;
  },
  reset() {
    clearTimeout(this.timer);
    this.version++;
    this.items = [];
    this.revealed.clear();
    this.enabled = false;
    el('photoTool').hidden = true;
  },
  async load() {
    const version = ++this.version;
    clearTimeout(this.timer);
    try {
      const data = await api('chat/photos');
      if (version !== this.version) return;
      this.items = data.photos;
      this.enabled = data.enabled;
      el('photoTool').hidden = !this.enabled;
      this.render();
    } finally {
      if (version === this.version && this.items.some((p) => ['queued', 'running'].includes(p.state))) {
        this.timer = setTimeout(() => this.load().catch(() => {}), 2500);
      }
    }
  },
  render(saved = []) {
    const list = el('messages');
    const atEnd = el('thread').scrollHeight - el('thread').scrollTop - el('thread').clientHeight < 55;
    const existing = new Map(
      [...saved, ...list.querySelectorAll('[data-photo]')].map((n) => [n.dataset.photo, n]),
    );
    const insert = (card, stamp) => {
      const next = [...list.children].find(
        (n) => n.dataset.at > stamp || n.classList.contains('queued'),
      );
      list.insertBefore(card, next || null);
    };
    for (const photo of this.items) {
      const previous = existing.get(photo.id);
      existing.delete(photo.id);
      if (previous?.dataset.state === photo.state) {
        if (!previous.isConnected) insert(previous, photo.created_at);
        continue;
      }
      const card = document.createElement('div');
      card.className = 'bubble photo-card';
      card.dataset.photo = photo.id;
      card.dataset.at = photo.created_at;
      card.dataset.state = photo.state;
      const title = document.createElement('p');
      title.textContent = 'Фото Миры · ' + sceneLabels[photo.scene];
      card.append(title);
      if (photo.state === 'ready') {
        const show = () => {
          const img = new Image();
          img.width = 640;
          img.height = 800;
          img.loading = 'lazy';
          img.alt = title.textContent;
          img.src = '/api/chat/photos/' + photo.id + '/image';
          card.append(img);
        };
        if (['nude', 'boudoir'].includes(photo.scene) && !this.revealed.has(photo.id)) {
          const reveal = document.createElement('button');
          reveal.textContent = 'Открыть фото 18+';
          reveal.onclick = () => {
            this.revealed.add(photo.id);
            reveal.remove();
            show();
          };
          card.append(reveal);
        } else show();
      } else if (photo.state === 'failed') {
        const again = document.createElement('button');
        again.textContent = 'Фото не получилось · Повторить';
        again.onclick = () => this.open('', photo.scene);
        card.append(again);
      } else {
        const status = document.createElement('small');
        status.textContent = photo.state === 'running' ? 'Мира готовит снимок…' : 'Снимок в очереди…';
        card.append(status);
      }
      const stamp = document.createElement('time');
      stamp.dateTime = photo.created_at;
      stamp.textContent = new Date(photo.created_at).toLocaleTimeString('ru-RU', {
        hour: '2-digit',
        minute: '2-digit',
      });
      card.append(stamp);
      if (previous?.isConnected) previous.replaceWith(card);
      else insert(card, photo.created_at);
    }
    for (const n of existing.values()) n.remove();
    const lastPhoto = this.items.at(-1);
    const lastReplyAt = latestTurn?.parts?.at(-1)?.at || latestTurn?.answered_at || '';
    if (lastPhoto && lastPhoto.created_at >= lastReplyAt)
      el('preview').textContent = 'Фото Миры · ' + sceneLabels[lastPhoto.scene];
    el('welcome').hidden = list.childElementCount > 0;
    if (!el('finder').hidden) searchMessages();
    else if (atEnd) scrollEnd();
  },
  open(draft = '', scene = 'selfie') {
    if (!canChat()) return;
    this.draft = draft;
    this.key = crypto.randomUUID();
    const form = el('photoForm');
    form.reset();
    form.elements.scene.value = scene;
    el('photoError').textContent = '';
    syncConsent();
    el('photoDialog').showModal();
  },
};
function syncConsent() {
  const adult = ['boudoir', 'nude'].includes(el('photoForm').elements.scene.value);
  el('photoConsent').hidden = !adult;
  el('wantAdult').required = adult;
  el('wantAdult').checked = false;
}
el('photoTool').onclick = () => photos.open();
el('photoCancel').onclick = () => el('photoDialog').close();
el('photoScenes').onchange = () => {
  photos.key = crypto.randomUUID();
  syncConsent();
};
el('photoForm').onsubmit = async (event) => {
  event.preventDefault();
  el('photoSubmit').disabled = true;
  try {
    const p = await api('chat/photos', 'POST', {
      id: photos.key,
      scene: el('photoForm').elements.scene.value,
      adult: el('wantAdult').checked,
    });
    if (!photos.items.some((item) => item.id === p.id)) photos.items.push(p);
    if (photos.draft && el('message').value.trim() === photos.draft) {
      el('message').value = '';
      el('message').rows = 1;
      voice.draft = false;
    }
    el('photoDialog').close();
    photos.render();
    scrollEnd();
    await photos.load();
  } catch (err) {
    el(el('photoDialog').open ? 'photoError' : 'status').textContent = err.message;
  } finally {
    el('photoSubmit').disabled = false;
  }
};
