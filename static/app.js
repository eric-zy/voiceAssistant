const $ = id => document.getElementById(id);
let ws, requestId = 0, recording = null, capturePending = false, replyBubble;
let player, playAt = 0, audioChain = Promise.resolve();
let listener = null, replyActive = false, master = null, spokenSegments = [], reportedPlaying = '', wasPlaying = false;
const LISTEN_HINT = '待命中，直接说话即可', PLAYING_HINT = '播报中，直接说话可打断';
const IGNORED = {noise:'没听清，已忽略', echo:'听到的是喇叭声，已忽略', short:'太短，已忽略', early:'请等问题播完再回答'};
const CONFIRM_HINT = '请说“确定执行”或“取消”，也可以点按钮';
let confirming = null;
const playing = new Set();
function status(text) { $('status').textContent = text; }
function bubble(role, text = '') {
  $('empty')?.remove();
  const box = document.createElement('div'); box.className = 'bubble ' + role;
  const label = document.createElement('div'); label.className = 'label';
  label.textContent = role === 'user' ? '你' : 'DeepSeek 助手';
  const content = document.createElement('div'); content.textContent = text;
  box.append(label, content); $('history').append(box);
  $('history').scrollTop = $('history').scrollHeight; return content;
}
async function unlockAudio() {
  player ??= new AudioContext();
  if (!master) { master = player.createGain(); master.connect(player.destination); }
  await player.resume();
}
function stopPlayback() {
  window.voicePreviewController?.abort();
  for (const source of playing) { try { source.stop(); } catch {} }
  playing.clear(); playAt = 0; audioChain = Promise.resolve(); spokenSegments = [];
  player?.resume(); duck(false);
}
function endConfirm() { confirming = null; $('confirmBox').hidden = true; }
function askConfirm(rid, text) {
  confirming = rid; $('confirmText').textContent = text; $('confirmBox').hidden = false; status(CONFIRM_HINT);
  // Answers only count once the question has finished, so its echo cannot answer itself.
  audioChain.then(() => {
    const left = $('tts').checked && player ? Math.max(0, playAt - player.currentTime) : 0;
    setTimeout(() => {
      if (confirming === rid) ws.send(JSON.stringify({type:'confirm_ready', request_id:rid}));
    }, left * 1000 + 300);
  });
}
function answerConfirm(approved) {
  if (confirming !== null) ws.send(JSON.stringify({type:'confirm_reply', request_id:confirming, approved}));
}
function nextTurn() {
  ++requestId; stopPlayback(); replyBubble = null; replyActive = false; endConfirm();
  ws.send(JSON.stringify({type: 'interrupt', request_id: requestId}));
  return requestId;
}
function savedMic() { try { return localStorage.getItem('micDevice') || ''; } catch { return ''; } }
async function refreshMics() {
  const devices = await navigator.mediaDevices.enumerateDevices();
  const mics = devices.filter(device => device.kind === 'audioinput' && device.deviceId && device.deviceId !== 'communications');
  const select = $('micDevice'), chosen = select.value || savedMic();
  // Browsers hide device ids until the first microphone permission is granted.
  const choices = mics.length ? mics : [{deviceId:'default', label:'默认麦克风（授权录音后可选择）'}];
  select.replaceChildren(...choices.map(mic => {
    const option = document.createElement('option');
    option.value = mic.deviceId === 'default' ? '' : mic.deviceId;
    option.textContent = mic.label;
    return option;
  }));
  if (mics.some(mic => mic.deviceId === chosen)) select.value = chosen;
}
function closeRecording() {
  const rec = recording; recording = null;
  if (rec) {
    clearInterval(rec.timer); rec.node.port.onmessage = null;
    rec.stream.getTracks().forEach(track => track.stop());
    rec.context.close();
  }
  $('mic').textContent = '开始说话'; $('level').value = 0;
  return rec;
}
function toPCM(chunks, sourceRate) {
  const count = chunks.reduce((n, chunk) => n + chunk.length, 0);
  const input = new Float32Array(count); let offset = 0;
  for (const chunk of chunks) { input.set(chunk, offset); offset += chunk.length; }
  const ratio = sourceRate / 16000;
  const buffer = new ArrayBuffer(Math.floor(count / ratio) * 2), view = new DataView(buffer);
  // Average source intervals when downsampling; supports 44.1kHz and 48kHz devices.
  for (let i = 0; i < buffer.byteLength / 2; i++) {
    const begin = i * ratio, end = Math.min((i + 1) * ratio, input.length);
    let value = 0;
    for (let j = Math.floor(begin); j < Math.ceil(end); j++) {
      value += input[j] * (Math.min(j + 1, end) - Math.max(j, begin));
    }
    value = Math.max(-1, Math.min(1, value / (end - begin)));
    view.setInt16(i * 2, value < 0 ? value * 32768 : value * 32767, true);
  }
  return buffer;
}
function finishRecording() {
  const rec = closeRecording(); if (!rec || rec.rid !== requestId) return;
  if (!rec.heardSpeech) {
    status(`没有检测到说话：正在使用“${rec.deviceName}”，最大音量 ${rec.detector.peak.toFixed(4)}。请在上方换一个麦克风，或在系统声音设置中调高输入音量`);
    return;
  }
  const pcm = toPCM(rec.chunks, rec.context.sampleRate);
  ws.send(JSON.stringify({type:'audio_start', request_id:rec.rid}));
  ws.send(pcm);
  ws.send(JSON.stringify({type:'audio_end', request_id:rec.rid}));
  replyActive = true;
  status('正在上传本地录音…');
}
// Immediate feedback: confirming speech takes about a second.
function duck(active) {
  master?.gain.setTargetAtTime(active ? .25 : 1, player.currentTime, .03);
}
function assistantSpeaking() {
  return replyActive || playingNow().length > 0;
}
function playingNow() {
  const now = player?.currentTime ?? 0;
  return spokenSegments.filter(item => item.start < now + .2 && item.end > now - .5);
}
function reportPlaying() {
  const now = player?.currentTime ?? 0, current = playingNow();
  const text = current.map(item => item.text).join('');
  const recent = spokenSegments.filter(item => item.start < now + .2 && item.end > now - 8).map(item => item.text).join('');
  if (text + '|' + recent !== reportedPlaying) {
    reportedPlaying = text + '|' + recent;
    ws.send(JSON.stringify({type:'playing', text, recent}));
  }
  if (Boolean(text) !== wasPlaying) {
    wasPlaying = Boolean(text);
    if (!listener.capturing) status(wasPlaying ? PLAYING_HINT : LISTEN_HINT);
  }
}
// The server hears the stream and says what happened; the page only carries it out.
function handleVoice({event, text, reason, chance}) {
  if (!listener) return;
  if (event === 'meter') { $('vad').textContent = `语音概率 ${chance.toFixed(2)}（达到 0.40 算说话）`; return; }
  const speaking = assistantSpeaking();
  listener.capturing = ['start', 'pause', 'interrupt'].includes(event) || (listener.capturing && event === 'resume');
  if (event === 'start') { duck(speaking); status(speaking ? '听到你说话…' : '聆听中…'); }
  if (event === 'dropped') { duck(false); status(speaking ? PLAYING_HINT : LISTEN_HINT); }
  if (event === 'transcribing') status('识别中…');
  if (event === 'pause') { player?.suspend(); status('已暂停，正在听你说…'); }
  if (event === 'resume') { player?.resume(); duck(false); status(PLAYING_HINT); }
  if (event === 'interrupt') { nextTurn(); status('聆听中…'); }
  if (event === 'stop') { nextTurn(); status(LISTEN_HINT); }
  if (event === 'ignored') {
    duck(false);
    if (confirming !== null) status(`${IGNORED[reason] || IGNORED.noise}；${CONFIRM_HINT}`);
    else status(speaking ? PLAYING_HINT : `${IGNORED[reason] || IGNORED.noise}，待命中`);
  }
  if (event === 'question') {
    const rid = nextTurn();
    bubble('user', text); status('思考中…'); replyActive = true;
    ws.send(JSON.stringify({type:'text', request_id:rid, text, source:'listen'}));
  }
}
function listen(chunk) {
  $('level').value = meterLevel(chunkRms(chunk));
  if (recording) return;
  reportPlaying();
  ws.send(toPCM([chunk], listener.context.sampleRate));
}
const MIC_MODES = [
  {label:'降噪+增益', noiseSuppression:true, autoGainControl:true},
  {label:'只降噪', noiseSuppression:true, autoGainControl:false},
  {label:'原始声音', noiseSuppression:false, autoGainControl:false},
];
function savedMicMode() {
  try { return JSON.parse(localStorage.getItem('micMode')) || MIC_MODES[0]; } catch { return MIC_MODES[0]; }
}
async function openMic(deviceId, mode) {
  const stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,
    noiseSuppression:mode.noiseSuppression,autoGainControl:mode.autoGainControl,...(deviceId?{deviceId:{exact:deviceId}}:{})},video:false});
  const context = new AudioContext();
  try {
    await context.audioWorklet.addModule('/static/recorder.js');
    await context.resume();
  } catch (error) {
    stream.getTracks().forEach(track => track.stop()); context.close(); throw error;
  }
  const node = new AudioWorkletNode(context, 'recorder'), muted = context.createGain();
  muted.gain.value = 0;
  context.createMediaStreamSource(stream).connect(node); node.connect(muted); muted.connect(context.destination);
  return {stream, context, node};
}
function closeMic({stream, context, node}) {
  node.port.onmessage = null;
  stream.getTracks().forEach(track => track.stop());
  context.close();
}
async function startListening() {
  await unlockAudio();
  const mic = await openMic($('micDevice').value, savedMicMode());
  if (!$('listen').checked) { closeMic(mic); return; }
  listener = mic;
  ws.send(JSON.stringify({type:'listen_start'}));
  mic.node.port.onmessage = event => { if (listener?.node === mic.node) listen(event.data); };
  refreshMics().catch(()=>{});
  status(LISTEN_HINT);
}
async function recordSample(deviceId, mode, seconds) {
  const mic = await openMic(deviceId, mode), chunks = [];
  mic.node.port.onmessage = event => {
    chunks.push(event.data);
    $('level').value = meterLevel(chunkRms(event.data));
  };
  await new Promise(resolve => setTimeout(resolve, seconds * 1000));
  const rate = mic.context.sampleRate;
  closeMic(mic);
  return toPCM(chunks, rate);
}
// Which microphone and browser processing work is decided by this room's real
// sound. A simulation already picked the wrong setting once.
async function testMicrophones() {
  if (listener) { stopListening(); $('listen').checked = false; }
  await navigator.mediaDevices.getUserMedia({audio:true}).then(stream => stream.getTracks().forEach(track => track.stop()));
  await refreshMics();
  const options = [...$('micDevice').options], named = options.filter(option => option.value);
  const devices = (named.length ? named : options).map(option => ({id:option.value, name:option.textContent}));
  const results = [], total = devices.length * MIC_MODES.length;
  $('micTest').disabled = true; $('micReport').textContent = '';
  try {
    for (const device of devices) for (const mode of MIC_MODES) {
      status(`麦克风诊断 ${results.length + 1}/${total}：请不停地说话…`);
      const pcm = await recordSample(device.id, mode, 3.5);
      const response = await fetch('/api/mic-test', {method:'POST', body:pcm});
      if (!response.ok) throw Error('聊天服务还是旧版本，请重启 start.ps1');
      results.push({...await response.json(), device, mode});
      $('micReport').textContent = results.map(item =>
        `${item.device.name} · ${item.mode.label}：语音 ${Math.round(item.speech_share * 100)}%，概率 ${item.peak.toFixed(2)}，音量 ${item.rms.toFixed(4)}，听到“${item.heard || '无'}”`).join('\n');
    }
  } finally { $('level').value = 0; $('micTest').disabled = false; }
  const best = bestMicResult(results);
  if (!best) { status('诊断完成：所有麦克风都没听到人声，请检查麦克风是否静音或离得太远'); return; }
  $('micDevice').value = best.device.id;
  try {
    localStorage.setItem('micDevice', best.device.id);
    localStorage.setItem('micMode', JSON.stringify(best.mode));
  } catch {}
  status(`诊断完成：已选用 ${best.device.name} · ${best.mode.label}，可以勾选持续监听了`);
}
// A page newer than a still-running server would be disconnected by the first utterance.
async function enableListening() {
  const service = await fetch('/api/status').then(response => response.json());
  if (service.voice_protocol !== 4) throw Error('聊天服务还是旧版本，请在运行 start.ps1 的窗口按 Ctrl+C 后重新启动');
  if (ws.readyState !== 1) throw Error('尚未连接聊天服务，请稍候重试');
  await startListening();
}
function stopListening() {
  const active = listener; listener = null;
  if (!active) return;
  if (ws.readyState === 1) ws.send(JSON.stringify({type:'listen_stop'}));
  player?.resume(); duck(false); reportedPlaying = ''; wasPlaying = false;
  closeMic(active); $('level').value = 0; $('vad').textContent = '';
}
$('mic').onclick = async () => {
  if (recording) { finishRecording(); return; }
  if (capturePending) return;
  const rid = nextTurn(); capturePending = true;
  let stream, context;
  try {
    await unlockAudio();
    const deviceId = $('micDevice').value;
    stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true,...(deviceId?{deviceId:{exact:deviceId}}:{})},video:false});
    refreshMics().catch(()=>{});
    if (rid !== requestId) { stream.getTracks().forEach(t=>t.stop()); return; }
    context = new AudioContext(); await context.audioWorklet.addModule('/static/recorder.js');
    if (rid !== requestId) { stream.getTracks().forEach(t=>t.stop()); await context.close(); return; }
    await context.resume();
    const source = context.createMediaStreamSource(stream), node = new AudioWorkletNode(context,'recorder');
    const muted = context.createGain(); muted.gain.value = 0;
    source.connect(node); node.connect(muted); muted.connect(context.destination);
    const rec = {rid, context, stream, node, chunks:[], started:Date.now(), heardSpeech:false, lastSpeech:Date.now(),
      detector:newSpeechDetector(), deviceName:stream.getAudioTracks()[0]?.label || '默认麦克风'};
    recording = rec;
    node.port.onmessage = event => {
      if (recording !== rec) return;
      rec.chunks.push(event.data);
      const rms = Math.sqrt(event.data.reduce((sum,x)=>sum+x*x,0)/event.data.length);
      $('level').value = meterLevel(rms);
      if (detectSpeech(rec.detector, rms)) { rec.heardSpeech = true; rec.lastSpeech = Date.now(); }
    };
    rec.timer = setInterval(() => {
      const now = Date.now();
      if ((rec.heardSpeech && now-rec.lastSpeech > 900 && now-rec.started > 600) || now-rec.started > 29000 || (!rec.heardSpeech && now-rec.started > 10000)) finishRecording();
    },100);
    $('mic').textContent = '说完了，立即提交'; status('正在听…');
  } catch (error) {
    stream?.getTracks().forEach(t=>t.stop()); if(context) context.close();
    status('无法录音：'+error.message);
  } finally { capturePending = false; }
};
$('micDevice').onchange = async () => {
  try { localStorage.setItem('micDevice', $('micDevice').value); } catch {}
  if (!listener) return;
  stopListening();
  try { await enableListening(); } catch (error) { $('listen').checked = false; status('无法监听：'+error.message); }
};
$('listen').onchange = async () => {
  if (!$('listen').checked) { stopListening(); status('已停止持续监听'); return; }
  try { await enableListening(); } catch (error) { $('listen').checked = false; status('无法监听：'+error.message); }
};
$('micTest').onclick = () => testMicrophones().catch(error => status('诊断失败：' + error.message));
$('stop').onclick = () => { closeRecording(); nextTurn(); status('已打断，可重新说话'); };
$('tts').onchange = () => { if (!$('tts').checked) stopPlayback(); };
$('confirmYes').onclick = () => answerConfirm(true);
$('confirmNo').onclick = () => answerConfirm(false);
$('form').onsubmit = async event => {
  event.preventDefault(); const text = $('text').value.trim(); if (!text || ws.readyState !== 1) return;
  closeRecording(); const rid = nextTurn(); await unlockAudio();
  if(rid !== requestId) return;
  bubble('user',text); $('text').value=''; status('正在生成回复…'); replyActive = true;
  ws.send(JSON.stringify({type:'text', request_id:rid, text}));
};
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onopen = () => { status('已连接，可以开始'); for(const id of ['mic','stop','send']) $(id).disabled=false; };
  ws.onclose = () => {
    ++requestId; closeRecording(); stopPlayback(); stopListening(); $('listen').checked = false; endConfirm();
    replyActive = false; status('连接已断开，正在重连…');
    setTimeout(connect, 1500);
    for(const id of ['mic','stop','send']) $(id).disabled=true;
  };
  ws.onmessage = event => {
    const message = JSON.parse(event.data), rid = message.request_id;
    if (message.type === 'voice') { handleVoice(message); return; }
    if (rid !== requestId) return;
    if (message.type === 'transcript') bubble('user',message.text);
    if (message.type === 'token') {
      replyBubble ??= bubble('assistant'); replyBubble.textContent += message.text;
      $('history').scrollTop = $('history').scrollHeight;
    }
    if (['status','error','warning'].includes(message.type)) status(message.text);
    if (message.type === 'confirm_request') askConfirm(rid, message.text);
    if (message.type === 'confirm_done') { endConfirm(); duck(false); status(message.approved ? '已确认，正在执行…' : '已取消，操作未执行'); }
    if (message.type === 'done') status(message.tts_failed ? '回复完成，但语音播报失败，请看文字' : '回复完成；可以继续说话或打断播报');
    if (message.type === 'done' || message.type === 'error') replyActive = false;
    if (message.type === 'audio' && $('tts').checked) {
      audioChain = audioChain.then(async()=> {
        if(rid !== requestId || !$('tts').checked) return;
        const bytes = Uint8Array.from(atob(message.data),char=>char.charCodeAt(0));
        const buffer = await player.decodeAudioData(bytes.buffer);
        if(rid !== requestId || !$('tts').checked) return;
        const source = player.createBufferSource(); source.buffer=buffer; source.connect(master);
        // Buffer the first chunk (or an underrun) briefly; adjacent chunks share
        // the same timeline and do not acquire an artificial pause on every chunk.
        if (playAt <= player.currentTime) playAt = player.currentTime+.25;
        source.start(playAt);
        spokenSegments.push({start:playAt, end:playAt+buffer.duration, text:message.text || ''});
        playAt+=buffer.duration;
        playing.add(source); source.onended=()=>playing.delete(source);
      }).catch(error=>status('播放失败：'+error.message));
    }
  };
}
function refreshHealth() { return fetch('/api/status').then(r=>r.json()).then(data=>{
  $('health').textContent = `识别模型：${data.asr_ready?'已准备':'尚未下载，请运行 prepare_asr.py'}\nDeepSeek：${data.llm_model} · ${data.api_key_configured?'Key 已载入（不代表聊天调用成功）':'请设置 DEEPSEEK_API_KEY 后重启'}\n接口：${data.api_host || '未知'} · Key 指纹：${data.key_fingerprint || '旧版服务，请重启'}\n语音播报：${data.tts_provider || 'windows'} · ${data.tts_voice}${data.tts_provider==='edge'?' · 语速 '+data.tts_rate:''}${data.tts_provider==='cosyvoice'?' · '+({balanced:'短首句优先',stream:'流式播放',smooth:'完整回复连续播放'}[data.clone_playback] || '请重启聊天服务'):''}
可用工具：${(data.tools || []).join('、') || '无'}`;
}).catch(()=> $('health').textContent='状态检查失败'); }
refreshHealth();
refreshMics().catch(()=>{});
navigator.mediaDevices?.addEventListener('devicechange', () => refreshMics().catch(()=>{}));
connect();
