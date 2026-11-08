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
    setTimeout(){}, performance:{now:()=>Date.now()}};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/speech.js','utf8'),context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/app.js','utf8'),context);
  vm.runInContext(`listener={context:{sampleRate:16000,close(){}},node:{port:{}},stream:{getTracks:()=>[]},
      gate:newSpeechGate(), uplink:{codec:'pcm', send:pcm=>ws.send(pcm), close(){}}};
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

function memoryPage(list=[]) {
  const elements = new Map(), calls = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id,{textContent:'',hidden:true,children:[],replaceChildren(...items){this.children=items;}});
    return elements.get(id);
  };
  const create = () => ({style:{},textContent:'',append(...items){this.children=items;}});
  const context = {$:get,document:{createElement:create},status(text){get('status').textContent=text;},console,
    fetch:async(url,options={})=>{calls.push([options.method||'GET',url]);return {ok:true,json:async()=>({memories:list})};}};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/memories.js','utf8'),context);
  return {get,context,calls};
}

test('memories are listed as plain text and an empty list shows the hint',async()=>{
  const page=memoryPage([{id:3,content:'<b>不吃香菜</b>'}]);
  await tick();
  assert.equal(page.get('memoryEmpty').hidden,true);
  assert.equal(page.get('memoryList').children[0].children[0].textContent,'<b>不吃香菜</b>');
  const empty=memoryPage([]);
  await tick();
  assert.equal(empty.get('memoryEmpty').hidden,false);
});

test('a new memory offers undo, and undo deletes it then hides the bar',async()=>{
  const page=memoryPage();
  page.context.memoryChanged({text:'记住了：不吃香菜',undo:{action:'delete',id:3}});
  assert.equal(page.get('undoBox').hidden,false);
  assert.equal(page.get('undoText').textContent,'记住了：不吃香菜');
  await page.get('undoButton').onclick();
  assert.deepEqual(page.calls.find(call=>call[0]==='DELETE'),['DELETE','/api/memories/3']);
  assert.equal(page.get('undoBox').hidden,true);
});

test('undoing a forget restores it',async()=>{
  const page=memoryPage();
  page.context.memoryChanged({text:'已经忘掉：不吃香菜',undo:{action:'restore',id:3}});
  await page.get('undoButton').onclick();
  assert.deepEqual(page.calls.find(call=>call[0]==='POST'),['POST','/api/memories/3/restore']);
});

test('clearing has nothing to undo',()=>{
  const page=memoryPage();
  page.context.memoryChanged({text:'已清空全部记忆',undo:null});
  assert.equal(page.get('undoBox').hidden,true);
});

test('a due reminder starts a new turn and asks to announce it',()=>{
  const page=confirmPage();
  page.receive({type:'reminder_due',request_id:null,reminder_id:7});
  assert.deepEqual(page.sent.slice(-2),[{type:'interrupt',request_id:1},{type:'announce',request_id:1,reminder_id:7}]);
});

test('reminders are listed with their time and can be cancelled then restored',async()=>{
  const page=memoryPage();
  page.context.fetch=async(url,options={})=>{page.calls.push([options.method||'GET',url]);
    return {ok:true,json:async()=>url==='/api/reminders'?{reminders:[{id:2,content:'开会',due_at:'2026-11-07T15:00:00'}]}:{memories:[]}};};
  await page.context.refreshReminders();
  assert.equal(page.get('reminderEmpty').hidden,true);
  const row=page.get('reminderList').children[0];
  assert.equal(row.children[0].textContent,'2026-11-07 15:00:00 开会');
  await row.children[1].onclick();
  assert.deepEqual(page.calls.find(call=>call[0]==='DELETE'),['DELETE','/api/reminders/2']);
  await page.get('undoButton').onclick();
  assert.deepEqual(page.calls.find(call=>call[0]==='POST'),['POST','/api/reminders/2/restore']);
});

function libraryPage(files, responses=[]) {
  const page=memoryPage();
  page.context.encodeURIComponent=encodeURIComponent;
  page.context.FormData=class{append(){}};
  page.context.fetch=async(url,options={})=>{page.calls.push([options.method||'GET',url]);
    const next=options.method?responses.shift():null;
    return next||{ok:true,json:async()=>url==='/api/knowledge'?{files}:{memories:[]}};};
  return page;
}

test('documents show how much text was found, or that none was',async()=>{
  const page=libraryPage([{name:'a.pdf',chunks:3},{name:'scan.pdf',chunks:0}]);
  await page.context.refreshKnowledge();
  const rows=page.get('knowledgeList').children;
  assert.equal(rows[0].children[0].textContent,'a.pdf（3 段）');
  assert.match(rows[1].children[0].textContent,/未提取到文字/);
});

test('deleting a document asks first, and declining sends nothing',async()=>{
  const page=libraryPage([{name:'子 目录/a.md',chunks:1}]);
  await page.context.refreshKnowledge();
  page.context.confirm=()=>false;
  await page.get('knowledgeList').children[0].children[1].onclick();
  assert.equal(page.calls.some(call=>call[0]==='DELETE'),false);
  page.context.confirm=()=>true;
  await page.get('knowledgeList').children[0].children[1].onclick();
  assert.deepEqual(page.calls.find(call=>call[0]==='DELETE'),['DELETE','/api/knowledge/%E5%AD%90%20%E7%9B%AE%E5%BD%95/a.md']);
});

test('an upload reports the result and clears the picker',async()=>{
  const page=libraryPage([],[{ok:true,json:async()=>({name:'a.pdf',chunks:0})}]);
  const input={files:[{name:'a.pdf'}],value:'x'};
  await page.get('knowledgeFile').onchange({target:input});
  assert.match(page.get('status').textContent,/没有提取到文字/);
  assert.equal(input.value,'');
});

test('a refused upload shows why',async()=>{
  const page=libraryPage([],[{ok:false,json:async()=>({detail:'只支持 md、txt、pdf、docx 文件'})}]);
  await page.get('knowledgeFile').onchange({target:{files:[{name:'a.exe'}],value:'x'}});
  assert.match(page.get('status').textContent,/只支持/);
});

function speechHelpers() {
  const context = {Math, Infinity};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/speech.js','utf8'), context);
  return context;
}
const QUIET = 0.0003, LOUD = 0.2, CHUNK_MS = 40;
function feed(gate, helpers, levels, start = 0, keepOpen = false) {
  return levels.map((rms, index) => helpers.gateChunk(gate, `c${start + index}`, rms, (start + index) * CHUNK_MS, CHUNK_MS, keepOpen));
}

test('silence never leaves the browser', () => {
  const helpers = speechHelpers(), gate = helpers.newSpeechGate();
  const results = feed(gate, helpers, Array(100).fill(QUIET));
  assert.equal(results.every(r => r.send.length === 0 && !r.gap), true);
});

test('speech is sent with the moments just before it, so the first syllable survives', () => {
  const helpers = speechHelpers(), gate = helpers.newSpeechGate();
  feed(gate, helpers, Array(30).fill(QUIET));
  const [first] = feed(gate, helpers, [LOUD], 30);
  assert.deepEqual([...first.send], ['c20', 'c21', 'c22', 'c23', 'c24', 'c25', 'c26', 'c27', 'c28', 'c29', 'c30']);
});

test('the gate stays open long enough for the server to see the sentence end, then closes once', () => {
  const helpers = speechHelpers(), gate = helpers.newSpeechGate();
  feed(gate, helpers, [LOUD]);
  const after = feed(gate, helpers, Array(60).fill(QUIET), 1);
  const sentUntil = after.findIndex(r => r.send.length === 0);
  assert.ok(sentUntil * CHUNK_MS >= 1000, `only ${sentUntil * CHUNK_MS} ms of trailing silence was sent`);
  assert.equal(after.filter(r => r.gap).length, 1);
  assert.equal(after[sentUntil].gap, true);
});

test('while a reply plays everything is sent, so talking over it can interrupt', () => {
  const helpers = speechHelpers(), gate = helpers.newSpeechGate();
  const results = feed(gate, helpers, Array(80).fill(QUIET), 0, true);
  assert.equal(results.every(r => r.send.length > 0), true);
});

test('pre-roll is bounded, however long the silence', () => {
  const helpers = speechHelpers(), gate = helpers.newSpeechGate();
  feed(gate, helpers, Array(1000).fill(QUIET));
  assert.ok(gate.buffer.length <= 11);
});

test('a quiet room sends nothing while listening', () => {
  const page = listenerPage();
  vm.runInContext('this.quiet=()=>listen(new Float32Array(1600).fill(0.0001))', page.context);
  for (let i = 0; i < 20; i++) page.context.quiet();
  assert.equal(page.sent.filter(item => typeof item !== 'string').length, 0);
});

test('the page announces whether it can play Ogg/Opus when it connects', () => {
  const page = listenerPage();
  page.context.Audio = function(){ this.canPlayType = () => 'probably'; };
  page.context.socket.onopen();
  assert.deepEqual(page.messages().find(m => m.type === 'client'), {type:'client', audio:['ogg-opus']});
});

function usagePage(data) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id,{textContent:'',open:false,children:[],listeners:{},
      addEventListener(name,fn){this.listeners[name]=fn;},replaceChildren(...items){this.children=items;}});
    return elements.get(id);
  };
  const cell = () => ({textContent:'',style:{}});
  const create = tag => tag==='table'
    ? {style:{},rows:[],insertRow(){const row={cells:[],insertCell(){const c=cell();this.cells.push(c);return c;},append(c){this.cells.push(c);}};this.rows.push(row);return row;}}
    : cell();
  const context = {$:get,document:{createElement:create},console,fetch:async()=>({ok:true,json:async()=>data})};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/usage.js','utf8'),context);
  return {get,context};
}
const TOTALS = {calls:3,yuan:0.000123,hit_tokens:800,miss_tokens:200,output_tokens:50,searches:1,incomplete:1,unpriced:0,cache_rate:0.8};

test('the spend panel loads when opened and shows money, cache rate and missing calls',async()=>{
  const page=usagePage({today:TOTALS,month:{...TOTALS,yuan:1.5},days:[{day:'2026-11-08',...TOTALS}],agents:[{agent:'main',...TOTALS}]});
  page.get('usagePanel').open=true; page.get('usagePanel').listeners.toggle();
  await tick();
  const text=page.get('usageBody').children.map(child=>child.textContent||'').join('\n');
  assert.match(text,/今天：¥0\.0001 · 2 次对话 · 1 次搜索/);
  assert.match(text,/本月：¥1\.50/);
  assert.match(text,/缓存命中 80%/);
  assert.match(text,/1 次被打断的对话没有拿到用量/);
  assert.equal(page.get('usageBody').children.filter(child=>child.rows).length,2);
});

test('only an admin response adds the per-account table, written as plain text',async()=>{
  const page=usagePage({today:TOTALS,month:TOTALS,days:[],agents:[],all:{month:TOTALS,users:[{email:'<b>x</b>@a.test',...TOTALS}]}});
  await page.get('usageRefresh').onclick();
  await tick();
  const tables=page.get('usageBody').children.filter(child=>child.rows);
  assert.equal(tables.length,1);
  assert.equal(tables[0].rows[1].cells[0].textContent,'<b>x</b>@a.test');
});

function taskPage(responses) {
  const elements = new Map(), calls = [], timers = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id,{textContent:'',hidden:false,open:false,children:[],style:{},replaceChildren(...items){this.children=items;}});
    return elements.get(id);
  };
  const create = () => ({style:{},textContent:'',children:[],append(...items){this.children.push(...items);}});
  let index = 0;
  const context = {$:get,document:{createElement:create},console,status(text){get('status').textContent=text;},
    setTimeout:(fn,ms)=>{timers.push(ms);return timers.length;},clearTimeout(){},
    fetch:async(url,options={})=>{calls.push([options.method||'GET',url]);
      const body=url==='/api/tasks'&&!options.method?responses[Math.min(index++,responses.length-1)]:{};
      return {ok:true,json:async()=>body};}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/tasks.js','utf8'),context);
  return {get,context,calls,timers};
}
const RUNNING = {id:1,title:'<i>周会</i>',status:'running',progress:'正在总结',result:null,error:'',seen:false};

test('a running task shows progress as plain text, can be cancelled, and the panel keeps polling',async()=>{
  const page=taskPage([{tasks:[RUNNING]}]);
  await tick();
  const row=page.get('taskList').children[0], head=row.children[0];
  assert.equal(head.children[0].textContent,'<i>周会</i>');
  assert.equal(head.children[1].textContent,'进行中');
  assert.equal(row.children[1].textContent,'正在总结');
  assert.deepEqual(page.timers,[3000]);
  await head.children[2].onclick();
  assert.deepEqual(page.calls.find(call=>call[0]==='POST'),['POST','/api/tasks/1/cancel']);
});

test('a task that finished is announced once and marked seen, and polling stops',async()=>{
  const page=taskPage([{tasks:[{...RUNNING,status:'done',progress:'',result:{text:'定了下周发布'}}]}]);
  await tick();
  assert.match(page.get('status').textContent,/后台任务完成：<i>周会<\/i>/);
  assert.deepEqual(page.calls.find(call=>call[0]==='POST'),['POST','/api/tasks/1/seen']);
  assert.equal(page.get('taskList').children[0].children[1].textContent,'定了下周发布');
  assert.deepEqual(page.timers,[]);
  const failed=taskPage([{tasks:[{...RUNNING,status:'failed',error:'今天的花费已到上限',seen:true}]}]);
  await tick();
  assert.equal(failed.get('status').textContent,'');
  assert.equal(failed.get('taskList').children[0].children[1].textContent,'今天的花费已到上限');
});
