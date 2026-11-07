const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function setup(busy=false) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {textContent:'',value:'',disabled:false,replaceChildren(){},append(){}});
    return elements.get(id);
  };
  let requested, previewCalls=0;
  const entered = new Promise(resolve => requested=resolve);
  const state = {profiles:[],provider:'cosyvoice',profile_id:'test',clone_service_ready:true,clone_service_busy:busy,clone_device:'cpu'};
  const context = {$:get,document:{createElement:()=>({})},WebSocket:{OPEN:1},ws:{readyState:1},
    requestId:0,playing:new Set(),closeRecording(){},refreshHealth:async()=>{},unlockAudio:async()=>{},
    AbortController,setTimeout,clearTimeout,setInterval,clearInterval,console};
  context.window=context;
  context.nextTurn=()=>{context.requestId++;context.voicePreviewController?.abort();return context.requestId;};
  context.fetch=async(url,options={})=>{
    if(url==='/api/voices') return {ok:true,json:async()=>state};
    if(url==='/api/voice/preview') {
      previewCalls++; requested();
      return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}));
    }
    throw Error('Unexpected URL');
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/voices.js','utf8'),context);
  return {get,context,entered,calls:()=>previewCalls};
}

test('preview shows progress and cancellation clears the wait',async()=>{
  const ui=setup();
  await new Promise(resolve=>setImmediate(resolve));
  const pending=ui.get('previewVoice').onclick();
  await ui.entered;
  assert.match(ui.get('voiceStatus').textContent,/正在合成试听/);
  assert.equal(ui.get('previewVoice').disabled,true);
  ui.get('cancelVoice').onclick();
  await pending;
  assert.match(ui.get('voiceStatus').textContent,/试听已停止/);
  assert.equal(ui.get('previewVoice').disabled,false);
});

test('busy service is visible and does not start another preview',async()=>{
  const ui=setup(true);
  await new Promise(resolve=>setImmediate(resolve));
  await ui.get('previewVoice').onclick();
  assert.match(ui.get('voiceStatus').textContent,/上一条合成仍在运行/);
  assert.equal(ui.calls(),0);
  assert.equal(ui.get('previewVoice').disabled,false);
  assert.equal(ui.get('cancelVoice').disabled,true);
});

test('streamed audio keeps order and stale decoded audio cannot play after interruption',async()=>{
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id,{textContent:'',checked:true,disabled:false,value:'',append(){},remove(){}});
    return elements.get(id);
  };
  const starts = [];
  let completeDecode;
  const player = {currentTime:10,destination:{},resume:async()=>{},suspend:async()=>{},
    createGain:()=>({connect(){},gain:{setTargetAtTime(){}}}),
    decodeAudioData:async()=>({duration:1}),
    createBufferSource:()=>({connect(){},start(t){starts.push(t)},stop(){}})};
  const context = {document:{getElementById:get},location:{host:'127.0.0.1:8765'},
    AudioContext:function(){return player},atob,Uint8Array,console,navigator:{},
    fetch:async()=>({json:async()=>({})}),WebSocket:function(){context.socket=this;this.send=()=>{}}};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/app.js','utf8'),context);
  await vm.runInContext('unlockAudio()',context);
  const audio = () => context.socket.onmessage({data:JSON.stringify({type:'audio',request_id:0,data:'AA=='})});
  audio(); audio();
  await vm.runInContext('audioChain',context);
  assert.deepEqual(starts,[10.25,11.25]);
  player.decodeAudioData = () => new Promise(resolve=>completeDecode=resolve);
  audio();
  await new Promise(resolve=>setImmediate(resolve));
  vm.runInContext('nextTurn()',context);
  completeDecode({duration:1});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(starts.length,2);
});

function speechApi() {
  const context = {};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/speech.js','utf8')+';this.api={newSpeechDetector,detectSpeech,meterLevel};',context);
  return context.api;
}

