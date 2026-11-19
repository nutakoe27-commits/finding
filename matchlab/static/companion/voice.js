'use strict';
const voice = {
  busy: false,
  draft: false,
  callMode: false,
  callSession: 0,
  capture: null,
  playId: null,
  generation: 0,
  stopPlayback() {
    this.generation++;
    this.audio?.pause();
    window.speechSynthesis?.cancel();
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
    const same = this.playId === turn.id;
    this.stopPlayback();
    if (same) return;
    this.playId = turn.id;
    this.button = button;
    const generation = this.generation;
    if (button) button.textContent = 'Остановить';
    const done = () => {
      if (generation === this.generation) this.stopPlayback();
    };
    const deviceVoice = window.speechSynthesis
      ?.getVoices()
      .find((v) => v.localService && /^ru(?:[-_]|$)/i.test(v.lang));
    if (deviceVoice && /[а-яё]/i.test(turn.assistant)) {
      const utterance = new SpeechSynthesisUtterance(turn.assistant);
      utterance.voice = deviceVoice;
      utterance.lang = deviceVoice.lang;
      utterance.onend = done;
      utterance.onerror = () => {
        if (generation === this.generation) this.serverPlay(turn, generation).catch(fail);
      };
      window.speechSynthesis.speak(utterance);
    } else {
      await this.serverPlay(turn, generation).catch(fail);
    }
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
      body: JSON.stringify({ id: turn.id }),
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
    const cap = { chunks: [], size: 0, callSession: this.callMode ? this.callSession : null };
    this.capture = cap;
    el('talk').disabled = true;
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
      el('finishRec').textContent = this.callMode ? 'Отправить' : 'Готово';
      el('status').textContent = this.callMode
        ? 'Слушаю. Через 20 секунд фраза отправится автоматически.'
        : 'До 20 секунд. Затем можно проверить текст.';
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
    el('compose').hidden = this.callMode;
    el('status').textContent = '';
    let sendDirect = false;
    try {
      if (!keep) return;
      if (cap.size < (rate || 16000) / 5) throw new Error('Запись слишком короткая. Попробуйте ещё раз.');
      el('status').textContent = 'Распознаём речь…';
      const mixer = new OfflineAudioContext(
        1,
        Math.min(320000, Math.floor((cap.size * 16000) / rate)),
        16000,
      );
      const buf = mixer.createBuffer(1, cap.size, rate);
      let off = 0;
      for (const chunk of cap.chunks) {
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
      const data = await api('chat/transcribe', 'POST', { audio: btoa(binary) });
      if (cap.callSession !== null && (!this.callMode || this.callSession !== cap.callSession)) return;
      el('message').value = data.text;
      this.draft = true;
      sendDirect = cap.callSession !== null;
      el('status').textContent = 'Проверьте текст и нажмите «Отправить». Мира озвучит ответ.';
    } finally {
      this.busy = false;
      el('talk').disabled = false;
      el('mic').disabled = false;
      el('send').disabled = false;
      el('message').readOnly = false;
    }
    if (sendDirect) el('compose').requestSubmit();
  },
  endCall() {
    this.callMode = false;
    this.callSession++;
    this.stopPlayback();
    if (this.capture) this.finish(false);
    el('call').setAttribute('aria-pressed', 'false');
    el('callbar').hidden = true;
    el('compose').hidden = false;
    el('status').textContent = '';
  },
};
el('call').onclick = guard(async () => {
  if (!canChat()) return;
  if (voice.callMode) {
    voice.endCall();
    return;
  }
  if (voice.busy || pending) return;
  if (el('message').value.trim()) throw new Error('Сначала отправьте или очистите черновик.');
  voice.stopPlayback();
  voice.callMode = true;
  voice.callSession++;
  el('call').setAttribute('aria-pressed', 'true');
  el('callbar').hidden = false;
  el('compose').hidden = true;
  el('status').textContent = '';
  el('talk').focus();
});
el('talk').onclick = guard(() => voice.start());
el('endCall').onclick = () => voice.endCall();
el('mic').onclick = guard(() => voice.start());
el('cancelRec').onclick = guard(() => voice.finish(false));
el('finishRec').onclick = guard(() => voice.finish(true));
window.speechSynthesis?.getVoices();
document.addEventListener('visibilitychange', () => {
  if (document.hidden && voice.capture) voice.finish(false);
});
window.addEventListener('pagehide', () => {
  voice.stopPlayback();
  if (voice.capture) voice.finish(false);
});
