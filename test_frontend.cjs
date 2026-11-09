const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

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

test('a due reminder starts a new turn and asks to announce it',()=>{
  const page=confirmPage();
  page.receive({type:'reminder_due',request_id:null,reminder_id:7});
  assert.deepEqual(page.sent.slice(-2),[{type:'interrupt',request_id:1},{type:'announce',request_id:1,reminder_id:7}]);
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


// The React pages: no DOM renderer in node, so these load the page scripts with a stand-in React and test what
// the pages are built from (their logic, the voice flows, the toasts, the task poller, the live English dock).
const plain = value => JSON.parse(JSON.stringify(value));  // values from another vm context
function uiContext(files, extra={}) {
  const calls = [], timers = [];
  const React = {createElement:(type,props,...children)=>({type,props,children}), Fragment:'Fragment',
    useState(){throw Error('not rendering')}, useEffect(){}, useRef(){return {}}, useMemo(f){return f()}, useCallback(f){return f}};
  const node = tag => {
    const n = {tag,style:{},textContent:'',children:[],value:'',className:'',type:'',
      append(...items){for (const item of items) { item.parent = this; this.children.push(item); }},
      insertBefore(item, ref){item.parent = this; const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(item); else this.children.splice(i, 0, item);},
      querySelector(sel){return this.querySelectorAll(sel)[0] || null;},
      querySelectorAll(sel){const out=[];const visit=x=>{for(const c of x.children||[]){if(sel.startsWith('.')&&String(c.className).split(' ').includes(sel.slice(1)))out.push(c);visit(c);}};visit(this);return out;},
      get parentElement(){ return this.parent || null; },
      get nextSibling(){ const list = this.parent?.children || []; return list[list.indexOf(this) + 1] || null; },
      setAttribute(name, value){ this[name] = value; },
      replaceChildren(...items){ this.children = []; this.append(...items); },
      remove(){ if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); },
      get nextElementSibling(){ const list = this.parent?.children || []; return list[list.indexOf(this) + 1] || null; }};
    return n;
  };
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, node('div')); return elements.get(id); };
  const context = {React, console, EventTarget, CustomEvent, AbortController, FormData:class{constructor(){this.parts=[];}append(k,v){this.parts.push([k,v]);}},
    location:{hash:'',replace(){}}, addEventListener(){}, document:{createElement:node},
    setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;}, clearTimeout(){}, setInterval:()=>0, clearInterval(){},
    $, confirm:()=>true,
    fetch:async(url,options={})=>{calls.push([options.method||'GET',url,options.body]);
      const body = (extra.replies||{})[url] ?? {};
      return {ok:!body.detail,status:200,json:async()=>body};},
    ...extra.globals};
  context.window = context;
  vm.createContext(context);
  for (const file of ['core.js', ...files]) vm.runInContext(fs.readFileSync(__dirname+'/static/ui/'+file,'utf8'),context);
  const run = code => vm.runInContext(code, context);
  return {context, calls, timers, node, $, run, UI: context.UI};
}

test('the menu groups pages in a fixed order and sorts each group',()=>{
  const ui=uiContext(['pages.js','voices.js','meetings.js','audio.js','english.js']);
  vm.runInContext(fs.readFileSync(__dirname+'/static/ui/shell.js','utf8').split('ReactDOM.createRoot')[0],ui.context);
  const menu=plain(ui.UI.logic.menu(ui.UI.pages).map(s=>[s.group,s.pages.map(p=>p.title)]));
  assert.deepEqual(menu,[['助手',['对话','音色']],['我的资料',['长期记忆','待办提醒','知识库']],
    ['智能体',['会议记录','有声内容','英语对话','后台任务']],['账户',['花费','设置']]]);
});

test('formatting: money, tokens, clocks and times',()=>{
  const {format}=uiContext([]).UI.logic;
  assert.equal(format.yuan(0.000123),'¥0.0001'); assert.equal(format.yuan(1.5),'¥1.50'); assert.equal(format.yuan(0),'¥0.00');
  assert.equal(format.tokens(12345),'1.2万'); assert.equal(format.tokens(999),'999');
  assert.equal(format.clock(65),'01:05'); assert.equal(format.clock(3725),'01:02:05');
  assert.equal(format.when('2026-11-08T10:00:00+0800'),'2026-11-08 10:00');
});