test('quiet microphone speech is detected below the old fixed threshold',()=>{
  const {newSpeechDetector,detectSpeech}=speechApi(), detector=newSpeechDetector();
  for(let i=0;i<30;i++) assert.equal(detectSpeech(detector,0.0005),false);
  assert.equal(detectSpeech(detector,0.008),true);
});

test('steady room noise stops counting as speech while louder speech still does',()=>{
  const {newSpeechDetector,detectSpeech}=speechApi(), detector=newSpeechDetector();
  let last=true;
  for(let i=0;i<300;i++) last=detectSpeech(detector,0.02);
  assert.equal(last,false);
  assert.equal(detectSpeech(detector,0.1),true);
});

test('a silent device reports no speech and a zero peak',()=>{
  const {newSpeechDetector,detectSpeech}=speechApi(), detector=newSpeechDetector();
  for(let i=0;i<100;i++) assert.equal(detectSpeech(detector,0),false);
  assert.equal(detector.peak,0);
});

test('meter keeps quiet input visible',()=>{
  const {meterLevel}=speechApi();
  assert.ok(meterLevel(0.002)>0.1);
  assert.equal(meterLevel(1),1);
});

function listenerPage() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id,{textContent:'',checked:id==='tts',disabled:false,value:'',append(){},remove(){},replaceChildren(){}});
    return elements.get(id);
  };
  const sent = [], volume = [], clock = [];
  const player = {currentTime:100,destination:{},resume:async()=>{clock.push('resume')},suspend:async()=>{clock.push('suspend')},close(){},
    createGain:()=>({connect(){},gain:{setTargetAtTime:value=>volume.push(value)}})};
  const context = {document:{getElementById:get,createElement:()=>({append(){}})},location:{host:'127.0.0.1:8765'},
    AudioContext:function(){return player},atob,Uint8Array,Float32Array,ArrayBuffer,DataView,JSON,Math,console,navigator:{},
    fetch:async()=>({json:async()=>({})}),WebSocket:function(){context.socket=this;this.readyState=1;this.send=item=>sent.push(item)},
    setTimeout(){}};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/speech.js','utf8'),context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/app.js','utf8'),context);
  vm.runInContext(`listener={context:{sampleRate:16000,close(){}},node:{port:{}},stream:{getTracks:()=>[]}};
    this.unlock=unlockAudio;
    this.frame=()=>listen(new Float32Array(1600).fill(.05));
    this.play=text=>{spokenSegments=[{start:99,end:105,text}]};
    this.setReply=value=>{replyActive=value};
    this.isReplying=()=>replyActive;`,context);
  const messages = () => sent.filter(item=>typeof item==='string').map(JSON.parse);
  const voice = (event,data={}) => context.socket.onmessage({data:JSON.stringify({type:'voice',request_id:null,event,...data})});
  return {context,get,sent,messages,voice,volume,clock,player};
}

test('the microphone streams to the server as binary frames',()=>{
  const page=listenerPage();
  page.context.frame(); page.context.frame();
  const frames=page.sent.filter(item=>typeof item!=='string');
  assert.equal(frames.length,2);
  assert.equal(frames[0].byteLength,3200);
});

test('a manual recording takes over the microphone from the stream',()=>{
  const page=listenerPage();
  vm.runInContext('recording={}',page.context);
  page.context.frame();
  assert.equal(page.sent.length,0);
});

test('what is playing is reported once per change, not on every frame',async()=>{
  const page=listenerPage();
  await page.context.unlock();
  page.context.play('今天天气真不错');
  page.context.frame(); page.context.frame(); page.context.frame();
  const reports=page.messages().filter(item=>item.type==='playing');
  assert.equal(reports.length,1);
  assert.equal(reports[0].text,'今天天气真不错');
  assert.match(page.get('status').textContent,/播报中/);
});

