'use strict';

// Each call owns its microphone, audio graph and network requests. A late result
// from a previous call must never start playback in a new one.
let currentCall = null;
const callScreen = el('callScreen');

function phase(call, name, label, action) {
  if (call !== currentCall) return;
  call.phase = name;
  callScreen.dataset.phase = name;
  el('callState').textContent = label;
  el('callAction').hidden = !action;
  if (action) el('actionLabel').textContent = action;
}
function stopPlayer(call) {
  if (call.player) {
    call.player.onended = null;
    call.player.stop();
    call.player.disconnect();
    call.player = null;
  }
}
function hangup() {
  const call = currentCall;
  if (!call) return;
  currentCall = null;
  call.abort.abort();
  clearInterval(call.ringer);
  clearInterval(call.clock);
  clearTimeout(call.permissionHint);
  call.context = null;
  call.parts = [];
  stopPlayer(call);
  call.stream?.getTracks().forEach((track) => track.stop());
  call.node?.disconnect();
  call.ctx?.close().catch(() => {});
  callScreen.close();
  el('call').setAttribute('aria-pressed', 'false');
  el('call').focus();
}
function ring(call) {
  if (currentCall !== call || call.phase !== 'ringing') return;
  const t = call.ctx.currentTime;
  const tone = call.ctx.createOscillator(),
    gain = call.ctx.createGain();
  tone.frequency.value = 425;
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(0.034, t + 0.04);
  gain.gain.setValueAtTime(0.034, t + 0.49);
  gain.gain.linearRampToValueAtTime(0, t + 0.55);
  tone.connect(gain).connect(call.ctx.destination);
  tone.start(t);
  tone.stop(t + 0.56);
  tone.onended = () => {
    tone.disconnect();
    gain.disconnect();
  };
}
function listening(call) {
  if (currentCall !== call) return;
  call.parts = [];
  call.size = 0;
  call.voiced = 0;
  call.silence = 0;
  call.detected = false;
  for (const track of call.stream.getAudioTracks()) track.enabled = !call.muted;
  phase(call, call.muted ? 'muted' : 'listening', call.muted ? 'Микрофон выключен' : 'Вас слышно');
}
async function playback(call, bytes) {
  if (currentCall !== call) return;
  const buffer = await call.ctx.decodeAudioData(bytes);
  if (currentCall !== call) return;
  stopPlayer(call);
  const player = call.ctx.createBufferSource();
  call.player = player;
  player.buffer = buffer;
  player.connect(call.ctx.destination);
  player.onended = () => {
    if (currentCall !== call || call.player !== player) return;
    call.player = null;
    player.disconnect();
    listening(call);
  };
  for (const track of call.stream.getAudioTracks()) track.enabled = false;
  phase(call, 'speaking', 'Мира говорит', 'Перебить');
  player.start();
}
function callError(call, error) {
  if (currentCall !== call) return;
  call.stream?.getAudioTracks().forEach((t) => {
    t.enabled = false;
  });
  phase(
    call,
    'error',
    error.message || 'Не удалось продолжить звонок.',
    call.connected ? 'Повторить' : 'Перезвонить',
  );
}
async function submitAudio(call) {
  if (currentCall !== call || call.phase !== 'listening' || !call.detected) return;
  phase(call, 'thinking', 'Мира отвечает…');
  call.stream.getAudioTracks().forEach((t) => {
    t.enabled = false;
  });
  const chunks = call.parts;
  call.parts = [];
  try {
    const audio = await encodeMicrophone(chunks, call.size, call.ctx.sampleRate);
    if (currentCall !== call) return;
    const r = await fetch('/api/chat/call/turn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf },
      body: JSON.stringify({ audio, context: call.context }),
      signal: call.abort.signal,
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      if (r.status === 409) call.context = null;
      throw new Error(data.detail || 'Соединение прервалось. Повторите фразу.');
    }
    const data = await r.json();
    if (currentCall !== call) return;
    call.context = data.context;
    const raw = atob(data.audio);
    const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0));
    await playback(call, bytes.buffer);
  } catch (error) {
    callError(call, error);
  }
}
function microphoneChunk(call, chunk) {
  if (currentCall !== call || call.phase !== 'listening') return;
  const rate = call.ctx.sampleRate;
  let power = 0;
  for (const sample of chunk) power += sample * sample;
  const loud = Math.sqrt(power / chunk.length) > 0.012;
  call.parts.push(chunk);
  call.size += chunk.length;
  if (loud) {
    call.voiced += chunk.length;
    call.silence = 0;
  } else {
    call.silence += chunk.length;
    if (!call.detected) call.voiced = 0;
  }
  if (!call.detected && call.voiced / rate >= 0.12) {
    call.detected = true;
    phase(call, 'listening', 'Слушаю…', 'Готово');
  }
  if (!call.detected) {
    while (call.size > rate * 0.55 && call.parts.length > 1) call.size -= call.parts.shift().length;
  } else if (call.size / rate >= 19.8 || call.silence / rate >= 1.05) {
    void submitAudio(call);
  }
}
async function startCall() {
  if (!canChat() || pending || voice.busy || currentCall) return;
  voice.stopPlayback();
  const call = { abort: new AbortController(), context: null, muted: false, connected: false, parts: [] };
  currentCall = call;
  callScreen.showModal();
  el('call').setAttribute('aria-pressed', 'true');
  el('callTime').hidden = true;
  el('callMic').disabled = true;
  el('callMic').setAttribute('aria-pressed', 'false');
  phase(call, 'ringing', 'Вызов…');
  try {
    if (!window.AudioContext || !window.AudioWorkletNode || !navigator.mediaDevices?.getUserMedia)
      throw new Error('Для аудиозвонка нужен браузер с доступом к микрофону.');
    call.ctx = new AudioContext();
    await call.ctx.resume();
    if (currentCall !== call) return;
    ring(call);
    call.ringer = setInterval(() => ring(call), 1850);
    const minRing = new Promise((resolve) => setTimeout(resolve, 2600 + Math.random() * 550));
    // Start loading on dial. Microphone frames are ignored until the greeting ends.
    call.permissionHint = setTimeout(() => {
      if (currentCall === call && call.phase === 'ringing' && !call.stream)
        phase(call, 'ringing', 'Ожидаем разрешения на микрофон…');
    }, 6000);
    const mic = async () => {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      if (currentCall !== call || call.phase !== 'ringing') {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      call.stream = stream;
      stream.getTracks().forEach((t) => {
        t.enabled = false;
      });
      await call.ctx.audioWorklet.addModule('/chat-assets/capture.js');
      if (currentCall !== call) return;
      call.node = new AudioWorkletNode(call.ctx, 'mira-capture');
      const gain = call.ctx.createGain();
      gain.gain.value = 0;
      call.ctx
        .createMediaStreamSource(stream)
        .connect(call.node)
        .connect(gain)
        .connect(call.ctx.destination);
      call.node.port.onmessage = (e) => microphoneChunk(call, e.data);
    };
    const loadGreeting = async () => {
      const r = await fetch('/chat-assets/hello.mp3?v=f2', { signal: call.abort.signal });
      if (!r.ok) throw new Error('Не удалось загрузить приветствие.');
      return r.arrayBuffer();
    };
    const [greeting] = await Promise.all([loadGreeting(), mic(), minRing]);
    if (currentCall !== call) return;
    clearInterval(call.ringer);
    clearTimeout(call.permissionHint);
    call.connected = true;
    el('callMic').disabled = false;
    const start = Date.now();
    const tick = () => {
      const s = Math.floor((Date.now() - start) / 1000);
      el('callTime').textContent =
        String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0');
    };
    tick();
    el('callTime').hidden = false;
    call.clock = setInterval(tick, 1000);
    await playback(call, greeting);
  } catch (error) {
    if (currentCall !== call) return;
    clearInterval(call.ringer);
    clearTimeout(call.permissionHint);
    if (!call.connected) call.stream?.getTracks().forEach((t) => t.stop());
    const reason = {
      NotAllowedError: 'Разрешите доступ к микрофону и перезвоните.',
      NotFoundError: 'Микрофон не найден. Подключите его и перезвоните.',
      NotReadableError: 'Микрофон занят. Освободите его и перезвоните.',
    }[error.name];
    callError(call, reason ? new Error(reason) : error);
  }
}
el('call').onclick = startCall;
el('endCall').onclick = hangup;
callScreen.oncancel = (e) => {
  e.preventDefault();
  hangup();
};
el('callMic').onclick = () => {
  const call = currentCall;
  if (!call?.connected) return;
  call.muted = !call.muted;
  el('callMic').setAttribute('aria-pressed', String(call.muted));
  if (['listening', 'muted'].includes(call.phase)) listening(call);
};
el('callAction').onclick = () => {
  const call = currentCall;
  if (!call) return;
  if (call.phase === 'speaking') {
    stopPlayer(call);
    listening(call);
  } else if (call.phase === 'listening') void submitAudio(call);
  else if (call.phase === 'error') {
    if (call.connected) listening(call);
    else {
      hangup();
      void startCall();
    }
  }
};
window.addEventListener('pagehide', hangup);