test('a voice command that changed memories offers undo, and undo restores and refreshes',async()=>{
  const ui=uiContext(['pages.js']);
  let changed=0; ui.run("bus.addEventListener('data-changed',()=>{window.changed=(window.changed||0)+1})");
  ui.context.memoryChanged({text:'已经忘掉：不吃香菜',undo:{action:'restore',id:3}});
  const [item]=ui.run('toasts.get().items');
  assert.equal(item.text,'已经忘掉：不吃香菜'); assert.equal(item.action.label,'撤销');
  item.action.run(); await new Promise(r=>setImmediate(r));
  assert.deepEqual(ui.calls.at(-1).slice(0,2),['POST','/api/memories/3/restore']);
  assert.equal(ui.context.changed,2);
  ui.context.memoryChanged({text:'已清空全部记忆',undo:null});
  assert.equal(ui.run('toasts.get().items').at(-1).action,null);
});

test('cancelling a reminder by undo uses the reminder address',async()=>{
  const ui=uiContext(['pages.js']);
  await ui.run("changeItem({kind:'reminder',action:'delete',id:4})");
  assert.deepEqual(ui.calls.at(-1).slice(0,2),['DELETE','/api/reminders/4']);
});

test('service status reads as labelled lines',()=>{
  const lines=uiContext(['pages.js']).UI.logic.healthLines({asr_provider:'sensevoice',asr_ready:true,llm_model:'deepseek-flash',api_key_configured:false,
    tts_provider:'edge',tts_voice:'晓晓',tts_rate:'+0%',tools:['get_weather']});
  assert.deepEqual(plain(lines.map(l=>l[0])),['语音识别','DeepSeek','语音播报','可用工具']);
  assert.match(lines[1][1],/请设置 DEEPSEEK_API_KEY/); assert.match(lines[2][1],/语速 \+0%/);
});

test('spending tables and the notes about missing or unpriced calls',()=>{
  const {usageRows,usageNotes}=uiContext(['pages.js']).UI.logic;
  const totals={calls:3,yuan:0.000123,hit_tokens:800,miss_tokens:200,output_tokens:50,searches:1,incomplete:1,unpriced:0};
  assert.deepEqual(plain(usageRows([{agent:'tutor',...totals}],'agent')),[['tutor','¥0.0001','2','1','1050']]);
  assert.deepEqual(plain(usageNotes(totals)),['1 次被打断的对话没有拿到用量，实际花费会略高']);
  assert.deepEqual(plain(usageNotes({...totals,incomplete:0})),[]);
});

test('a finished task is announced once and marked seen; a running one keeps the poller going',async()=>{
  const running={id:1,title:'周会',status:'running',progress:'正在转写',seen:false};
  const ui=uiContext([],{replies:{'/api/tasks':{tasks:[running,{id:2,title:'兔子',status:'done',seen:false,result:{text:'好'}}],kinds:[]}}});
  await ui.UI.refreshTasks();
  assert.deepEqual(plain(ui.run('toasts.get().items').map(t=>t.text)),['后台任务完成：兔子']);
  assert.ok(ui.calls.some(c=>c[1]==='/api/tasks/2/seen'));
  assert.equal(ui.timers.at(-1).ms,3000);
  const {taskPreview}=ui.UI.logic;
  assert.equal(taskPreview(running),'正在转写');
  assert.equal(taskPreview({status:'failed',error:'超时'}),'超时');
  assert.equal(taskPreview({status:'done',result:{text:'x'.repeat(300)}}).length,200);
});

test('only ticked to-dos become reminders, with the time as edited',()=>{
  const {reminderItems}=uiContext(['meetings.js']).UI.logic;
  assert.deepEqual(plain(reminderItems([{task:'写说明',ticked:true,when:'2099-02-02T09:30'},{task:'想想',ticked:false,when:''},{task:'无时间',ticked:true,when:''}])),
    [{task:'写说明',due_at:'2099-02-02T09:30:00'},{task:'无时间',due_at:''}]);
});

