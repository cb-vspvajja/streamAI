const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ui/static/app.js'), 'utf8');
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {innerHTML:'',textContent:'',close(){this.closed=true;}});
  return elements.get(id);
};
const context = {Date,Set,String,Boolean,Number,state:{selectedTitle:{id:'movie::1',title:'Example'},status:{services:{ai_functions:{enabled:true}}}},
  $:element, formatNumber:x=>String(x), escapeHtml:x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
  isShowcaseExperience:()=>false,openAssistant:()=>{},sendChat:()=>{},loadMetrics:()=>{},refreshStatus:()=>{}};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('const guideRequests ='),source.indexOf('async function runDataProcessingProof()')),context);
const guide = {summary:'<img src=x onerror=alert(1)>',classification:'cerebral',sentiment:'neutral',generatedAt:1790590000,durationMs:1000,executionId:'abc',queryRequestId:'query-id',source:'Capella SQL++ AI Functions'};
const data = {titleId:'movie::1',title:'Example',enabled:true,guide};
context.renderViewingGuide(data);
assert.match(element('detailAIGuide').innerHTML,/Saved in Capella|Saved execution details/);
assert.match(element('detailAIGuide').innerHTML,/&lt;img/);
assert.doesNotMatch(element('detailAIGuide').innerHTML,/<img/);
assert.match(element('detailAIGuide').innerHTML,/parental controls and availability remain authoritative/);
assert.match(element('detailAIGuide').innerHTML,/Ask the assistant/);
context.renderViewingGuide({titleId:'movie::1',enabled:false,stale:true});
assert.match(element('detailAIGuide').innerHTML,/Synopsis changed/);
assert.match(element('detailAIGuide').innerHTML,/id="generateViewingGuide" disabled/);
(async()=>{
  let resolve, calls=0;
  context.api=()=>{calls++;return new Promise(r=>resolve=r);};
  const pending=context.generateViewingGuide('movie::1',false,data);
  await context.generateViewingGuide('movie::1',false,data);
  assert.equal(calls,1,'double-clicks must not generate twice');
  resolve({data:{result:{title:'Example'},guide,cacheHit:true}});
  await pending;
  assert.match(element('detailAIGuide').innerHTML,/No new AI Functions query/);
  context.api=async()=>{throw new Error('permission missing');};
  await context.generateViewingGuide('movie::1',true,data);
  assert.match(element('detailAIGuide').innerHTML,/permission missing.*You can retry/);
  assert.doesNotMatch(element('detailAIGuide').innerHTML,/id="generateViewingGuide" disabled/);
  context.api=()=>new Promise(r=>resolve=r);
  const oldRequest=context.generateViewingGuide('movie::1',true,data);
  context.state.selectedTitle={id:'movie::2',title:'Other'};
  element('detailAIGuide').innerHTML='Other title';
  resolve({data:{result:{title:'Example'},guide,cacheHit:false}});
  await oldRequest;
  assert.equal(element('detailAIGuide').innerHTML,'Other title','old responses must not overwrite another title');
  console.log('PASS: visible saved output, escaping, stale/disabled states, duplicate suppression, retry and title-switch protection');
})().catch(error=>{console.error(error);process.exitCode=1;});