test('the status follows the turn so a waiting page is never mistaken for a stuck one',()=>{
  const page=listenerPage();
  page.voice('start'); assert.match(page.get('status').textContent,/聆听中/);
  page.voice('transcribing'); assert.match(page.get('status').textContent,/识别中/);
  page.voice('ignored',{reason:'noise'}); assert.match(page.get('status').textContent,/没听清.*待命/);
  page.voice('question',{text:'你好'}); assert.match(page.get('status').textContent,/思考中/);
});

test('ignored speech says why',()=>{
  const page=listenerPage();
  page.voice('ignored',{reason:'echo'});
  assert.match(page.get('status').textContent,/喇叭/);
});

test('a question ends the old turn and asks the new one',()=>{
  const page=listenerPage();
  page.context.setReply(true);
  page.voice('question',{text:'换个话题'});
  const [interrupt,question]=page.messages().slice(-2);
  assert.equal(interrupt.type,'interrupt');
  assert.equal(question.type,'text');
  assert.equal(question.text,'换个话题');
  assert.equal(question.request_id,interrupt.request_id);
});

test('a stop ends the reply and asks nothing',()=>{
  const page=listenerPage();
  page.context.setReply(true);
  page.voice('stop');
  assert.equal(page.context.isReplying(),false);
  assert.equal(page.messages().at(-1).type,'interrupt');
  assert.equal(page.messages().some(item=>item.type==='text'),false);
});

test('pause freezes the audio clock so resume continues from the same word',async()=>{
  const page=listenerPage();
  await page.context.unlock();
  page.clock.length=0;
  page.voice('pause');
  assert.deepEqual(page.clock,['suspend']);
  assert.match(page.get('status').textContent,/已暂停/);
  page.voice('resume');
  assert.deepEqual(page.clock,['suspend','resume']);
  assert.equal(page.messages().some(item=>item.type==='interrupt'),false);
});

test('interrupting after a pause restarts the clock for the next reply',async()=>{
  const page=listenerPage();
  await page.context.unlock();
  page.voice('pause');
  page.clock.length=0;
  page.voice('interrupt');
  assert.ok(page.clock.includes('resume'));
  assert.equal(page.messages().at(-1).type,'interrupt');
});

test('playback ducks at the first sound and recovers when it was nothing',async()=>{
  const page=listenerPage();
  await page.context.unlock();
  page.context.setReply(true);
  page.voice('start');
  assert.equal(page.volume.at(-1),.25);
  page.voice('dropped');
  assert.equal(page.volume.at(-1),1);
});

test('a dropped connection stops listening honestly and reconnects',()=>{
  const page=listenerPage();
  let retry;
  page.context.setTimeout=fn=>{retry=fn};
  page.get('listen').checked=true;
  page.context.socket.onclose();
  assert.equal(page.get('listen').checked,false);
  assert.match(page.get('status').textContent,/正在重连/);
  const closed=page.context.socket;
  retry();
  assert.notEqual(page.context.socket,closed);
});

test('listening refuses to start against an old server and says what to do',async()=>{
  const page=listenerPage();
  page.get('listen').checked=true;
  await page.get('listen').onchange();
  assert.equal(page.get('listen').checked,false);
  assert.match(page.get('status').textContent,/旧版本.*重新启动/);
});

test('the live speech chance is shown for diagnosing a real microphone',()=>{
  const page=listenerPage();
  page.voice('meter',{chance:.27});
  assert.match(page.get('vad').textContent,/0\.27/);
  assert.equal(page.get('status').textContent,'');
});

test('talking over playback is acknowledged at once',async()=>{
  const page=listenerPage();
  await page.context.unlock();
  page.context.setReply(true);
  page.voice('start');
  assert.match(page.get('status').textContent,/听到你说话/);
});

test('playback ending mid-sentence does not claim the page is idle',async()=>{
  const page=listenerPage();
  await page.context.unlock();
  page.context.play('正在播报');
  page.context.frame();
  page.voice('start');
  vm.runInContext('spokenSegments=[]',page.context);
  page.context.frame();
  assert.doesNotMatch(page.get('status').textContent,/待命/);
});