test('script length and audio size read naturally',()=>{
  const {scriptLength,audioInfo}=uiContext(['audio.js']).UI.logic;
  assert.equal(scriptLength('字'.repeat(600)),'600 字，约 2 分钟'); assert.equal(scriptLength('短'),'1 字，约 1 分钟');
  assert.equal(audioInfo({seconds:75,bytes:1048576}),'1 分 15 秒 · 1.0 MB');
});

function voiceUi(busy=false) {
  let requested, previewCalls=0;
  const entered=new Promise(resolve=>requested=resolve);
  const state={profiles:[{id:'me',name:'我的声音'}],provider:'cosyvoice',profile_id:'me',clone_service_ready:true,clone_service_busy:busy,clone_device:'cpu'};
  const globals={WebSocket:{OPEN:1},ws:{readyState:1},requestId:0,playing:new Set(),closeRecording(){},unlockAudio:async()=>{}};
  const ui=uiContext(['pages.js','voices.js'],{globals});
  ui.context.nextTurn=()=>{ui.context.requestId++;ui.context.voicePreviewController?.abort();return ui.context.requestId;};
  vm.runInContext('var requestId=0',ui.context);
  ui.context.nextTurn=()=>{vm.runInContext('requestId++',ui.context);ui.context.voicePreviewController?.abort();return vm.runInContext('requestId',ui.context);};
  ui.context.setInterval=setInterval; ui.context.clearInterval=clearInterval; ui.context.setTimeout=setTimeout; ui.context.clearTimeout=clearTimeout;
  ui.context.fetch=async(url,options={})=>{
    if(url==='/api/voices') return {ok:true,json:async()=>state};
    if(url==='/api/voice/preview'){previewCalls++;requested();
      return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true}));}
    throw Error('Unexpected URL '+url);
  };
  const seen={status:'',previewing:false,cancellable:false,choices:[],value:''};
  const actions=ui.UI.logic.createVoiceActions({status:t=>seen.status=t,selected:()=>seen.value,setPreviewing:v=>seen.previewing=v,
    setCancellable:v=>seen.cancellable=v,setUploading(){},setChoices:(list,value)=>{seen.choices=list;seen.value=value;}});
  return {actions,seen,entered,calls:()=>previewCalls};
}

test('voices list the clone and say whether the service is free',async()=>{
  const ui=voiceUi();
  await ui.actions.load();
  assert.equal(ui.seen.value,'cosyvoice:me');
  assert.deepEqual(plain(ui.seen.choices.at(-1)),['cosyvoice:me','我的声音 · 自定义声音']);
  assert.match(ui.seen.status,/克隆服务：CPU · 空闲/);
});

test('preview shows progress and cancellation clears the wait',async()=>{
  const ui=voiceUi();
  const pending=ui.actions.preview();
  await ui.entered;
  assert.match(ui.seen.status,/正在合成试听/);
  assert.equal(ui.seen.previewing,true);
  ui.actions.cancel();
  await pending;
  assert.match(ui.seen.status,/试听已(停止|取消)/);
  assert.equal(ui.seen.previewing,false);
});

test('busy service is visible and does not start another preview',async()=>{
  const ui=voiceUi(true);
  await ui.actions.preview();
  assert.match(ui.seen.status,/上一条合成仍在运行/);
  assert.equal(ui.calls(),0);
  assert.equal(ui.seen.previewing,false);
  assert.equal(ui.seen.cancellable,false);
});

function englishDock() {
  const sent=[];
  const history=[];
  const ui=uiContext(['english.js'],{globals:{ws:{readyState:1,send:t=>sent.push(JSON.parse(t))},nextTurn:()=>9,unlockAudio:async()=>{},replyActive:false,replyBubble:null},
    replies:{'/api/practice':{levels:[],sessions:[],counts:{},level:'intermediate'}}});
  const bubbles=new Map();
  ui.context.userBubbleFor=rid=>bubbles.get(rid);
  ui.context.bubble=(role,text)=>{const box=ui.$('history');const b=ui.node('div');b.role=role;const c=ui.node('div');c.textContent=text;b.append(c);box.append(b);return c;};
  const live=role=>{const b=ui.node('div');b.role=role;ui.$('history').append(b);return b;};
  const state=()=>ui.run('english.get()');
  return {ui,sent,bubbles,live,state,handle:m=>ui.UI.english.handle(m)};
}
const textOf = n => [n.textContent, ...(n.children||[]).map(textOf)].join(' ');

