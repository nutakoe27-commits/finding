'use strict';
async function encodeMicrophone(chunks, size, rate) {
  const mixer = new OfflineAudioContext(1, Math.min(320000, Math.floor((size * 16000) / rate)), 16000);
  const buf = mixer.createBuffer(1, size, rate);
  let off = 0;
  for (const chunk of chunks) {
    buf.copyToChannel(chunk, 0, off);
    off += chunk.length;
  }
  const src = mixer.createBufferSource();
  src.buffer = buf;
  src.connect(mixer.destination);
  src.start();
  const samples = (await mixer.startRendering()).getChannelData(0);
  const bytes = new Uint8Array(samples.length * 2),
    view = new DataView(bytes.buffer);
  for (let i = 0; i < samples.length; i++)
    view.setInt16(i * 2, Math.round(Math.max(-1, Math.min(1, samples[i])) * 32767), true);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 8192)
    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}

const voice = {
  busy: false,
  draft: false,
  capture: null,
  playId: null,
  generation: 0,
  stopPlayback() {
    this.generation++;
    this.audio?.pause();
    this.abort?.abort();
    if (this.url) URL.revokeObjectURL(this.url);
    if (this.button) this.button.textContent = 'Озвучить';
    this.audio = null;
    this.url = null;
    this.playId = null;
    this.button = null;
    this.abort = null;
  },
  async play(turn, button) {
    if (!canChat() || this.busy || pending) return;
    const key = turn.id + ':' + (turn.part ?? 'all');
    const same = this.playId === key;
    this.stopPlayback();
    if (same) return;
    this.playId = key;
    this.button = button;
    const generation = this.generation;
    if (button) button.textContent = 'Остановить';
    const done = () => {
      if (generation === this.generation) this.stopPlayback();
    };
    await this.serverPlay(turn, generation).catch(fail);
    function fail(error) {
      if (generation !== voice.generation) return;
      done();
      el('status').textContent =
        error.name === 'NotAllowedError' ? 'Нажмите «Озвучить», чтобы включить звук.' : error.message;
    }
  },
  async serverPlay(turn, generation) {
    this.abort = new AbortController();
    const r = await fetch('/api/chat/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf },
      body: JSON.stringify({ id: turn.id, part: turn.part }),
      signal: this.abort.signal,
    });
    if (!r.ok) throw new Error('Озвучка недоступна. Ответ сохранён в переписке.');
    const blob = await r.blob();
    if (generation !== this.generation) return;
    this.url = URL.createObjectURL(blob);
    this.audio = new Audio(this.url);
    this.audio.onended = () => {
      if (generation === this.generation) this.stopPlayback();
    };
    this.audio.onerror = () => {
      if (generation === this.generation) {
        this.stopPlayback();
        el('status').textContent = 'Не удалось воспроизвести звук.';
      }
    };
    await this.audio.play();
  },
  async start() {
    if (!canChat()) return;
    if (this.busy || pending) return;
    if (el('message').value.trim()) {
      el('status').textContent = 'Сначала отправьте или очистите черновик.';
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode)
      throw new Error('В этом браузере запись недоступна. Можно написать сообщение.');
    this.stopPlayback();
    this.busy = true;
    this.draft = false;
    const cap = { chunks: [], size: 0 };
    this.capture = cap;
    el('mic').disabled = true;
    el('send').disabled = true;
    el('message').readOnly = true;
    el('status').textContent = 'Подключаем микрофон…';
    try {
      cap.ctx = new AudioContext();
      await cap.ctx.resume();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true },
        video: false,
      });
      if (this.capture !== cap) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      cap.stream = stream;
      await cap.ctx.audioWorklet.addModule('/chat-assets/capture.js');
      if (this.capture !== cap) return;
      cap.node = new AudioWorkletNode(cap.ctx, 'mira-capture');
      const source = cap.ctx.createMediaStreamSource(stream),
        silent = cap.ctx.createGain();
      silent.gain.value = 0;
      source.connect(cap.node).connect(silent).connect(cap.ctx.destination);
      cap.node.port.onmessage = (event) => {
        if (this.capture !== cap || cap.size >= cap.ctx.sampleRate * 20) return;
        const chunk = event.data.subarray(0, cap.ctx.sampleRate * 20 - cap.size);
        cap.chunks.push(chunk);
        cap.size += chunk.length;
      };
      el('recording').hidden = false;
      el('compose').hidden = true;
      el('recTime').textContent = 'Запись · 0:00';
      el('status').textContent = 'До 20 секунд. Затем можно проверить текст.';
      const started = performance.now();
      cap.timer = setInterval(() => {
        const secs = Math.floor((performance.now() - started) / 1000);
        el('recTime').textContent = 'Запись · 0:' + String(Math.min(20, secs)).padStart(2, '0');
        if (secs >= 20) guard(() => this.finish(true))();
      }, 200);
    } catch (error) {
      if (this.capture !== cap) return;
      await this.finish(false);
      throw new Error(
        error.name === 'NotAllowedError'
          ? 'Разрешите доступ к микрофону в настройках браузера.'
          : 'Не удалось включить микрофон. Проверьте устройство.',
      );
    }
  },
  async finish(keep) {
    const cap = this.capture;
    if (!cap) return;
    this.capture = null;
    clearInterval(cap.timer);
    cap.stream?.getTracks().forEach((t) => t.stop());
    cap.node?.disconnect();
    const rate = cap.ctx?.sampleRate;
    if (cap.ctx && cap.ctx.state !== 'closed') await cap.ctx.close();
    el('recording').hidden = true;
    el('compose').hidden = false;
    el('status').textContent = '';
    try {
      if (!keep) return;
      if (cap.size < (rate || 16000) / 5)
        throw new Error('Запись слишком короткая. Попробуйте ещё раз.');
      el('status').textContent = 'Распознаём речь…';
      const audio = await encodeMicrophone(cap.chunks, cap.size, rate);
      const data = await api('chat/transcribe', 'POST', { audio });
      el('message').value = data.text;
      this.draft = true;
      el('status').textContent = prefs.autoplay
        ? 'Проверьте текст и нажмите «Отправить». Мира озвучит ответ.'
        : 'Проверьте текст и нажмите «Отправить».';
    } finally {
      this.busy = false;
      el('mic').disabled = false;
      el('send').disabled = false;
      el('message').readOnly = false;
    }
  },
};
el('mic').onclick = guard(() => voice.start());
el('cancelRec').onclick = guard(() => voice.finish(false));
el('finishRec').onclick = guard(() => voice.finish(true));
document.addEventListener('visibilitychange', () => {
  if (document.hidden && voice.capture) voice.finish(false);
});
window.addEventListener('pagehide', () => {
  voice.stopPlayback();
  if (voice.capture) voice.finish(false);
});
