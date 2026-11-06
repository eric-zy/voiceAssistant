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
