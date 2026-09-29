const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../ui/static/app.js'), 'utf8');
const elements = new Map();
const context = { Intl, Date, Number, String, state: {}, isShowcaseExperience: () => true,
  escapeHtml: x => String(x), $: id => { if (!elements.has(id)) elements.set(id, {textContent:'',innerHTML:'',style:{}}); return elements.get(id); } };
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function formatNumber('), source.indexOf('async function loadMetrics(')), context);
const text = () => [...elements.values()].map(e=>e.innerHTML+' '+e.textContent).join(' ');
const render = data => { elements.clear(); context.renderMetrics(data); };
render({runtime:{description:'Capella Model Service · mistral'}, measured:{available:false,error:'Gateway unavailable'}, totals:{llmCalls:0}, derived:{tokenReductionPct:100,estimatedCostSaved:1000}});
assert.match(text(), /Unavailable/); assert.doesNotMatch(text(), /100%|£|1,000|Ollama/);
render({measured:{available:true,windowStartedAt:1,activityViewer:'sai',totals:{requests:2,chatRequests:2,embeddingRequests:0,failed:0,inFlight:0,requestsWithMissingUsage:1,reportedInputTokens:12,inputUsageReports:1,reportedOutputTokens:3,chatOutputUsageReports:1},activity:{meanServerAnswerMs:null},bySource:[{source:'agent_memory',operation:'chat/completions',model:'mistral',requests:2,failed:0,unknownOutcome:0,inputUsageReports:1,reportedInputTokens:12,chatOutputUsageReports:1,reportedOutputTokens:3,requestsWithMissingUsage:1}]}});
assert.match(text(), /2 from Agent Memory/); assert.match(text(), /1 inference requests have missing/);
assert.match(text(), /1 \/ 2/); assert.match(text(), /12/);
assert.equal(elements.get('tokensUsedBar').style.width, '50%');
assert.equal(elements.get('tokensSavedBar').style.width, '50%');
assert.match(text(), /Not measured/);
render({measured:{available:true,totals:{requests:1,chatRequests:1,requestsWithMissingUsage:1,inputUsageReports:0,chatOutputUsageReports:0,reportedInputTokens:0,reportedOutputTokens:0},recentRequests:[{source:'app',operation:'chat/completions',status:'unknown_outcome',timestamp:1,usage:{}}]}});
assert.match(elements.get('metricsGrid').innerHTML, /Reported input tokens<\/span><strong>Unavailable/);
assert.match(elements.get('metricsEventList').innerHTML, /Input unknown/);
console.log('PASS: unavailable data, legacy rejection, background scope, partial sums, coverage arithmetic and unknown tokens');

const phases = {
 previous:{llmRequests:6,llmTokens:7200,llmInputTokens:6000,llmOutputTokens:1200,embeddingRequests:4,embeddingInputTokens:800,referenceCostUsd:.02708},
 optimized:{llmRequests:0,llmTokens:0,llmInputTokens:0,llmOutputTokens:0,embeddingRequests:4,embeddingInputTokens:800,referenceCostUsd:.00008}
};
const trial={id:"trial",status:"completed",validComparison:true,turns:4,facts:2,phases,
 difference:{llmRequests:6,llmTokens:7200,referenceCostUsd:.027},llmReductionPct:100};
render({measured:{available:true,memorySavings:trial}});
assert.match(elements.get('savingsCards').innerHTML,/LLM calls avoided<\/span><strong>6/);
assert.match(elements.get('savingsCards').innerHTML,/LLM tokens saved<\/span><strong>7,200/);
assert.match(elements.get('savingsCards').innerHTML,/Token-cost equivalent saved<\/span><strong>\$0.027/);
assert.match(elements.get('savingsCoverage').textContent,/not whole-application or billed savings/);
assert.match(elements.get('comparisonReduction').textContent,/100.0% fewer measured/);
assert.doesNotMatch(elements.get('savingsTable').innerHTML,/Assumed baseline/);
render({measured:{available:true,memorySavings:{...trial,difference:{llmRequests:-1,llmTokens:-20,referenceCostUsd:-.001},llmReductionPct:-1}}});
assert.match(elements.get('savingsCards').innerHTML,/1 extra/);
assert.match(elements.get('savingsCards').innerHTML,/\$0.001 extra/);
render({measured:{available:true,memorySavings:{...trial,validComparison:false,problems:["Missing token reports"]}}});
assert.match(elements.get('savingsCards').innerHTML,/Unavailable/);
assert.doesNotMatch(elements.get('savingsCards').innerHTML,/<strong>7,200|<strong>\$0.027/);
assert.match(elements.get('savingsStatus').textContent,/Missing token reports/);
render({measured:{available:true,comparison:{completedChats:6,estimatedDifference:{llmRequests:999,llmTokens:999}}}});
assert.doesNotMatch(elements.get('savingsCards').innerHTML,/999/);
assert.match(elements.get('savingsCards').innerHTML,/Run comparison/);
console.log('PASS: measured policy savings, actual negatives, incomplete suppression, and legacy assumptions excluded from headline');
