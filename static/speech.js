// Mic gain varies widely between devices, so speech is judged against each
// device's own noise floor. A fixed threshold misses quiet USB microphones.
const MIN_SPEECH_RMS = 0.004, FLOOR_RATIO = 3, FLOOR_RISE = 0.01;
function newSpeechDetector(minRms = MIN_SPEECH_RMS) { return {minRms, floor: minRms, peak: 0}; }
function detectSpeech(detector, rms) {
  const isSpeech = rms > Math.max(detector.minRms, detector.floor * FLOOR_RATIO);
  detector.peak = Math.max(detector.peak, rms);
  detector.floor = rms < detector.floor ? rms : detector.floor + (rms - detector.floor) * FLOOR_RISE;
  return isSpeech;
}
// Square root keeps quiet microphones visible on the meter.
function meterLevel(rms) { return Math.min(1, Math.sqrt(rms * 10)); }
// A microphone counts only if it actually heard speech; loudness alone proves nothing.
function bestMicResult(results) {
  const heard = results.filter(item => item.peak >= 0.4 && item.speech_share > 0);
  return heard.sort((a, b) => b.speech_share - a.speech_share || b.peak - a.peak)[0] || null;
}
function chunkRms(chunk) { return Math.sqrt(chunk.reduce((sum, x) => sum + x * x, 0) / chunk.length); }
// Silence stays in the browser. The server still decides what is speech; this only spares it hours of quiet.
// Pre-roll keeps the first syllable, which starts before it is loud enough to open the gate. The hangover must
// outlast the server's 0.6 s end-of-turn silence, or it would never see a sentence finish.
const GATE_PRE_ROLL_MS = 400, GATE_HANGOVER_MS = 1200;
function newSpeechGate(preRollMs = GATE_PRE_ROLL_MS, hangoverMs = GATE_HANGOVER_MS) {
  return {detector: newSpeechDetector(), open: false, lastSound: -Infinity, buffer: [], bufferMs: 0, preRollMs, hangoverMs};
}
// Returns the chunks to upload now, and gap: true once when the gate closes.
// keepOpen holds it open while a reply plays, so talking over it still reaches the server's barge-in checks.
function gateChunk(gate, chunk, rms, now, chunkMs, keepOpen = false) {
  const sound = detectSpeech(gate.detector, rms) || keepOpen;
  if (sound) gate.lastSound = now;
  if (gate.open) {
    if (now - gate.lastSound <= gate.hangoverMs) return {send: [chunk], gap: false};
    gate.open = false;
    gate.buffer = [{chunk, chunkMs}]; gate.bufferMs = chunkMs;
    return {send: [], gap: true};
  }
  if (sound) {
    const send = [...gate.buffer.map(item => item.chunk), chunk];
    gate.open = true; gate.buffer = []; gate.bufferMs = 0;
    return {send, gap: false};
  }
  gate.buffer.push({chunk, chunkMs}); gate.bufferMs += chunkMs;
  while (gate.buffer.length > 1 && gate.bufferMs - gate.buffer[0].chunkMs >= gate.preRollMs) gate.bufferMs -= gate.buffer.shift().chunkMs;
  return {send: [], gap: false};
}