test('live English: corrections go under your sentence, and redoing a sentence removes it and what came after',()=>{
  const d=englishDock();
  d.handle({type:'practice_started',level:'中级',topic:''});
  assert.equal(d.state().practicing,true);
  const opening=d.live('assistant'); d.ui.context.replyBubble={parentElement:opening};
  d.handle({type:'practice_turn',request_id:9,turn:0,who:'tutor'});
  const mine=d.live('user'); d.bubbles.set(12,mine);
  d.handle({type:'practice_turn',request_id:12,turn:1,who:'learner'});
  d.handle({type:'practice_note',request_id:12,turn:1,category:'语法',heard:'I go',original:'<b>I go</b>',corrected:'I went',better:'',explain:'过去式'});
  assert.match(textOf(mine),/语法：<b>I go<\/b>.*→ I went.*过去式/s);
  assert.ok(mine.children.indexOf(mine.querySelector('.practice-note')) < mine.children.indexOf(mine.querySelector('.turn-tools')));
  d.live('assistant');
  mine.querySelector('.practice-redo').onclick();
  assert.deepEqual(d.sent.at(-1),{type:'practice_rewind',request_id:9,turn:1});
  d.handle({type:'practice_rewound',request_id:9,turn:1,text:'I go to cinema'});
  assert.deepEqual(d.ui.$('history').children,[opening]);
  assert.equal(d.ui.$('text').value,'I go to cinema');
  assert.match(d.state().status,/已回到这句之前/);
});

test('live English: resuming shows the earlier turns with their notes and hands back the sentence',()=>{
  const d=englishDock();
  d.handle({type:'practice_started',level:'初级',topic:'food',redo:'I eat it every days',
    turns:[{who:'tutor',text:'What do you like?'},{who:'learner',text:'I like noodle'},{who:'tutor',text:'Nice!'}],
    notes:[{turn:1,category:'语法',heard:'I like noodle',original:'noodle',corrected:'noodles',better:'',explain:'复数'}]});
  const shown=d.ui.$('history').children;
  assert.deepEqual(plain(shown.map(b=>b.role)),['assistant','user','assistant']);
  assert.match(textOf(shown[1]),/→ noodles/);
  assert.ok(shown[1].querySelector('.practice-redo'));
  assert.equal(d.ui.$('text').value,'I eat it every days');
  assert.match(d.state().status,/接着之前的对话（初级，food）/);
});

test('live English: ending removes the redo buttons and says where the report is',()=>{
  const d=englishDock();
  d.handle({type:'practice_started',level:'中级',topic:''});
  const mine=d.live('user'); d.bubbles.set(3,mine);
  d.handle({type:'practice_turn',request_id:3,turn:1,who:'learner'});
  d.handle({type:'practice_stopped',task_id:5,notes:2,error:''});
  assert.equal(mine.querySelector('.practice-redo'),null);
  assert.equal(d.state().practicing,false);
  assert.match(d.state().status,/2 条批注已记进错题本/);
  d.handle({type:'practice_stopped',task_id:null,notes:0,error:'这次没有对话，不生成总结'});
  assert.match(d.state().status,/不生成总结/);
});

test('a correction for a sentence no longer on screen shows in the status line',()=>{
  const d=englishDock();
  d.handle({type:'practice_note',request_id:99,turn:7,category:'用词',heard:'x',original:'',corrected:'y',better:'',explain:''});
  assert.equal(d.state().status,'改法：x → y');
});

