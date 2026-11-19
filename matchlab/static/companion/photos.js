'use strict';
const sceneLabels = {
  portrait: 'Портрет',
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
      title.textContent = 'Фото Миры · ' + (photo.caption || sceneLabels[photo.scene]);
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
        const requestId = crypto.randomUUID();
        again.onclick = guard(async () => {
          again.disabled = true;
          try {
            await api('chat/photos', 'POST', { id: requestId, retry_of: photo.id });
            await this.load();
          } finally {
            again.disabled = false;
          }
        });
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
      el('preview').textContent = 'Фото Миры · ' + (lastPhoto.caption || sceneLabels[lastPhoto.scene]);
    el('welcome').hidden = list.childElementCount > 0;
    if (!el('finder').hidden) searchMessages();
    else if (atEnd) scrollEnd();
  },
};
el('photoTool').onclick = () => {
  if (!canChat() || pending || voice.busy) return;
  const field = el('message');
  const description = field.value.trim();
  field.value = description ? 'Пришли фото: ' + description : 'Пришли своё фото';
  voice.draft = false;
  el('compose').requestSubmit();
};
