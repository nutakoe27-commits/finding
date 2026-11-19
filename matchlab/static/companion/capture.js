class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(2048);
    this.used = 0;
  }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (samples)
      for (const sample of samples) {
        this.buffer[this.used++] = sample;
        if (this.used === this.buffer.length) {
          this.port.postMessage(this.buffer);
          this.used = 0;
        }
      }
    return true;
  }
}
registerProcessor('mira-capture', VoiceCapture);
