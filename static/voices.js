function voiceStatus(text) { $('voiceStatus').textContent = text; }
async function loadVoices(preferred) {
  const response = await fetch('/api/voices');
  if(!response.ok) throw new Error('读取音色失败');
  const data = await response.json(), select = $('voiceSelect');
  select.replaceChildren();
  const choices = [
    ['edge:zh-CN-XiaoxiaoNeural','晓晓女声 · 在线'],
    ['edge:zh-CN-YunxiNeural','云希男声 · 在线'],
    ['windows:Microsoft Huihui Desktop','慧慧 · 本地 Windows'],
    ...data.profiles.map(p=>['cosyvoice:'+p.id,p.name+' · 自定义声音'])
  ];
  for(const [value, label] of choices) {
    const option = document.createElement('option'); option.value=value; option.textContent=label; select.append(option);
  }
  select.value = preferred || (data.provider==='cosyvoice'?'cosyvoice:'+data.profile_id:data.provider+':'+data.voice);
  const device=data.clone_device==='cuda'?'GPU':data.clone_device==='cpu'?'CPU':'未知设备';
  voiceStatus(data.clone_service_ready?`克隆服务：${device} · ${data.clone_service_busy?'上一条合成仍在运行，请稍后重试':'空闲，可以试听'}。`:'本机克隆服务未就绪。晓晓/云希仍可使用；克隆声音需要先运行 start_cosyvoice.ps1。');
  return data;
}
async function postJSON(url, body) {
  const response = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data = await response.json();
  if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'配置失败');
  return data;
}
$('applyVoice').onclick = async()=> {
  try {
    if(ws.readyState!==WebSocket.OPEN) throw new Error('请先等待聊天连接就绪');
    closeRecording(); nextTurn();
    const value=$('voiceSelect').value, separator=value.indexOf(':'), provider=value.slice(0,separator), name=value.slice(separator+1);
    if(!provider) throw new Error('请选择音色');
    await postJSON('/api/voice/select', {provider,voice:provider==='cosyvoice'?'custom':name,profile_id:provider==='cosyvoice'?name:''});
    await refreshHealth();
    const info=await loadVoices();
    if(provider!=='cosyvoice') voiceStatus('已应用；可以点击试听。');
    else if(!info.clone_service_ready) voiceStatus('音色已保存，但克隆服务尚未就绪；请先运行 start_cosyvoice.ps1。');
    else if(info.clone_service_busy) voiceStatus('音色已应用；上一条合成尚未结束，请稍后再试听。');
    else voiceStatus(`音色已应用，使用${info.clone_device==='cuda'?'GPU':'CPU'}合成。可以点击试听。`);
  } catch(error) { voiceStatus(error.message); }
};
$('uploadVoice').onclick = async()=> {
  const button=$('uploadVoice'); button.disabled=true;
  try {
    const file=$('voiceFile').files[0]; if(!file) throw new Error('请选择参考录音');
    if(file.size>10*1024*1024) throw new Error('参考录音不能超过10MB');
    const form=new FormData(); form.append('name',$('voiceName').value); form.append('prompt_text',$('voiceTranscript').value); form.append('audio',file);
    voiceStatus('正在转换并保存参考录音…');
    const response=await fetch('/api/voices',{method:'POST',body:form}), data=await response.json();
    if(!response.ok) throw new Error(typeof data.detail==='string'?data.detail:'请填写名称、录音及对应文字');
    await loadVoices('cosyvoice:'+data.id);
    voiceStatus('参考音色已保存在本机。请点击“应用音色”，启动克隆服务后试听。');
  } catch(error) { voiceStatus(error.message); }
  finally { button.disabled=false; }
};
$('previewVoice').onclick = async()=> {
  const button=$('previewVoice'); button.disabled=true;
  let timer, deadline, rid, timedOut=false;
  $('cancelVoice').disabled=false;
  voiceStatus('正在准备试听…');
  try {
    if(ws.readyState!==WebSocket.OPEN) throw new Error('请先等待聊天连接就绪');
    closeRecording(); rid=nextTurn();
    window.voicePreviewController=new AbortController();
    // Activate playback inside the click gesture, before any network await.
    let audioTimer;
    try {
      await Promise.race([unlockAudio(),new Promise((_,reject)=>{audioTimer=setTimeout(()=>reject(new Error('浏览器未允许播放，请再点击试听或检查站点声音权限')),5000);})]);
    } finally { clearTimeout(audioTimer); }
    if(rid!==requestId) return;
    const info=await (await fetch('/api/voices',{signal:window.voicePreviewController.signal})).json();
    if(info.provider==='cosyvoice' && !info.clone_service_ready) throw new Error('克隆服务未就绪，请运行 start_cosyvoice.ps1');
    if(info.provider==='cosyvoice' && info.clone_service_busy) throw new Error('上一条合成仍在运行，请稍后再试听；取消等待不一定立即终止模型计算。');
    if(rid!==requestId) return;
    const started=Date.now();
    const progress=()=>voiceStatus(`正在合成试听，已等待 ${Math.floor((Date.now()-started)/1000)} 秒${info.provider==='cosyvoice'?(info.clone_device==='cuda'?' · GPU 推理':' · CPU 较慢，可能需要一分钟以上'):''}。可点击“停止试听”。`);
    progress(); timer=setInterval(progress,1000);
    deadline=setTimeout(()=>{timedOut=true;window.voicePreviewController.abort();},120000);
    const response=await fetch('/api/voice/preview',{method:'POST',signal:window.voicePreviewController.signal});
    if(!response.ok) { const error=await response.json(); throw new Error(error.detail || '试听失败'); }
    const data=await response.arrayBuffer();
    if(rid!==requestId) return;
    const buffer=await player.decodeAudioData(data);
    if(rid!==requestId) return;
    const source=player.createBufferSource(); source.buffer=buffer; source.connect(player.destination);
    clearInterval(timer); clearTimeout(deadline);
    playing.add(source); source.onended=()=>{playing.delete(source);if(rid===requestId){voiceStatus('试听播放完毕');$('cancelVoice').disabled=true;}}; source.start();
    voiceStatus('正在播放当前音色。可点击“停止试听”。');
  } catch(error) {
    if(rid===undefined || rid===requestId) voiceStatus(timedOut?'试听超过120秒，已停止等待。请检查服务状态或暂用晓晓/云希。':error.name==='AbortError'?'试听已取消':error.message);
  }
  finally { clearInterval(timer);clearTimeout(deadline);button.disabled=false;$('cancelVoice').disabled=playing.size===0; }
};
$('cancelVoice').onclick=()=>{if(ws.readyState===WebSocket.OPEN)nextTurn();else stopPlayback();voiceStatus('试听已停止；已开始的克隆计算可能仍需结束。');$('cancelVoice').disabled=true;};
$('checkVoice').onclick=()=>loadVoices().catch(error=>voiceStatus(error.message));
loadVoices().catch(error=>voiceStatus(error.message));
