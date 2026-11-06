class Recorder extends AudioWorkletProcessor {
  constructor() { super(); this.samples = []; }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) {
      for (const sample of channel) this.samples.push(sample);
      if (this.samples.length >= 2048) {
        const batch = new Float32Array(this.samples);
        this.port.postMessage(batch, [batch.buffer]); this.samples = [];
      }
    }
    return true;
  }
}
registerProcessor('recorder', Recorder);