test('the best microphone is the one that heard speech, not the loudest',()=>{
  const context={};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/speech.js','utf8')+';this.best=bestMicResult;',context);
  const loud={name:'loud',peak:.02,speech_share:0,rms:.09}, clear={name:'clear',peak:.97,speech_share:.5,rms:.01}, faint={name:'faint',peak:.6,speech_share:.1,rms:.002};
  assert.equal(context.best([loud,faint,clear]).name,'clear');
  assert.equal(context.best([loud]),null);
});

test('listening starts with browser noise suppression on until a test proves otherwise',()=>{
  const source=fs.readFileSync(__dirname+'/static/app.js','utf8');
  assert.match(source,/MIC_MODES = \[\s*\{label:'降噪\+增益', noiseSuppression:true, autoGainControl:true\}/);
});


function confirmPage() {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id,{textContent:'',checked:true,disabled:false,value:'',hidden:true,append(){},remove(){}});
    return elements.get(id);
  };
  const timers = [], sent = [];
  const context = {document:{getElementById:get},location:{host:'127.0.0.1:8765'},atob,Uint8Array,console,navigator:{},
    fetch:async()=>({json:async()=>({})}),setTimeout:(run,ms)=>timers.push({run,ms}),clearTimeout(){},setInterval(){},clearInterval(){},
    localStorage:{getItem:()=>null,setItem(){}},WebSocket:function(){context.socket=this;this.send=text=>sent.push(JSON.parse(text))}};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/speech.js','utf8'),context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/app.js','utf8'),context);
  context.socket.readyState=1;
  const receive = message => context.socket.onmessage({data:JSON.stringify(message)});
  return {get,context,sent,timers,receive};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('a confirmation shows buttons and reports ready only after the question has played',async()=>{
  const page=confirmPage();
  page.receive({type:'confirm_request',request_id:0,text:'要执行吗？'});
  assert.equal(page.get('confirmBox').hidden,false);
  assert.equal(page.get('confirmText').textContent,'要执行吗？');
  await tick();
  assert.equal(page.sent.length,0);
  page.timers.at(-1).run();
  assert.deepEqual(page.sent,[{type:'confirm_ready',request_id:0}]);
});

test('the buttons send the answer for the current turn',async()=>{
  const page=confirmPage();
  page.receive({type:'confirm_request',request_id:0,text:'要执行吗？'});
  page.get('confirmNo').onclick();
  assert.deepEqual(page.sent.at(-1),{type:'confirm_reply',request_id:0,approved:false});
  page.get('confirmYes').onclick();
  assert.equal(page.sent.at(-1).approved,true);
});

test('the server closing a confirmation hides the buttons and stops further answers',async()=>{
  const page=confirmPage();
  page.receive({type:'confirm_request',request_id:0,text:'要执行吗？'});
  page.receive({type:'confirm_done',request_id:0,approved:false});
  assert.equal(page.get('confirmBox').hidden,true);
  assert.match(page.get('status').textContent,/已取消/);
  const before=page.sent.length;
  page.get('confirmYes').onclick();
  assert.equal(page.sent.length,before);
});

test('interrupting hides the question and a late ready is not sent',async()=>{
  const page=confirmPage();
  page.receive({type:'confirm_request',request_id:0,text:'要执行吗？'});
  await tick();
  page.get('stop').onclick();
  assert.equal(page.get('confirmBox').hidden,true);
  page.timers.at(-1).run();
  assert.equal(page.sent.some(message=>message.type==='confirm_ready'),false);
});

test('a reply whose speech failed still ends with a status saying so',()=>{
  const page=confirmPage();
  page.receive({type:'confirm_done',request_id:0,approved:true});
  assert.match(page.get('status').textContent,/已确认/);
  page.receive({type:'done',request_id:0,tts_failed:true});
  assert.match(page.get('status').textContent,/语音播报失败/);
  page.receive({type:'done',request_id:0,tts_failed:false});
  assert.match(page.get('status').textContent,/回复完成；/);
});