test('a review reads as labelled chips: pronunciation, words to practise, fluency, content',()=>{
  const {reviewChips}=uiContext(['english.js']).UI.logic;
  const chips=plain(reviewChips({pron:{overall:72,accuracy:65,fluency:80,completeness:100,words:[{word:'cinema',score:40},{word:'went',score:95}]},
    fluency:{wpm:110,pauses:2,longest_pause:0.8},logic:{score:4,comment:'回答切题'}}));
  assert.deepEqual(chips.map(c=>[c.label,c.tone]),[['发音','warn'],['要练的词','danger'],['流利度','neutral'],['内容','success']]);
  assert.equal(chips[0].text,'72 分（准确 65 · 流利 80 · 完整 100）');
  assert.equal(chips[1].text,'cinema（40）');
  assert.equal(chips[2].text,'语速 110 词/分 · 停顿 2 次（最长 0.8 秒）');
  assert.deepEqual(plain(reviewChips({pron:{error:'Key 无效'}})).map(c=>c.text),['评测失败：Key 无效']);
  assert.deepEqual(plain(reviewChips({logic:null,fluency:null,pron:null})),[]);
});

test('live English: a spoken sentence gets a replay button and its review under it',()=>{
  const d=englishDock();
  d.handle({type:'practice_started',level:'中级',topic:'',session_id:7});
  const mine=d.live('user'); d.bubbles.set(12,mine);
  d.handle({type:'practice_turn',request_id:12,turn:1,who:'learner',audio:true});
  const play=mine.querySelector('.practice-play');
  assert.ok(play);
  const played=[]; d.ui.context.Audio=function(url){played.push(url);this.play=async()=>{};this.pause=()=>{};};
  play.onclick();
  assert.deepEqual(played,['/api/practice/sessions/7/audio/1']);
  d.handle({type:'practice_review',request_id:12,turn:1,logic:{score:2,comment:'没有回答问题'},fluency:null,pron:null});
  assert.match(textOf(mine.querySelector('.practice-review')),/内容 2\/5 · 没有回答问题/);
  const typed=d.live('user'); d.bubbles.set(13,typed);
  d.handle({type:'practice_turn',request_id:13,turn:3,who:'learner',audio:false});
  assert.equal(typed.querySelector('.practice-play'),null);
  d.handle({type:'practice_review',request_id:13,turn:3,logic:null,fluency:null,pron:null});
  assert.equal(typed.querySelector('.practice-review'),null);
});

test('a correction offers both readings of the better sentence, and plays what the server made',async()=>{
  const d=englishDock();
  assert.equal(d.ui.UI.logic.sayText({corrected:'I went',better:'I went to the cinema.'}),'I went to the cinema.');
  assert.equal(d.ui.UI.logic.sayText({corrected:'I went',better:''}),'I went');
  const card=d.ui.UI.english.noteCard({category:'语法',original:'I go',corrected:'I went',better:'I went to the cinema.',explain:''});
  const standard=card.querySelector('.say-standard'), mine=card.querySelector('.say-mine');
  assert.equal(standard.textContent,'▶ 标准读法'); assert.equal(mine.textContent,'▶ 我的声音');
  const played=[];
  d.ui.context.Audio=function(url){played.push(url);this.play=async()=>{};this.pause=()=>{};};
  d.ui.context.URL={createObjectURL:()=> 'blob:said'};
  d.ui.context.fetch=async(url,options)=>{d.ui.calls.push([options.method,url,options.body]);return {ok:true,blob:async()=>({})};};
  await mine.onclick();
  assert.deepEqual(JSON.parse(d.ui.calls.at(-1)[2]),{text:'I went to the cinema.',voice:'mine'});
  assert.deepEqual(played,['blob:said']);
  assert.equal(mine.textContent,'▶ 我的声音'); assert.equal(mine.disabled,false);
});

test('live English: every sentence can show its Chinese translation, fetched once and then toggled',async()=>{
  const d=englishDock();
  d.handle({type:'practice_started',level:'中级',topic:'',session_id:7});
  const partner=d.live('assistant'); const label=d.ui.node('div'); const content=d.ui.node('div'); content.textContent='Oh no, that sounds rough.';
  partner.append(label, content); d.ui.context.replyBubble=content;
  d.handle({type:'practice_turn',request_id:9,turn:0,who:'tutor',audio:false});
  const button=partner.querySelector('.practice-zh');
  assert.equal(button.textContent,'译');
  let asked=0;
  d.ui.context.fetch=async(url,options)=>{asked++;return {ok:true,status:200,json:async()=>({text:'Oh no, that sounds rough.',zh:'哦不，听起来挺难受的。'})};};
  await button.onclick();
  const zh=partner.querySelector('.zh');
  assert.equal(zh.textContent,'哦不，听起来挺难受的。'); assert.equal(button.textContent,'收起译文');
  assert.equal(partner.children.indexOf(zh),2);  // right under the sentence, above any notes
  await button.onclick();
  assert.equal(zh.hidden,true); assert.equal(button.textContent,'译'); assert.equal(asked,1);
});

test('looking up a selection shows its meaning, phonetic, example and readings, and says it was saved',async()=>{
  const d=englishDock();
  d.ui.context.document.body=d.ui.node('body'); d.ui.context.innerWidth=1200; d.ui.context.innerHeight=800;
  d.ui.context.fetch=async(url,options)=>({ok:true,status:200,json:async()=>({text:'rough',phonetic:'/rʌf/',meaning:'（日子）难熬的',note:'sounds rough 意思是听起来很糟',example:'I had a rough day.',kind:'word',item_id:3})});
  await d.ui.UI.lookup.show('rough','Oh no, that sounds rough.',{left:100,bottom:200,top:180});
  const popup=d.ui.context.document.body.children[0];
  const text=textOf(popup);
  assert.match(text,/rough.*\/rʌf\//s); assert.match(text,/（日子）难熬的/); assert.match(text,/I had a rough day\./); assert.match(text,/已记进生词本/);
  assert.ok(popup.querySelector('.say-standard') && popup.querySelector('.say-mine'));
  assert.equal(popup.style.top,'208px');
  d.ui.UI.lookup.close();
  assert.equal(d.ui.context.document.body.children.length,0);
  const box=d.ui.node('div'); const who=d.ui.node('div'); const said=d.ui.node('div'); said.textContent='  I had a rough day '; box.append(who,said);
  assert.equal(d.ui.UI.lookup.contextOf(box),'I had a rough day');
});

test('review progress and the next review read naturally, and a used review word shows in the review',()=>{
  const {nextReviewText,scheduleText,reviewChips}=uiContext(['english.js']).UI.logic;
  const day=86400000, iso=ms=>new Date(Date.now()+ms).toISOString().slice(0,19);
  assert.equal(nextReviewText({mastered:true}),'已掌握，不再出题');
  assert.equal(nextReviewText({mastered:false,due_at:iso(day)}),'明天再考');
  assert.equal(nextReviewText({mastered:false,due_at:iso(4*day)}),'4 天后再考');
  assert.equal(nextReviewText({mastered:false,due_at:iso(0)}),'今天还会再考');
  assert.equal(scheduleText({mastered:0,tested:0}),'还没复习过');
  assert.equal(scheduleText({mastered:0,tested:3,correct:2,box:2,due_at:'2026-11-12T10:00:00'}),'复习第 2 级 · 答对 2/3 · 下次 2026-11-12 10:00');
  assert.equal(scheduleText({mastered:1}),'已掌握');
  assert.deepEqual(plain(reviewChips({used:['rough']})),[{label:'复习词',text:'用上了 rough，算一次复习',tone:'success'}]);
});

test('the admin metrics read as labelled rows',()=>{
  const {metricRows}=uiContext(['pages.js']).UI.logic;
  assert.deepEqual(plain(metricRows({latency:{asr:{n:12,p50:0.26,p95:0.96,max:1.2},custom:{n:1,p50:1,p95:1,max:1}}})),
    [['识别','12','0.26 秒','0.96 秒','1.20 秒'],['custom','1','1.00 秒','1.00 秒','1.00 秒']]);
});

test('opening a saved conversation clears the chat area and shows its messages; saving marks it current',()=>{
  const ui=uiContext(['pages.js']);
  const shown=[];
  ui.context.bubble=(role,text)=>{shown.push([role,text]);return ui.node('div');};
  ui.$('history').append(ui.node('div'));
  ui.context.chatMessage({type:'chat_started',chat_id:4,title:'周末',messages:[{role:'user',text:'去哪玩'},{role:'assistant',text:'爬山'}]});
  assert.equal(ui.$('history').children.length,1); assert.equal(ui.$('history').children[0].id,'empty');
  assert.deepEqual(shown,[['user','去哪玩'],['assistant','爬山']]);
  assert.equal(ui.run('chats.get().current'),4);
  ui.context.chatMessage({type:'chat_saved',chat_id:9,title:'新',turns:1});
  assert.equal(ui.run('chats.get().current'),9);
  const evening=new Date(2026,10,8,23,30);   // late evening: already the next day in UTC
  assert.equal(ui.UI.logic.chatWhen('2026-11-08T09:05:00',evening),'09:05');
  assert.equal(ui.UI.logic.chatWhen('2026-01-02T09:05:00'),'01-02');
});

test('account, reminder, recording and quiz helpers read naturally',()=>{
  const ui=uiContext(['pages.js','meetings.js','english.js']);
  const {passwordProblem,defaultReminderTime,recordingName,resultLabel,answerText}=ui.UI.logic;
  assert.equal(passwordProblem({current:'',next:'',again:''}),'请填写现在的密码');
  assert.equal(passwordProblem({current:'a',next:'short',again:'short'}),'新密码至少 8 位');
  assert.equal(passwordProblem({current:'old-pass1',next:'old-pass1',again:'old-pass1'}),'新密码不能和现在的一样');
  assert.equal(passwordProblem({current:'old-pass1',next:'new-pass1',again:'new-pass2'}),'两次输入的新密码不一样');
  assert.equal(passwordProblem({current:'old-pass1',next:'new-pass1',again:'new-pass1'}),'');
  assert.equal(defaultReminderTime(new Date(2026,10,9,10,25)),'2026-11-09T11:00');
  assert.equal(defaultReminderTime(new Date(2026,11,31,23,40)),'2027-01-01T00:00');
  assert.equal(recordingName(new Date(2026,10,9,14,5)),'会议录音-2026-11-09-1405.webm');
  assert.equal(resultLabel({ok:true,near:true}),'≈ 基本对'); assert.equal(resultLabel({ok:true}),'✓ 对了'); assert.equal(resultLabel({ok:false}),'✗ 不对');
  assert.equal(answerText({type:'choice',options:['a','b']},1),'b'); assert.equal(answerText({type:'choice',options:['a']},undefined),'（没答）');
  assert.equal(answerText({type:'spell'},' '),'（没答）'); assert.equal(answerText({type:'spell'},'rough'),'rough');
});

test('the page scripts share one global scope, so no two of them may declare the same top-level name',()=>{
  // A clash is a SyntaxError that stops the later script from running at all (it once silenced the voice engine).
  const names=file=>{const out=new Set();
    for(const line of fs.readFileSync(file,'utf8').split('\n')){
      const fn=line.match(/^(?:async\s+)?(?:function|class)\s+([A-Za-z_$][\w$]*)/); if(fn) out.add(fn[1]);
      if(!/^(?:let|const|var)\s/.test(line)) continue;
      let depth=0,current='';const parts=[];
      for(const ch of line.replace(/^(?:let|const|var)\s+/,'')){
        if('([{'.includes(ch)) depth++; if(')]}'.includes(ch)) depth--;
        if((ch===','||ch===';')&&depth===0){parts.push(current);current='';if(ch===';')break;} else current+=ch;
      }
      parts.push(current);
      for(const part of parts){const name=part.trim().match(/^([A-Za-z_$][\w$]*)/); if(name) out.add(name[1]);
        const destructured=part.trim().match(/^\{([^}]*)\}/); if(destructured) destructured[1].split(',').forEach(x=>{const k=x.trim().split(':').pop().trim(); if(k) out.add(k);});}
    }
    return out;};
  const html=fs.readFileSync(__dirname+'/static/index.html','utf8');
  const files=[...html.matchAll(/src="\/static\/([^"?]+)/g)].map(m=>m[1]).filter(f=>!f.startsWith('vendor/'));
  assert.ok(files.includes('app.js')&&files.includes('ui/meetings.js'));
  const seen=new Map(), clashes=[];
  for(const file of files) for(const name of names(__dirname+'/static/'+file)){
    if(seen.has(name)) clashes.push(`${name}: ${seen.get(name)} and ${file}`); else seen.set(name,file);
  }
  assert.deepEqual(clashes,[]);
});
