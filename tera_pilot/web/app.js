/* ===================================================================
   TERA_PILOT v2.0.0 — REAL frontend logic (English)
   =================================================================== */

window.__apiBase = null;  // Set by __teraPilotReady from the local API server
// v1.0.5-security: bearer token for mutating endpoints on the local HTTP
// API server (CSRF-to-localhost defense, BUGS_REPORT C-API-1).
// Populated from `status.api_token` in window.__teraPilotReady.
window.__apiToken = null;

// Helper: build headers for a mutating HTTP request.
// Adds `Authorization: Bearer <token>` when a token is available.
function _apiHeaders(extra){
  var h = extra || {};
  h['Content-Type'] = 'application/json';
  if (window.__apiToken){
    h['Authorization'] = 'Bearer ' + window.__apiToken;
  }
  return h;
}

const state = {
  activeChatId: null,
  activeProvider: 'ollama',
  activeTemplate: null,
  activeSkill: null,
  isGenerating: false,
  chats: [],
  providers: [],
  templates: [],
  skills: [],
  snippets: [],
  files: { app: [], tests: [], root: [] },
  openTabs: new Map(),
  activeTab: null,
  projectRoot: null,
  activeModalTab: 'appearance',
  neuralBgDisabled: false,
  sessionTokens: 0,
  sessionCost: 0,
  sessionRequests: 0,
  autoRoute: false,
  agentMode: false,   // explicit toggle
  agentAutonomy: "always_ask",
  fileTreePanelOpen: false,
  lastRouterDecision: null, // replaces the old regex auto-detect
};

const PROVIDER_META = {
  // ── Local (no key) ───────────────────────────────────────────────
  lmstudio:   { label:'LM Studio', model:'',                              needsKey:false, statusLabel:'LM Studio',         modelDisplay:'LM Studio', keyHint:'Download LM Studio, load a model, and start its local server — no account or key needed.', keyUrl:'https://lmstudio.ai' },
  ollama:     { label:'Ollama',     model:'llama3.3',                    needsKey:false, statusLabel:'Ollama · Llama 3.3',       modelDisplay:'Llama 3.3', keyHint:'Install Ollama and pull a model — runs fully on your machine, no key needed.', keyUrl:'https://ollama.com/download' },
  vllm:       { label:'vLLM',       model:'meta-llama/Meta-Llama-3.1-8B-Instruct', needsKey:false, statusLabel:'vLLM · Llama 3.1 8B', modelDisplay:'Llama 3.1 8B', keyHint:'Self-hosted OpenAI-compatible server. Set TERA_PILOT_VLLM_BASE_URL env var to point at your server.', keyUrl:'https://docs.vllm.ai' },
  koboldcpp:  { label:'KoboldCpp',  model:'local-model',                 needsKey:false, statusLabel:'KoboldCpp · Local',        modelDisplay:'KoboldCpp local', keyHint:'Local GGUF server with an OpenAI-compatible API. Start KoboldCpp and load any model.', keyUrl:'https://github.com/LostRuins/koboldcpp' },
  llamafile:  { label:'llamafile',  model:'local-model',                 needsKey:false, statusLabel:'llamafile · Local',        modelDisplay:'llamafile local', keyHint:'Mozilla\'s single-file LLM runner. Start with --server and load any .llamafile.', keyUrl:'https://github.com/Mozilla-Ocho/llamafile' },
  // ── Major cloud providers ────────────────────────────────────────
  openai:     { label:'OpenAI',     model:'gpt-5.5',                      needsKey:true,  statusLabel:'OpenAI · GPT-5.5',         modelDisplay:'GPT-5.5', keyHint:'Requires billing set up on your OpenAI account before keys work.', keyUrl:'https://platform.openai.com/api-keys' },
  anthropic:  { label:'Anthropic',  model:'claude-sonnet-5',              needsKey:true,  statusLabel:'Anthropic · Sonnet 5',    modelDisplay:'Claude Sonnet 5', keyHint:'Best for coding/agentic tasks. Requires billing set up on your account.', keyUrl:'https://console.anthropic.com/settings/keys' },
  gemini:     { label:'Google Gemini', model:'gemini-3.1-pro',           needsKey:true,  statusLabel:'Gemini · 3.1 Pro',           modelDisplay:'Gemini 3.1 Pro', keyHint:'Free tier available through Google AI Studio.', keyUrl:'https://aistudio.google.com/apikey' },
  deepseek:   { label:'DeepSeek',   model:'deepseek-v4-pro',             needsKey:true,  statusLabel:'DeepSeek · V4 Pro',        modelDisplay:'DeepSeek V4 Pro', keyHint:'Low-cost, strong coding performance.', keyUrl:'https://platform.deepseek.com/api_keys' },
  zai:        { label:'Z.ai',       model:'glm-5.1',                      needsKey:true,  statusLabel:'Z.ai · GLM-5.1',            modelDisplay:'GLM-5.1', keyHint:'Some GLM models are free to use on the API.', keyUrl:'https://z.ai/manage-apikey/apikey-list' },
  mistral:    { label:'Mistral',    model:'mistral-large-latest',        needsKey:true,  statusLabel:'Mistral · Large 3',          modelDisplay:'Mistral Large 3', keyHint:'European provider; free tier for experimentation.', keyUrl:'https://console.mistral.ai/api-keys' },
  xai:        { label:'xAI',        model:'grok-4.3',                    needsKey:true,  statusLabel:'xAI · Grok 4.3',            modelDisplay:'Grok 4.3', keyHint:'Grok models from xAI.', keyUrl:'https://console.x.ai' },
  cohere:     { label:'Cohere',     model:'command-r-plus-08-2024',      needsKey:true,  statusLabel:'Cohere · Command R+',      modelDisplay:'Command R+', keyHint:'Strong on RAG and multilingual. Free trial keys available.', keyUrl:'https://dashboard.cohere.com/api-keys' },
  perplexity: { label:'Perplexity', model:'llama-3.1-sonar-large-128k-online', needsKey:true, statusLabel:'Perplexity · Sonar Large', modelDisplay:'Sonar Large (online)', keyHint:'Online models with built-in web search. API key on Perplexity settings page.', keyUrl:'https://www.perplexity.ai/settings/api' },
  ai21:       { label:'AI21 Labs',  model:'jamba-1.6-large',             needsKey:true,  statusLabel:'AI21 · Jamba 1.6 Large',   modelDisplay:'Jamba 1.6 Large', keyHint:'SSM-Transformer hybrid, very long context. Free tier available.', keyUrl:'https://studio.ai21.com/account/api-key' },
  // ── Fast inference (open models hosted) ──────────────────────────
  groq:       { label:'Groq',       model:'meta-llama/llama-4-maverick-17b-128e-instruct', needsKey:true,  statusLabel:'Groq · Llama 4 Maverick', modelDisplay:'Llama 4 Maverick', keyHint:'Generous free tier, extremely fast responses. Good default for quick tasks.', keyUrl:'https://console.groq.com/keys' },
  cerebras:   { label:'Cerebras',   model:'llama-4-scout-17b-16e-instruct', needsKey:true,  statusLabel:'Cerebras · Llama 4 Scout',  modelDisplay:'Llama 4 Scout', keyHint:'Free tier; the fastest inference speeds available anywhere.', keyUrl:'https://cloud.cerebras.ai' },
  sambanova:  { label:'SambaNova',  model:'Meta-Llama-4-Maverick-17B-128E-Instruct', needsKey:true,  statusLabel:'SambaNova · Llama 4 Maverick', modelDisplay:'Llama 4 Maverick', keyHint:'Free tier with no credit card required.', keyUrl:'https://cloud.sambanova.ai/apis' },
  // ── Open-model hosting / aggregators ─────────────────────────────
  openrouter: { label:'OpenRouter', model:'anthropic/claude-sonnet-4.6', needsKey:true,  statusLabel:'OpenRouter · Claude Sonnet 4.6', modelDisplay:'Claude Sonnet 4.6', keyHint:'One key, access to almost every model. Free credits for new accounts.', keyUrl:'https://openrouter.ai/keys' },
  together:   { label:'Together AI',model:'meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8', needsKey:true, statusLabel:'Together · Llama 4 Maverick', modelDisplay:'Llama 4 Maverick', keyHint:'Wide catalog of open-source models.', keyUrl:'https://api.together.ai/settings/api-keys' },
  fireworks:  { label:'Fireworks',  model:'accounts/fireworks/models/llama4-maverick-instruct-basic', needsKey:true, statusLabel:'Fireworks · Llama 4 Maverick', modelDisplay:'Llama 4 Maverick', keyHint:'Fast hosted inference for open models.', keyUrl:'https://app.fireworks.ai/settings/users/api-keys' },
  novita:     { label:'Novita AI',  model:'meta-llama/llama-3.1-70b-instruct', needsKey:true, statusLabel:'Novita · Llama 3.1 70B', modelDisplay:'Llama 3.1 70B', keyHint:'Cheap hosted open models. Free trial credits for new accounts.', keyUrl:'https://novita.ai/get-key' },
  hyperbolic: { label:'Hyperbolic', model:'meta-llama/Meta-Llama-3.1-70B-Instruct', needsKey:true, statusLabel:'Hyperbolic · Llama 3.1 70B', modelDisplay:'Llama 3.1 70B', keyHint:'Low-cost GPU inference for open models.', keyUrl:'https://app.hyperbolic.xyz/settings' },
  lepton:     { label:'Lepton AI',  model:'llama3-8b',                   needsKey:true,  statusLabel:'Lepton · Llama 3 8B',       modelDisplay:'Llama 3 8B', keyHint:'Serverless open-model inference.', keyUrl:'https://dashboard.lepton.ai/tokens' },
  siliconflow:{ label:'SiliconFlow',model:'Qwen/Qwen2.5-72B-Instruct',   needsKey:true,  statusLabel:'SiliconFlow · Qwen 2.5 72B', modelDisplay:'Qwen 2.5 72B', keyHint:'Chinese aggregator with many open models. Free tier.', keyUrl:'https://cloud.siliconflow.cn/account/ak' },
  friendli:   { label:'Friendli AI',model:'meta-llama-3.1-8b-instruct',  needsKey:true,  statusLabel:'Friendli · Llama 3.1 8B',   modelDisplay:'Llama 3.1 8B', keyHint:'High-throughput inference engine for open models.', keyUrl:'https://friendli.ai/webapp-api-keys' },
  // ── ML platforms / model hubs ────────────────────────────────────
  huggingface:{ label:'Hugging Face', model:'meta-llama/Meta-Llama-3.1-8B-Instruct', needsKey:true, statusLabel:'HF · Llama 3.1 8B', modelDisplay:'Llama 3.1 8B', keyHint:'Inference Endpoints / serverless API. Free tier with HF account.', keyUrl:'https://huggingface.co/settings/tokens' },
  replicate:  { label:'Replicate',  model:'meta/llama-3.1-8b-instruct',  needsKey:true,  statusLabel:'Replicate · Llama 3.1 8B',  modelDisplay:'Llama 3.1 8B', keyHint:'Per-second billing for open models. Free trial credit.', keyUrl:'https://replicate.com/account/api-tokens' },
  // ── Enterprise cloud ─────────────────────────────────────────────
  azure_openai: { label:'Azure OpenAI', model:'gpt-5',                  needsKey:true,  statusLabel:'Azure · GPT-5',            modelDisplay:'Azure GPT-5', keyHint:'Set TERA_PILOT_AZURE_OPENAI_ENDPOINT + TERA_PILOT_AZURE_OPENAI_API_KEY env vars. Your own Azure deployment of OpenAI models.', keyUrl:'https://learn.microsoft.com/azure/ai-services/openai' },
  vertex_ai:    { label:'Vertex AI',    model:'gemini-3.1-pro',         needsKey:true,  statusLabel:'Vertex · Gemini 3.1 Pro',  modelDisplay:'Vertex Gemini 3.1 Pro', keyHint:'Google Cloud Vertex AI. Uses Application Default Credentials (gcloud auth application-default login).', keyUrl:'https://console.cloud.google.com/vertex-ai' },
  bedrock:      { label:'AWS Bedrock',  model:'anthropic.claude-sonnet-5-v1:0', needsKey:true, statusLabel:'Bedrock · Claude Sonnet 5', modelDisplay:'Bedrock Claude Sonnet 5', keyHint:'Set AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY env vars. Uses boto3 client.', keyUrl:'https://console.aws.amazon.com/bedrock' },
  // ── Nvidia NIM ───────────────────────────────────────────────────
  nvidia_nim: { label:'Nvidia NIM', model:'meta/llama-3.1-8b-instruct', needsKey:true, statusLabel:'Nvidia NIM · Llama 3.1 8B', modelDisplay:'NIM Llama 3.1 8B', keyHint:'Get a free key at build.nvidia.com — covers Llama, Mistral, Nemotron hosted by Nvidia.', keyUrl:'https://build.nvidia.com' },
  // ── Generic fallback ─────────────────────────────────────────────
  openai_compat: { label:'OpenAI-compatible (custom)', model:'gpt-3.5-turbo', needsKey:true, statusLabel:'Custom · OpenAI-compat', modelDisplay:'Custom OpenAI-compat', keyHint:'Any endpoint exposing POST /v1/chat/completions. Use the Custom Providers wizard in Settings → Providers to configure base URL + key.', keyUrl:'#' },
};

const DEFAULT_TEMPLATES = [
  { id:'code_project', name:'Code Project', desc:'Scaffold a new project — files, structure, dependencies, tests.', sections:['intent','stack','structure','tests','docs'] },
  { id:'refactor', name:'Refactor', desc:'Reorganize existing code with preserved behaviour.', sections:['scope','before','after','verify'] },
  { id:'feature_spec', name:'Feature Spec', desc:'Define a feature: users, flows, edges, acceptance criteria.', sections:['users','flows','edges','acceptance'] },
  { id:'bug_fix', name:'Bug Fix', desc:'Reproduce, diagnose, patch, regression-test.', sections:['repro','diagnose','patch','regression'] },
  { id:'documentation', name:'Documentation', desc:'Generate README, API reference, architecture notes.', sections:['overview','install','usage','api'] },
  { id:'research', name:'Research', desc:'Investigate a topic, summarize findings, propose next steps.', sections:['question','sources','findings','next'] },
];

const TEMPLATE_PROMPTS = {
  code_project:
    '## Intent\n{{Describe what you want to build}}\n\n'+
    '## Tech Stack\n{{Languages, frameworks, libraries, databases}}\n\n'+
    '## Project Structure\n{{Desired folder/file structure, architecture patterns}}\n\n'+
    '## Tests\n{{Testing strategy, frameworks, coverage requirements}}\n\n'+
    '## Documentation\n{{What documentation to generate}}',
  refactor:
    '## Scope\n{{Which files, modules, or functions to refactor}}\n\n'+
    '## Current Problems\n{{Describe issues, code smells, or pain points}}\n\n'+
    '## Desired Outcome\n{{How the code should look after refactoring}}\n\n'+
    '## Verification\n{{How to verify the refactoring didn\'t break anything}}',
  feature_spec:
    '## Target Users\n{{Who will use this feature?}}\n\n'+
    '## User Flows\n{{Step-by-step flow of how users interact with the feature}}\n\n'+
    '## Edge Cases\n{{Error states, corner cases, unusual inputs}}\n\n'+
    '## Acceptance Criteria\n{{Conditions that must be met for the feature to be complete}}',
  bug_fix:
    '## Reproduction Steps\n{{How to reproduce the bug, step by step}}\n\n'+
    '## Diagnosis\n{{What you think is causing the bug, relevant logs or errors}}\n\n'+
    '## Proposed Fix\n{{Your idea for fixing it, or ask AI to suggest one}}\n\n'+
    '## Regression Test\n{{How to prevent this bug from coming back}}',
  documentation:
    '## Overview\n{{Brief description of the project or module}}\n\n'+
    '## Installation\n{{How to install, configure, and set up}}\n\n'+
    '## Usage\n{{Examples of how to use the main features}}\n\n'+
    '## API Reference\n{{Key functions, classes, endpoints to document}}',
  research:
    '## Question\n{{What specific question or topic are you investigating?}}\n\n'+
    '## Sources\n{{Any existing materials, links, or references to consider}}\n\n'+
    '## Expected Findings\n{{What kind of answer or summary are you looking for?}}\n\n'+
    '## Next Steps\n{{What you plan to do with the research results}}',
};

function insertTemplateIntoComposer(templateId, templateName){
  var promptText = TEMPLATE_PROMPTS[templateId];
  if(!promptText) return;
  // Find first {{...}} placeholder
  var phMatch = promptText.match(/\{\{(.+?)\}\}/);
  if(phMatch){
    var phStart = promptText.indexOf('{{');
    var phEnd = promptText.indexOf('}}', phStart) + 2;
    var before = promptText.slice(0, phStart);
    var hint = phMatch[1];
    var after = promptText.slice(phEnd);
    composerInput.value = before + hint + after;
    composerInput.focus();
    composerInput.setSelectionRange(before.length, before.length + hint.length);
  } else {
    composerInput.value = promptText;
    composerInput.focus();
    composerInput.setSelectionRange(promptText.length, promptText.length);
  }
  composerInput.dispatchEvent(new Event('input'));
  // Show chip for visual reference, but don't set activeTemplate
  // so backend won't add a redundant skeleton system message
  templateChipText.textContent = 'Template \u00b7 ' + templateName;
  templateChip.style.display = 'inline-flex';
  state.activeTemplate = null;
}

const DEFAULT_SKILLS = [
  { id:'python_architect', tag:'architect', name:'Python Architect', desc:'Designs clean package structures, dependency boundaries, layered architecture.' },
  { id:'ui_polish', tag:'frontend', name:'UI Polish', desc:'Pixel-perfect CSS, motion systems, accessibility, responsive behavior.' },
  { id:'security_auditor', tag:'security', name:'Security Auditor', desc:'Threat models, OWASP, secrets hygiene, sandboxing, least privilege.' },
  { id:'performance', tag:'perf', name:'Performance', desc:'Profiles bottlenecks, optimizes hot paths, measures with benchmarks.' },
  { id:'test_engineer', tag:'testing', name:'Test Engineer', desc:'Property tests, fuzzing, fixtures, coverage of edge cases.' },
  { id:'data_engineer', tag:'data', name:'Data Engineer', desc:'Schemas, migrations, idempotent pipelines, observability.' },
  { id:'devops', tag:'devops', name:'DevOps', desc:'CI/CD, IaC, containers, blue-green deploys, incident response.' },
  { id:'tech_writer', tag:'docs', name:'Tech Writer', desc:'Clear prose, diagrams, examples that compile, audience awareness.' },
];

function isBackendAvailable(){return typeof window!=='undefined'&&window.bridge&&window.__teraPilotBridgeConnected}

// v1.1.4-fix: _TeraPilotWebPage.createWindow() returns `self`, so any
// target="_blank" link (chat markdown links, "Get API key" links in
// Settings, etc.) used to navigate the app's own UI away to that page
// instead of opening it externally. Intercept every such click here and
// route it through the OS's default browser via the bridge; fall back
// to window.open in plain-browser/demo mode where target="_blank"
// already works correctly.
document.addEventListener('click', function(e){
  const a = e.target.closest('a[target="_blank"]');
  if(!a || !a.href) return;
  e.preventDefault();
  if(isBackendAvailable()){
    callBridge('open_external_url', a.href).catch(()=>window.open(a.href, '_blank', 'noopener'));
  } else {
    window.open(a.href, '_blank', 'noopener');
  }
});
const VOID_METHODS=new Set(['set_provider','stop_generation','stop_agent']);

// v1.0.6: Agent Mode is now an EXPLICIT toggle (see #agentModeToggle), not an
// auto-detect regex. The old AGENT_PATTERNS heuristic only matched English
// keywords ("write", "save", "file"...), so any non-English prompt — or an
// English prompt phrased slightly differently — silently fell back to plain
// chat mode with zero tools available. That's a language bug and a
// reliability bug at once; a manual switch is unambiguous and can't misfire.
function shouldUseAgentMode(){return !!(state.agentMode && state.projectRoot)}
function callBridge(method,...args){return new Promise((resolve)=>{const bridgeError=(e)=>({ok:false,error:e&&e.message?e.message:String(e)});if(!isBackendAvailable()){resolve({ok:false,error:'Backend not connected'});return}try{if(VOID_METHODS.has(method)){window.bridge[method](...args);setTimeout(()=>resolve({ok:true}),50);return}window.bridge[method](...args,function(result){resolve(result!=null?result:{ok:false,error:'empty bridge result'})})}catch(e){resolve(bridgeError(e))}})}

function toast(msg,kind=''){const el=document.getElementById('toast');el.textContent=msg;el.className='toast show '+kind;clearTimeout(window._toastTimer);window._toastTimer=setTimeout(()=>{el.className='toast '+kind},2800)}

// Neural background — network of nodes, synapses, and traveling pulses
(function(){
  var c=document.getElementById('neural-canvas');
  if(!c)return;
  var ctx=c.getContext('2d');
  var dpr=window.devicePixelRatio||1;
  var w,h,nodes=[],synapses=[],pulses=[];
  var CONN_DIST=140,CORE_PULL=0.3,PULSE_SPEED=0.008;
  var isLightTheme=function(){return document.documentElement.getAttribute('data-theme')==='light'};
  var LIGHT_PULSE_MULT=1.6,LIGHT_SPAWN_RATE=1.5,LIGHT_GLOW_MULT=1.8;

  function resize(){
    w=c.width=window.innerWidth*dpr;
    h=c.height=window.innerHeight*dpr;
    c.style.width=window.innerWidth+'px';
    c.style.height=window.innerHeight+'px';
    build();
  }

  function build(){
    var count=Math.max(30,Math.floor((window.innerWidth*window.innerHeight)/25000));
    nodes=[];
    for(var i=0;i<count;i++){
      var layer=i<count*0.3?'input':i<count*0.7?'hidden':'output';
      nodes.push({
        x:Math.random()*w, y:Math.random()*h,
        vx:(Math.random()-0.5)*0.15, vy:(Math.random()-0.5)*0.15,
        layer:layer, phase:Math.random()*Math.PI*2,
        radius:layer==='hidden'?3.5*dpr:layer==='input'?2.8*dpr:2.5*dpr,
        activation:0.1+Math.random()*0.2
      });
    }
    // Build synapses — connect nearby nodes
    synapses=[];
    var md=CONN_DIST*dpr;
    for(var i=0;i<nodes.length;i++){
      for(var j=i+1;j<nodes.length;j++){
        var dx=nodes[i].x-nodes[j].x, dy=nodes[i].y-nodes[j].y;
        var d=Math.sqrt(dx*dx+dy*dy);
        if(d<md && Math.random()<0.35){
          synapses.push({
            from:i, to:j,
            weight:0.3+Math.random()*0.7,
            delay:0.05+Math.random()*0.15
          });
        }
      }
    }
    pulses=[];
  }

  function tick(time){
    ctx.clearRect(0,0,w,h);
    var t=time*0.001;

    // Update nodes
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i];
      n.x+=n.vx; n.y+=n.vy;
      if(n.x<0||n.x>w)n.vx*=-1;
      if(n.y<0||n.y>h)n.vy*=-1;
      n.activation=0.15+0.85*(0.5+0.5*Math.sin(t*1.8+n.phase));
    }

    // Spawn pulses randomly — higher rate in light theme
    var light=isLightTheme();
    var maxPulses=light?40:25;
    var spawnRate=light?0.06*LIGHT_SPAWN_RATE:0.04;
    if(pulses.length<maxPulses && Math.random()<spawnRate){
      var si=Math.floor(Math.random()*synapses.length);
      pulses.push({si:si, progress:0});
    }

    // Draw synapses
    for(var i=0;i<synapses.length;i++){
      var s=synapses[i];
      var a=nodes[s.from], b=nodes[s.to];
      var act=(a.activation+b.activation)*0.5;
      ctx.strokeStyle='rgba(100,120,160,'+(0.04+act*0.08)+')';
      ctx.lineWidth=0.5*dpr;
      ctx.beginPath();
      ctx.moveTo(a.x,a.y);
      ctx.lineTo(b.x,b.y);
      ctx.stroke();
    }

    // Update & draw pulses
    for(var i=pulses.length-1;i>=0;i--){
      var p=pulses[i];
      p.progress+=PULSE_SPEED*(0.8+synapses[p.si].weight)*(light?LIGHT_PULSE_MULT:1);
      if(p.progress>1){pulses.splice(i,1);continue}
      var s=synapses[p.si];
      var a=nodes[s.from], b=nodes[s.to];
      var px=a.x+(b.x-a.x)*p.progress;
      var py=a.y+(b.y-a.y)*p.progress;
      var glow=1-p.progress;
      var pulseAlpha=glow*0.7*(light?LIGHT_GLOW_MULT:1);
      ctx.fillStyle='rgba(180,100,255,'+Math.min(1,pulseAlpha)+')';
      ctx.beginPath();
      ctx.arc(px,py,(1.5+glow*1.5)*dpr,0,Math.PI*2);
      ctx.fill();
    }

    // Draw nodes
    for(var i=0;i<nodes.length;i++){
      var n=nodes[i];
      var act=n.activation;
      var r=n.radius+act*2*dpr;

      // Glow for active nodes — lower threshold & stronger in light
      var glowThreshold=light?0.4:0.6;
      var glowMult=light?LIGHT_GLOW_MULT:1;
      if(act>glowThreshold){
        ctx.fillStyle='rgba(120,80,220,'+(act*0.12*glowMult)+')';
        ctx.beginPath();
        ctx.arc(n.x,n.y,r+6*dpr,0,Math.PI*2);
        ctx.fill();
      }

      // Node body
      if(n.layer==='hidden'){
        ctx.fillStyle='rgba(140,100,220,'+(0.4+act*0.6)+')';
      }else if(n.layer==='input'){
        ctx.fillStyle='rgba(100,160,220,'+(0.3+act*0.5)+')';
      }else{
        ctx.fillStyle='rgba(100,200,180,'+(0.3+act*0.5)+')';
      }
      ctx.beginPath();
      ctx.arc(n.x,n.y,r,0,Math.PI*2);
      ctx.fill();

      // Ring for high activation
      if(act>0.7){
        ctx.strokeStyle='rgba(180,140,255,'+(act*0.3)+')';
        ctx.lineWidth=0.8*dpr;
        ctx.beginPath();
        ctx.arc(n.x,n.y,r+2*dpr,0,Math.PI*2);
        ctx.stroke();
      }
    }

    requestAnimationFrame(tick);
  }

  resize();
  window.addEventListener('resize',resize);
  requestAnimationFrame(tick);
})();

// DOM refs
const chatView=document.getElementById('chatView');
const emptyState=document.getElementById('emptyState');
const composerInput=document.getElementById('composerInput');
const sendBtn=document.getElementById('sendBtn');
const agentModeToggle=document.getElementById('agentModeToggle');
const agentModeToggleText=document.getElementById('agentModeToggleText');
const chatList=document.getElementById('chatList');
const chatBreadcrumb=document.getElementById('chatBreadcrumb');
const statusbar=document.getElementById('statusbar');
const statusProvider=document.getElementById('statusProvider');
const statusContext=document.getElementById('statusContext');
const activityPanel=document.getElementById('activityPanel');
const activitySteps=document.getElementById('activitySteps');
const composerStatus=document.getElementById('composerStatus');
const profilePlan=document.getElementById('profilePlan');
const templateChip=document.getElementById('templateChip');
const templateChipText=document.getElementById('templateChipText');
const skillChip=document.getElementById('skillChip');
const skillChipText=document.getElementById('skillChipText');

// Provider dropdown
const providerDropdown=document.getElementById('providerDropdown');
const providerTrigger=document.getElementById('providerTrigger');
const providerMenu=document.getElementById('providerMenu');
const providerTriggerText=document.getElementById('providerTriggerText');

providerTrigger.addEventListener('click',(e)=>{e.stopPropagation();providerDropdown.classList.toggle('open');renderProviderMenu()});
document.addEventListener('click',(e)=>{if(!providerDropdown.contains(e.target))providerDropdown.classList.remove('open')});

function renderProviderMenu(){
  providerMenu.innerHTML='';
  const providers=state.providers.length?state.providers:Object.entries(PROVIDER_META).map(([id,m])=>({id,label:m.label,model:m.model,api_key_set:false,active:id===state.activeProvider}));
  for(const p of providers){
    const meta=PROVIDER_META[p.id]||{needsKey:true};
    const item=document.createElement('div');
    item.className='provider-menu-item'+(p.id===state.activeProvider?' active':'');
    item.dataset.provider=p.id;
    item.innerHTML=`
      <div class="pm-dot"></div>
      <div class="pm-info">
        <div class="pm-name">${escapeHtml(p.label)}</div>
        <div class="pm-model">${escapeHtml(p.model||'')}</div>
      </div>
      ${meta.needsKey?`<span class="pm-key-status ${p.api_key_set?'set':'unset'}">${p.api_key_set?'key set':'no key'}</span>`:''}
    `;
    item.addEventListener('click',async()=>{
      state.activeProvider=p.id;
      document.querySelectorAll('.provider-menu-item').forEach(i=>i.classList.remove('active'));
      item.classList.add('active');
      providerTriggerText.textContent=meta.statusLabel;
      statusProvider.textContent=meta.modelDisplay||meta.model;
      profilePlan.textContent=meta.modelDisplay||meta.model;
      providerDropdown.classList.remove('open');
      if(isBackendAvailable()){try{await callBridge('set_provider',p.id);toast(`Switched to ${meta.label}`)}catch(e){toast('Failed: '+e.message,'error')}}
    });
    providerMenu.appendChild(item);
  }
  const divider=document.createElement('div');divider.className='provider-menu-divider';providerMenu.appendChild(divider);
  const footer=document.createElement('div');footer.className='provider-menu-footer';footer.innerHTML='Configure in <a id="pmSettingsLink">Settings → Providers</a>';
  providerMenu.appendChild(footer);
  document.getElementById('pmSettingsLink').addEventListener('click',()=>{providerDropdown.classList.remove('open');openSettings('providers')});
}


/* Model Selector Chip */
var modelSelectorChip = document.getElementById('modelSelectorChip');
var modelSelectorMenu = document.getElementById('modelSelectorMenu');
var msLabel = document.getElementById('msLabel');
var msDot = document.getElementById('msDot');

function updateModelSelectorChip(){
  if(!msLabel) return;
  if(state.autoRoute){ msLabel.textContent = 'Auto'; if(msDot) msDot.className = 'ms-dot auto'; }
  else { var meta = PROVIDER_META[state.activeProvider] || {}; msLabel.textContent = meta.label || state.activeProvider; if(msDot) msDot.className = 'ms-dot connected'; }
}

function renderModelSelectorMenu(){
  if(!modelSelectorMenu) return;
  modelSelectorMenu.innerHTML = '';
  var autoItem = document.createElement('div');
  autoItem.className = 'msm-auto' + (state.autoRoute ? ' active' : '');
  autoItem.innerHTML = '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M7 1.5v2M7 10.5v2M1.5 7h2M10.5 7h2" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><circle cx="7" cy="7" r="2.5" stroke="currentColor" stroke-width="1.3"/></svg><span>Auto-Router</span><span class="msm-router-badge">smart</span>';
  autoItem.addEventListener('click', async function(){
    state.autoRoute = true;
    if(isBackendAvailable()){ try{ await callBridge('toggle_auto_router', true); toast('Auto-router enabled','success'); }catch(e){ toast(e.message,'error'); }}
    modelSelectorMenu.classList.remove('open'); modelSelectorChip.classList.remove('open');
    updateModelSelectorChip(); renderModelSelectorMenu();
  });
  modelSelectorMenu.appendChild(autoItem);
  var divider = document.createElement('div'); divider.className = 'msm-divider'; modelSelectorMenu.appendChild(divider);
  var providers = state.providers.length ? state.providers : Object.entries(PROVIDER_META).map(function(e){return {id:e[0],label:e[1].label,model:e[1].model,api_key_set:false,active:e[0]===state.activeProvider}});
  providers.forEach(function(p){
    var meta = PROVIDER_META[p.id] || {needsKey:true};
    var item = document.createElement('div');
    item.className = 'msm-item' + (p.id===state.activeProvider && !state.autoRoute ? ' active' : '');
    var connected = !meta.needsKey || p.api_key_set;
    item.innerHTML = '<div class="msm-dot"></div><div class="msm-info"><div class="msm-name">'+escapeHtml(p.label)+'</div></div>'+(meta.needsKey?'<span class="msm-status '+(connected?'connected':'disconnected')+'">'+(connected?'ready':'no key')+'</span>':'<span class="msm-status connected">local</span>');
    item.addEventListener('click', async function(){
      state.autoRoute = false; state.activeProvider = p.id;
      if(isBackendAvailable()){ try{ await callBridge('set_provider', p.id); await callBridge('toggle_auto_router', false); toast('Switched to '+meta.label); }catch(e){ toast(e.message,'error'); }}
      providerTriggerText.textContent = meta.statusLabel; statusProvider.textContent = meta.modelDisplay || meta.model;
      modelSelectorMenu.classList.remove('open'); modelSelectorChip.classList.remove('open');
      updateModelSelectorChip(); renderModelSelectorMenu();
    });
    modelSelectorMenu.appendChild(item);
  });
  var footer = document.createElement('div'); footer.className = 'msm-footer';
  footer.innerHTML = 'Configure in <a id="msmSettingsLink">Settings > Providers</a>';
  modelSelectorMenu.appendChild(footer);
  document.getElementById('msmSettingsLink').addEventListener('click', function(){ modelSelectorMenu.classList.remove('open'); modelSelectorChip.classList.remove('open'); openSettings('providers'); });
}

if(modelSelectorChip){
  modelSelectorChip.addEventListener('click', function(e){
    e.stopPropagation(); modelSelectorChip.classList.toggle('open'); modelSelectorMenu.classList.toggle('open');
    if(modelSelectorMenu.classList.contains('open')) renderModelSelectorMenu();
  });
  document.addEventListener('click', function(e){
    if(!modelSelectorMenu.contains(e.target) && e.target !== modelSelectorChip && !modelSelectorChip.contains(e.target)){
      modelSelectorMenu.classList.remove('open'); modelSelectorChip.classList.remove('open');
    }
  });
  updateModelSelectorChip();
}

var _origShowRouterDecision = window.showRouterDecision;
window.showRouterDecision = function(decision){
  if(_origShowRouterDecision) _origShowRouterDecision(decision);
  state.lastRouterDecision = decision;
  if(decision && decision.provider_id && msLabel){ var m = decision.model.split('/').pop(); msLabel.textContent = 'Auto > ' + m; }
  setTimeout(function(){ if(state.autoRoute && msLabel) msLabel.textContent = 'Auto'; }, 12000);
};

// Composer
function autosize(){composerInput.style.height='auto';composerInput.style.height=Math.min(composerInput.scrollHeight,Math.min(200,window.innerHeight*0.4))+'px';updateSendButton()}
function updateSendButton(){
  if(state.isGenerating){sendBtn.disabled=false;sendBtn.classList.add('stop');sendBtn.title='Stop generation';sendBtn.innerHTML='<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><rect x="2" y="2" width="8" height="8" rx="1" fill="currentColor"/></svg>'}
  else{sendBtn.classList.remove('stop');sendBtn.title='Send (⌘ + Enter)';sendBtn.innerHTML='<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 7h9M7 3l4 4-4 4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';sendBtn.disabled=!composerInput.value.trim()}
}
composerInput.addEventListener('input',autosize);
composerInput.addEventListener('keydown',(e)=>{if((e.metaKey||e.ctrlKey)&&e.key==='Enter'){e.preventDefault();if(!sendBtn.disabled)handleSend()}});

// ── Agent Mode toggle (explicit — see note near shouldUseAgentMode) ──
function updateAgentModeToggleUI(){
  if(!agentModeToggle)return;
  const hasProject=!!state.projectRoot;
  agentModeToggle.disabled=!hasProject;
  agentModeToggle.classList.toggle('active',state.agentMode&&hasProject);
  agentModeToggle.setAttribute('aria-pressed',String(state.agentMode&&hasProject));
  if(!hasProject){
    agentModeToggle.title='Agent Mode — open a project first to let Tera Pilot read/write files';
    if(agentModeToggleText)agentModeToggleText.textContent='Agent';
  }else{
    agentModeToggle.title=state.agentMode?'Agent Mode ON — Tera Pilot can read, write, and run files in '+hasProject:'Agent Mode OFF — click to let Tera Pilot read, write, and run files in this project';
    if(agentModeToggleText)agentModeToggleText.textContent=state.agentMode?'Agent: On':'Agent';
  }
}
if(agentModeToggle){
  agentModeToggle.addEventListener('click',()=>{
    if(!state.projectRoot){
      toast('Open a project first — Agent Mode needs a folder to work in.','error');
      return;
    }
    state.agentMode=!state.agentMode;
    localStorage.setItem('tera_pilot:agentMode',state.agentMode?'on':'off');
    updateAgentModeToggleUI();
    toast(state.agentMode?'Agent Mode on — Tera Pilot can now write files, run code, and modify the project.':'Agent Mode off — back to plain chat.','success');
  });
  const savedAgentMode=localStorage.getItem('tera_pilot:agentMode');
  if(savedAgentMode==='on')state.agentMode=true;
  updateAgentModeToggleUI();
}

// ===== Plan Mode =====
(function(){
  var toggle = document.getElementById('planModeToggle');
  var toggleText = document.getElementById('planModeToggleText');
  var panel = document.getElementById('planReviewPanel');
  var backdrop = document.getElementById('planReviewBackdrop');
  var content = document.getElementById('planReviewContent');
  var approveBtn = document.getElementById('planApproveBtn');
  var rejectBtn = document.getElementById('planRejectBtn');
  var editInput = document.getElementById('planEditInput');
  var editSendBtn = document.getElementById('planEditSendBtn');
  
  state.planMode = false;

  if(toggle){
    // Show plan toggle only when agent mode is active and project is open
    toggle.addEventListener('click', function(){
      state.planMode = !state.planMode;
      toggle.setAttribute('aria-pressed', state.planMode);
      toggleText.textContent = state.planMode ? 'Plan ✓' : 'Plan';
      if(state.planMode){
        toast('Plan mode ON — agent will show plan for review before executing', 'info');
      }
    });
  }

  // Show/hide plan toggle based on agent mode
  var agentToggle = document.getElementById('agentModeToggle');
  if(agentToggle){
    // Observe agent mode changes to show/hide plan toggle
    agentToggle.addEventListener('click', function(){
      setTimeout(function(){
        if(toggle) toggle.style.display = (state.agentMode && state.projectRoot) ? 'inline-flex' : 'none';
      }, 50);
    });
  }

  // Plan review panel
  function showPlanReview(planText){
    content.textContent = planText;
    panel.style.display = 'flex';
    backdrop.style.display = 'block';
    editInput.style.display = 'none';
    editSendBtn.style.display = 'none';
  }

  function hidePlanReview(){
    panel.style.display = 'none';
    backdrop.style.display = 'none';
  }

  if(approveBtn) approveBtn.addEventListener('click', function(){
    hidePlanReview();
    state._planApproved = true;
    toast('Plan approved — executing...', 'success');
    // Re-send the stored message with plan_approved flag
    if(state._pendingPlanMessage){
      var msg = state._pendingPlanMessage;
      state._pendingPlanMessage = null;
      msg.plan_approved = true;
      msg.plan_mode = true;  // v1.2.1-fix: явно указываем что это план-мод
      callBridge('send_agent_message', msg);
    }
  });

  if(rejectBtn) rejectBtn.addEventListener('click', function(){
    // Show edit input for feedback
    editInput.style.display = 'block';
    editSendBtn.style.display = 'inline-flex';
    editInput.focus();
  });

  if(editSendBtn) editSendBtn.addEventListener('click', function(){
    var feedback = editInput.value.trim();
    if(!feedback) return;
    hidePlanReview();
    state._planApproved = false;
    // Re-send with plan feedback
    if(state._pendingPlanMessage){
      var msg = state._pendingPlanMessage;
      state._pendingPlanMessage = null;
      msg.plan_feedback = feedback;
      msg.plan_mode = true;  // v1.2.1-fix: явно указываем что это план-мод
      callBridge('send_agent_message', msg);
    }
  });

  if(backdrop) backdrop.addEventListener('click', function(){
    // Don't close on backdrop click — user must approve or reject
  });

  // Hook into the agent step signal to detect plan_created events
  // and show the review panel
  window.__onPlanCreated = function(planText){
    if(state.planMode && !state._planApproved){
      state._pendingPlanText = planText;
      showPlanReview(planText);
    }
  };

  // Expose for integration
  window.__showPlanReview = showPlanReview;
  window.__hidePlanReview = hidePlanReview;
})();

// ===== Verification Loop =====
(function(){
  var verifyEl = document.getElementById('verifyStatus');
  var verifyText = document.getElementById('verifyStatusText');
  var _verifyTimeout = null;

  function setVerifyState(state, text){
    if(!verifyEl) return;
    verifyEl.style.display = 'inline-flex';
    verifyEl.setAttribute('data-state', state);
    verifyText.textContent = text || state;
    clearTimeout(_verifyTimeout);
    if(state === 'passed'){
      _verifyTimeout = setTimeout(function(){
        verifyEl.style.display = 'none';
      }, 5000);
    }
  }

  function hideVerify(){
    if(verifyEl) verifyEl.style.display = 'none';
  }

  // Hook into agent step signal to track verification
  // This gets called from the agent_step_signal handler
  window.__onVerifyEvent = function(step){
    var tool = step.tool || step.name || '';
    var toolLower = tool.toLowerCase();

    if(toolLower === 'self_verify'){
      var status = step.status || 'running';
      if(status === 'running' || status === 'started'){
        setVerifyState('verifying', 'Verifying...');
      } else if(status === 'ok' || status === 'done' || status === 'passed'){
        setVerifyState('passed', 'Verified ✓');
      } else if(status === 'error' || status === 'fail' || status === 'failed'){
        setVerifyState('failed', 'Verify failed ✗');
      }
    }

    // Also detect test running
    if(toolLower === 'execute_command' || toolLower === 'run_code'){
      var args = step.args || {};
      var cmd = (args.command || args.code || '').toLowerCase();
      if(cmd.indexOf('test') !== -1 || cmd.indexOf('pytest') !== -1 || cmd.indexOf('jest') !== -1){
        setVerifyState('running-tests', 'Running tests...');
      }
    }
  };

  // Reset verification when agent starts/stops
  window.__onVerifyReset = function(){
    hideVerify();
  };

  // Expose
  window.__setVerifyState = setVerifyState;
})();

// v1.0.7: classify_intent was removed from the bridge (agent is always on).
// This stub is kept for backward-compat but always returns "action".
async function checkAgentIntent(text){ return "action"; }

async function handleSend(){
  let text=composerInput.value.trim();if(!text)return;
  if(state.isGenerating){
    state.isGenerating=false;updateSendButton();composerStatus.textContent='Stopped';hideActivity();
    // v1.0.5-security: in agent mode, call stop_agent (cancels the agent
    // worker thread). Previously the Stop button only called
    // stop_generation, which doesn't touch the agent worker — so the
    // agent kept running and modifying files after the user pressed Stop
    // (BUGS_REPORT H-API-1, regression of the original C1 fix).
    if(isBackendAvailable()){
      if(state.projectRoot){
        // Agent mode: cancel the agent worker thread (bridge path).
        callBridge('stop_agent').catch(function(){});
        // Also cancel any in-flight chat-stream worker, just in case.
        callBridge('stop_generation').catch(function(){});
        // v1.1.1: also cancel via HTTP — the agent may be running via
        // /api/agent/stream (HTTP path) which has its own cancel flag
        // on ServerContext. stop_agent (bridge) only cancels the
        // AgentWorker QThread, which doesn't exist for the HTTP path.
        // Calling both ensures Stop works regardless of which transport
        // the send went through.
        if(window.__apiBase){
          fetch(window.__apiBase+'/api/agent/stop',{
            method:'POST',
            headers:_apiHeaders()
          }).catch(function(){});
        }
      }else{
        callBridge('stop_generation').catch(function(){});
      }
    }
    const msgs=chatView.querySelectorAll('.msg');const last=msgs[msgs.length-1];if(last){last.querySelector('.msg-body').classList.remove('stream-cursor')}
    return;
  }

  // v1.0.9: slash commands — /context, /clear, /compact
  // These are handled locally (no LLM call) and show their result
  // directly in the chat as a system message.
  if(text.startsWith('/')){
    var cmd = text.split(/\s+/)[0].toLowerCase();
    var handled = await handleSlashCommand(cmd, text);
    if(handled){
      composerInput.value='';autosize();
      return;
    }
    // Unknown slash command — fall through to agent as a normal message
  }

  // Prepend context if loaded
  let sendText=text;
  if(activeContextText){sendText='--- CONTEXT ---\n'+activeContextText+'\n--- END CONTEXT ---\n\n'+text}
  appendMessage('user',text);composerInput.value='';autosize();
  const assistantEl=appendMessage('assistant','');assistantEl.querySelector('.msg-body').classList.add('stream-cursor');

  // v1.0.6: show a VISIBLE badge in the assistant message header so
  // the user can tell at a glance whether this response went through
  // the agent runtime (with tools) or plain chat (no tools). Without
  // this, when the silent classifier-downgrade bug fired, the user
  // saw a "I can't write files" response and couldn't tell if Agent
  // Mode was actually used. Now: if the response is from the agent,
  // a small "AGENT" pill appears next to the "TERA_PILOT" role label.
  // If it's plain chat, a "CHAT" pill appears instead.
  var agentBadge = assistantEl.querySelector('.msg-agent-badge');
  if(!agentBadge){
    agentBadge = document.createElement('span');
    agentBadge.className = 'msg-agent-badge';
    agentBadge.style.cssText = 'font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;margin-left:6px;text-transform:uppercase;letter-spacing:0.05em;font-family:JetBrains Mono,monospace';
    var headerSpan = assistantEl.querySelector('.msg-role.assistant');
    if(headerSpan){ headerSpan.parentNode.insertBefore(agentBadge, headerSpan.nextSibling); }
  }
  // Default to CHAT; will be flipped to AGENT below if useAgent is true.

  state.isGenerating=true;updateSendButton();composerStatus.textContent='Generating\u2026';showActivity();if(window.__onVerifyReset)window.__onVerifyReset();

  // v1.0.7: AGENT MODE IS ALWAYS ON. No toggle, no classifier, no
  // word-count heuristic. The whole point of Tera Pilot is "give the AI a
  // task and it does everything itself" — so every message goes
  // through the agent runtime (planning → tools → verification →
  // final answer). If the user just asks a question, the agent will
  // plan, see no tools are needed, and answer directly.
  //
  // The only exception: if there's no project open, the agent has
  // nowhere to read/write files, so we fall back to plain chat AND
  // tell the user — they need to open a project first.
  var useAgent = true;
  if(!state.projectRoot){
    useAgent = false;
    toast('Open a project first (⌘O) — agent needs a workspace to read/write files.','warning');
    composerStatus.textContent = 'No project — using chat mode. Open a project to enable agent tools.';
  }else{
    composerStatus.textContent = 'Agent mode — planning…';
  }

  // v1.0.6: visible AGENT/CHAT badge in the assistant message header
  // so the user can tell at a glance whether this response went
  // through the agent runtime.
  if(agentBadge){
    if(useAgent){
      agentBadge.textContent = 'AGENT';
      agentBadge.style.background = 'var(--accent-dim, rgba(244,185,66,0.15))';
      agentBadge.style.color = 'var(--accent, #F4B942)';
      agentBadge.style.border = '1px solid var(--accent, #F4B942)';
    }else{
      agentBadge.textContent = 'CHAT';
      agentBadge.style.background = 'var(--bg-floating, #17181A)';
      agentBadge.style.color = 'var(--text-muted, #6D7078)';
      agentBadge.style.border = '1px solid var(--border, #2A2B2E)';
    }
  }

  // Try the local HTTP API first; if it fails, fall back to the QWebChannel bridge.
  // v1.1.5-fix (tera_pilot_bug_report.md bug #5): track whether at least one SSE
  // event has already been processed. If the connection breaks AFTER that
  // (e.g. mid-stream token delivery, or after the `chat_info` event that
  // created a new chat), blindly falling back to the Qt bridge would
  // RE-SEND the same message — producing a duplicate chat / duplicated
  // tokens. We only fall back when the failure happened BEFORE any event
  // was received (i.e. the connection never got going); otherwise we
  // surface the error and let the user retry manually.
  if(window.__apiBase){
    var streamStarted=false;
    try{
      var endpoint=useAgent?'/api/agent/stream':'/api/chat/stream';
      const resp=await fetch(window.__apiBase+endpoint,{method:'POST',headers:_apiHeaders(),body:JSON.stringify({text:sendText,chat_id:state.activeChatId,project_root:state.projectRoot})});
      if(!resp.ok){const err=await resp.json().catch(()=>({}));throw new Error(err.error||'HTTP '+resp.status)}
      const reader=resp.body.getReader();const decoder=new TextDecoder();let buffer='';
      while(true){const{done,value}=await reader.read();if(done)break;buffer+=decoder.decode(value,{stream:true});
        const lines=buffer.split('\n');buffer=lines.pop()||'';
        for(const line of lines){if(!line.startsWith('data: '))continue;
          try{const data=JSON.parse(line.slice(6));
            // v1.1.5-fix (bug #5): mark stream as started so a mid-flight
            // connection break does NOT trigger a duplicate send via the
            // Qt bridge fallback. Any successfully parsed SSE event counts
            // — chat_info, token, step, done, etc. — because each of them
            // either created state we can't safely re-create (a new chat)
            // or already painted content into the DOM (tokens/steps).
            streamStarted=true;
            if(data.type==='chat_info'){state.activeChatId=data.chat_id;chatBreadcrumb.textContent=data.title||'New chat';debouncedRefreshChatList()}
            else if(data.type==='router_decision'){showRouterDecision(data)}
            else if(data.type==='token'){streamToken(data.content)}
            else if(data.type==='step'){
              addActivityStep(data);
              if(data.label) composerStatus.textContent = data.label;
              // v1.0.8: stream thought/plan/tool activity into the chat body
              // via the same appendAgentText path used by the Qt bridge.
              if(data.detail === 'thought' && data.thought){
                appendAgentText(data.thought);
              } else if(data.detail === 'plan_created' && data.plan){
                appendAgentText('## Plan\n' + data.plan);
              } else if(data.detail === 'tool_called' && data.tool){
                var toolLine = '→ ' + data.tool;
                if(data.args && data.args.path) toolLine += ' ' + data.args.path;
                if(data.write_intent) toolLine = '[WRITE_FILE] ' + data.write_intent + '\n' + toolLine;
                appendAgentText(toolLine);
              } else if(data.detail === 'tool_result' && data.tool){
                appendAgentText('  ✓ ' + data.tool + ' done');
              }
              if(data.detail === 'tool_result' || data.tool){ refreshFileTree(); }
            }
            else if(data.type==='done'){finalizeMessage(data);debouncedRefreshChatList();hideActivity();if(useAgent&&state.projectRoot)refreshFileTree()}
            else if(data.type==='diff_review'){
              // v1.1.1: agent paused for diff review (HTTP path)
              // v1.0.5-security: capture the per-request review_id so the
              // accept/reject POST goes to the right agent stream
              // (BUGS_REPORT C-API-2 — concurrent agent diff reviews were
              // previously routed to whichever thread happened to be waiting).
              if (data.review_id){ window.__pendingReviewId = data.review_id; }
              // Show the same diff modal as the Qt bridge path
              showDiffReview(data);
              // Override the modal buttons to POST to the HTTP endpoint
              var dm=document.getElementById('diffModal');if(dm){
                var applyBtn=document.getElementById('diffApply');
                var rejectBtn=document.getElementById('diffReject');
                if(applyBtn){
                  var newApply=applyBtn.cloneNode(true);applyBtn.parentNode.replaceChild(newApply,applyBtn);
                  newApply.addEventListener('click',async()=>{
                    dm.style.display='none';
                    try{await fetch(window.__apiBase+'/api/agent/diff_review',{method:'POST',headers:_apiHeaders(),body:JSON.stringify({accepted:true,review_id:window.__pendingReviewId})})}catch(e){}
                    toast('Change applied','success');
                  });
                }
                if(rejectBtn){
                  var newReject=rejectBtn.cloneNode(true);rejectBtn.parentNode.replaceChild(newReject,rejectBtn);
                  newReject.addEventListener('click',async()=>{
                    dm.style.display='none';
                    try{await fetch(window.__apiBase+'/api/agent/diff_review',{method:'POST',headers:_apiHeaders(),body:JSON.stringify({accepted:false,review_id:window.__pendingReviewId})})}catch(e){}
                    toast('Change rejected','warning');
                  });
                }
              }
              composerStatus.textContent='Waiting for diff review…';
            }
            else if(data.type==='error'){assistantEl.querySelector('.msg-body').textContent='Error: '+data.message;toast(data.message,'error');state.isGenerating=false;updateSendButton();composerStatus.textContent='Ready';assistantEl.querySelector('.msg-body').classList.remove('stream-cursor');hideActivity();if(window.__activateNeuralPixels)window.__activateNeuralPixels(false);if(window.__activateSynapse)window.__activateSynapse(false)}
          }catch(e){}}}
    }catch(fetchErr){
      // v1.1.5-fix (tera_pilot_bug_report.md bug #5): only fall back to the Qt
      // bridge if the failure happened BEFORE any SSE event was received.
      // If the stream already started (chat was created, tokens were
      // painted, etc.), re-sending via the bridge would duplicate the
      // message — so we surface the error and reset UI state instead,
      // letting the user retry manually.
      if(!streamStarted && isBackendAvailable()){
        console.warn('[tera_pilot] HTTP API fetch failed before stream start, falling back to bridge:',fetchErr.message);
        // Fallback: use the QWebChannel bridge (always available when running inside Tera Pilot)
        try{
          var bridgeMethod=useAgent&&state.projectRoot?'send_agent_message':'send_message';
          var bridgeOpts={text:sendText,chat_id:state.activeChatId};
          bridgeOpts.skill=state.activeSkill;if(bridgeMethod==='send_message'){bridgeOpts.template=state.activeTemplate}
          const result=await callBridge(bridgeMethod,bridgeOpts);
          if(result.ok){
            state.activeChatId=result.chat_id;chatBreadcrumb.textContent=result.title||'New chat';debouncedRefreshChatList();if(useAgent&&state.projectRoot)refreshFileTree();
            // v1.1.5-fix (tera_pilot_bug_report.md bug #6): finalizeMessage is
            // the ONLY place that resets state.isGenerating=false,
            // re-enables the send button, and sets status back to
            // 'Ready'. Without this call the UI would hang in
            // 'Generating…' forever after a successful bridge fallback.
            finalizeMessage(result);
          }
          else{assistantEl.querySelector('.msg-body').textContent='Error: '+(result.error||'unknown');toast(result.error||'Failed to send','error')}
        }catch(bridgeErr){assistantEl.querySelector('.msg-body').textContent='Error: '+bridgeErr.message;toast(bridgeErr.message,'error')}
      }else if(streamStarted){
        // Stream broke mid-flight — DO NOT re-send (would duplicate).
        console.warn('[tera_pilot] HTTP stream broke mid-flight — NOT re-sending to avoid duplicate:',fetchErr.message);
        var bodyEl=assistantEl.querySelector('.msg-body');
        if(bodyEl){
          var cur=bodyEl.textContent||'';
          bodyEl.textContent=cur+'\n\n[stream interrupted: '+fetchErr.message+']';
          bodyEl.classList.remove('stream-cursor');
        }
        toast('Stream interrupted — please retry manually','warning');
        state.isGenerating=false;
        updateSendButton();
        composerStatus.textContent='Ready';
        hideActivity();
        if(window.__activateNeuralPixels)window.__activateNeuralPixels(false);
        if(window.__activateSynapse)window.__activateSynapse(false);
      }else{
        // No stream started AND no bridge available — show error and reset.
        assistantEl.querySelector('.msg-body').textContent='Error: '+fetchErr.message;toast(fetchErr.message,'error');state.isGenerating=false;updateSendButton();composerStatus.textContent='Ready';assistantEl.querySelector('.msg-body').classList.remove('stream-cursor');hideActivity()
      }
    }
  }else if(isBackendAvailable()){
    try{
      var bridgeMethod=useAgent&&state.projectRoot?'send_agent_message':'send_message';
      var bridgeOpts={text:sendText,chat_id:state.activeChatId};
      bridgeOpts.skill=state.activeSkill;if(bridgeMethod==='send_message'){bridgeOpts.template=state.activeTemplate}
      const result=await callBridge(bridgeMethod,bridgeOpts);
      if(result.ok){
        state.activeChatId=result.chat_id;chatBreadcrumb.textContent=result.title||'New chat';debouncedRefreshChatList();if(useAgent&&state.projectRoot)refreshFileTree();
        // v1.1.5-fix (tera_pilot_bug_report.md bug #6): same rationale as the
        // HTTP-fallback branch above — without finalizeMessage the UI
        // would stay stuck in 'Generating…' even though the bridge
        // successfully delivered the message.
        finalizeMessage(result);
      }
      else{assistantEl.querySelector('.msg-body').textContent='Error: '+(result.error||'unknown');toast(result.error||'Failed to send','error')}
    }catch(e){assistantEl.querySelector('.msg-body').textContent='Error: '+e.message;toast(e.message,'error')}
  }else{
    setTimeout(()=>{const tokens='Backend not connected. Running in demo mode \u2014 open this HTML inside Tera Pilot to use real providers.'.split(' ');let i=0;const stream=()=>{if(i>=tokens.length){state.isGenerating=false;updateSendButton();composerStatus.textContent='Ready';assistantEl.querySelector('.msg-body').classList.remove('stream-cursor');return}const body=assistantEl.querySelector('.msg-body');body.textContent+=(i===0?'':' ')+tokens[i++];requestAnimationFrame(stream)};stream()},300)
  }
}
sendBtn.addEventListener('click',handleSend);

// v1.0.9: Slash commands — /context, /clear, /compact, /help
// These are handled locally without sending to the LLM. The result
// is shown as a system message in the chat.
async function handleSlashCommand(cmd, fullText){
  if(!isBackendAvailable()){
    toast('Slash commands need the Tera Pilot backend','error');
    return true;
  }
  try{
    if(cmd === '/context' || cmd === '/ctx'){
      var status = await callBridge('get_context_status');
      showContextStatus(status);
      return true;
    }
    if(cmd === '/clear'){
      var result = await callBridge('clear_context');
      appendMessage('assistant', '**Context cleared.** Starting fresh — previous conversation history removed from the agent\'s memory.\n\n*TERA_PILOT.md project instructions are preserved.*');
      toast('Context cleared','success');
      return true;
    }
    if(cmd === '/compact'){
      appendMessage('assistant', '*Compacting context…*');
      var compactResult = await callBridge('compact_context');
      if(compactResult.ok){
        var msg = '**Context compacted.**\n';
        msg += '- Summary: ' + (compactResult.summary_chars || 0) + ' chars\n';
        msg += '- Kept recent messages: ' + (compactResult.kept_messages || 0) + '\n\n';
        msg += 'The agent now has a summary of the conversation plus the most recent messages. Early details are preserved in the summary.';
        // Replace the "Compacting…" message
        var msgs = chatView.querySelectorAll('.msg');
        var last = msgs[msgs.length-1];
        if(last){ last.querySelector('.msg-body').innerHTML = renderMarkdown(msg); }
        toast('Context compacted','success');
      } else {
        toast('Compaction failed: ' + (compactResult.error || 'unknown'),'error');
      }
      return true;
    }
    if(cmd === '/pin' || cmd === '/unpin'){
      var argPath = fullText.slice(cmd.length).trim();
      if(!argPath){
        appendMessage('assistant', 'Usage: `' + cmd + ' path/to/file.py` (path relative to the project root).');
        return true;
      }
      var pinResult = await callBridge(cmd === '/pin' ? 'pin_context_file' : 'unpin_context_file', argPath);
      if(pinResult && pinResult.ok){
        appendMessage('assistant', (cmd === '/pin' ? '📌 Pinned' : 'Unpinned') + ' `' + argPath + '` — ' +
          (cmd === '/pin' ? "it will always be auto-attached to the agent's context." : 'it will only be attached when relevant.'));
        toast(cmd === '/pin' ? 'File pinned' : 'File unpinned', 'success');
      } else {
        toast('Command failed','error');
      }
      return true;
    }
    if(cmd === '/help'){
      showSlashHelp();
      return true;
    }
    if(cmd === '/reload-context' || cmd === '/reload-tera-pilot-md' || cmd === '/reload-claude-md'){
      var reload = await callBridge('reload_project_context');
      if(reload.ok){
        appendMessage('assistant', '**Project context reloaded.**\n\nSources: ' + (reload.sources || []).join(', ') + '\nTotal: ' + (reload.total_chars || 0) + ' chars');
        toast('TERA_PILOT.md reloaded','success');
      }
      return true;
    }
    // Unknown slash command — not handled, fall through to agent
    return false;
  }catch(e){
    console.error('[tera_pilot] slash command error:', e);
    toast('Command failed: ' + e.message,'error');
    return true;
  }
}

function showContextStatus(status){
  var lines = ['## Context Status\n'];
  // Memory
  var mem = status.memory || {};
  lines.push('### Conversation Memory');
  lines.push('- Messages: ' + (mem.message_count || 0) + ' / ' + (mem.max_messages || 0));
  lines.push('- Tokens: ~' + (mem.total_tokens || 0) + ' / ' + (mem.max_tokens || 0) +
             ' (' + Math.round((mem.utilization || 0) * 100) + '% utilized)');
  if(mem.compaction_summary_chars > 0){
    lines.push('- Compaction summary: ' + mem.compaction_summary_chars + ' chars');
  }
  lines.push('');
  // Project context
  var pc = status.project_context || {};
  lines.push('### Project Instructions (TERA_PILOT.md)');
  if(pc.sources && pc.sources.length > 0){
    lines.push('- Sources: ' + pc.sources.join(', '));
    lines.push('- Total: ' + (pc.total_chars || 0) + ' chars');
  } else {
    lines.push('- No TERA_PILOT.md found. Create one at the project root to add persistent project rules.');
    lines.push('  (CLAUDE.md is also accepted for compatibility.)');
  }
  lines.push('');
  // v1.1.4-fix (bug 4.2): auto-attached project files (ContextManager)
  var files = status.files || null;
  lines.push('### Auto-Attached Files');
  if(files && files.total_indexed > 0){
    lines.push('- Indexed: ' + files.total_indexed + ' files in project');
    lines.push('- Attached: ' + (files.files ? files.files.length : 0) + ' files, ~' +
               (files.total_tokens || 0) + ' / ' + (files.budget || 0) +
               ' tokens (' + (files.utilization_pct || 0) + '%)');
    if(files.files && files.files.length > 0){
      files.files.slice(0, 10).forEach(function(f){
        lines.push('  - ' + f.path + ' (~' + f.approx_tokens + ' tok, ' + f.reason + ')');
      });
      if(files.files.length > 10) lines.push('  - …and ' + (files.files.length - 10) + ' more');
    }
  } else {
    lines.push('- No project indexed yet — open a project folder first.');
  }
  lines.push('');
  // System prompt
  lines.push('### System Prompt');
  lines.push('- Size: ' + (status.system_prompt_chars || 0) + ' chars (~' + (status.system_prompt_tokens || 0) + ' tokens)');
  lines.push('');
  lines.push('---');
  lines.push('*Use `/clear` to wipe conversation memory, `/compact` to summarise old messages, `/pin <path>` to always attach a file, or edit TERA_PILOT.md and run `/reload-context`.*');
  appendMessage('assistant', lines.join('\n'));
}

function showSlashHelp(){
  var help = '## Slash Commands\n\n' +
    '- `/context` — show what\'s in the context window (messages, tokens, TERA_PILOT.md)\n' +
    '- `/clear` — wipe conversation memory (start fresh). TERA_PILOT.md is preserved.\n' +
    '- `/compact` — summarise old messages to free up context space\n' +
    '- `/reload-context` — re-read TERA_PILOT.md after editing it\n' +
    '- `/pin <path>` — always auto-attach a file to the agent\'s context\n' +
    '- `/unpin <path>` — stop always-attaching a file\n' +
    '- `/help` — show this help\n\n' +
    '*Project instructions file: `TERA_PILOT.md` at the project root.*\n' +
    '*Fallback: `CLAUDE.md` for compatibility with existing memory files.*\n\n' +
    '*Any other `/xxx` is sent to the agent as a normal message.*';
  appendMessage('assistant', help);
}

function appendMessage(role,content){
  // v1.3.1 fix: `emptyState` above is captured once at page load and goes
  // stale the moment emptyStateMarkup() is re-rendered into chatView for a
  // new/switched chat (a brand-new #emptyState node replaces it). Hiding the
  // stale reference left the fresh "new chat" heading + suggestions visible
  // and pinned above the real conversation. Query live and remove for good.
  const liveEmptyState=chatView.querySelector('#emptyState, .empty-state');
  if(liveEmptyState)liveEmptyState.remove();
  else if(emptyState)emptyState.style.display='none';
  const msg=document.createElement('div');msg.className='msg '+role;
  const now=new Date();const time=now.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  const rendered = role==='assistant' ? renderMarkdown(content) : escapeHtml(content);
  // v1.1.1: Revert/Explain sit next to Copy, assistant messages only —
  // "Revert" undoes the whole agent run this message belongs to (that's
  // the granularity the undo mechanism actually supports; true per-tool-
  // step revert would need per-step file snapshots, which aren't kept
  // today), "Explain" asks the agent to walk through what it just did.
  const extraBtns = role==='assistant'
    ? '<button class="revert-msg-btn msg-action-btn" title="Undo the changes from this response">Revert</button>'
      +'<button class="explain-msg-btn msg-action-btn" title="Ask the agent to explain this response">Explain</button>'
    : '';
  msg.innerHTML=`<div class="msg-header"><div class="msg-avatar">${role==='user'?'U':'T'}</div><span class="msg-role ${role}">${role==='user'?'You':'Tera Pilot'}</span><span class="msg-time">${time}</span><div class="msg-actions">${extraBtns}<button class="copy-msg-btn msg-action-btn" title="Copy message">Copy</button></div></div><div class="msg-body ${role}">${rendered}</div><div class="msg-meta"></div>`;
  var copyBtn=msg.querySelector('.copy-msg-btn');
  copyBtn.addEventListener('click',function(e){e.stopPropagation();navigator.clipboard.writeText(content).then(function(){copyBtn.textContent='Copied!';setTimeout(function(){copyBtn.textContent='Copy'},1500)}).catch(function(){})});
  var revertBtn=msg.querySelector('.revert-msg-btn');
  if(revertBtn)revertBtn.addEventListener('click',async function(e){
    e.stopPropagation();
    if(!isBackendAvailable()){toast('Backend not connected','error');return}
    if(!confirm('Revert the changes made in this response? This uses the same undo as the global Undo button — it reverts the whole agent run, not a single tool call.'))return;
    try{const r=await callBridge('undo_last_agent');if(r.ok){toast('Reverted ('+r.method+')','success');refreshFileTree()}else{toast(r.error||'Nothing to revert','error')}}catch(err){toast(err.message,'error')}
  });
  var explainBtn=msg.querySelector('.explain-msg-btn');
  if(explainBtn)explainBtn.addEventListener('click',function(e){
    e.stopPropagation();
    composerInput.value='Explain what you just did in your last response, step by step, and why.';
    autosize();composerInput.focus();
  });
  msg.addEventListener('mouseenter',function(){copyBtn.style.opacity='1';if(revertBtn)revertBtn.style.opacity='1';if(explainBtn)explainBtn.style.opacity='1'});
  msg.addEventListener('mouseleave',function(){copyBtn.style.opacity='0';if(revertBtn)revertBtn.style.opacity='0';if(explainBtn)explainBtn.style.opacity='0'});
  chatView.appendChild(msg);chatView.scrollTop=chatView.scrollHeight;return msg;
}

// v1.0.2: Markdown renderer with Apply/Copy buttons on code blocks
function renderMarkdown(text){
  if(!text) return '';
  // 1) Extract fenced code blocks BEFORE escapeHtml so newlines are preserved
  const codeBlocks=[];
  let h=text.replace(/```(\w*)\n([\s\S]*?)```/g,function(match,lang,code){
    const idx=codeBlocks.length;
    const langLabel=lang||'code';
    const id='cb_'+Math.random().toString(36).slice(2,8);
    codeBlocks.push({id,lang:langLabel,code:escapeHtml(code)});
    return '\x00CB'+idx+'\x00';
  });
  // 2) Extract inline code BEFORE escapeHtml
  const inlineCodes=[];
  h=h.replace(/`([^`]+)`/g,function(m,code){
    const idx=inlineCodes.length;
    inlineCodes.push(escapeHtml(code));
    return '\x00IC'+idx+'\x00';
  });
  // 3) Escape remaining text
  h=escapeHtml(h);
  // 4) Restore code blocks as HTML
  for(let i=0;i<codeBlocks.length;i++){
    const{ id, lang, code }=codeBlocks[i];
    h=h.replace('\x00CB'+i+'\x00','<div style="position:relative;margin:var(--s-8) 0"><div style="display:flex;align-items:center;justify-content:space-between;padding:4px 12px;background:var(--bg-floating);border:1px solid var(--border);border-bottom:none;border-radius:var(--r-sm) var(--r-sm) 0 0;font-size:10px;font-family:JetBrains Mono,monospace;color:var(--text-muted)"><span>'+escapeHtml(lang)+'</span><div style="display:flex;gap:4px"><button class="apply-btn" data-codeblock="'+id+'" title="Apply this code to the file">Apply</button><button class="apply-btn" data-copyblock="'+id+'" title="Copy to clipboard">Copy</button></div></div><pre id="'+id+'" style="margin:0;border-radius:0 0 var(--r-sm) var(--r-sm)"><code>'+code+'</code></pre></div>');
  }
  // 5) Restore inline code
  for(let i=0;i<inlineCodes.length;i++){
    h=h.replace('\x00IC'+i+'\x00','<code class="inline-code">'+inlineCodes[i]+'</code>');
  }
  // 6) Headings (## H2, ### H3, #### H4) — must run before bold/italic, line-based
  h=h.replace(/^#### (.+)$/gm,'<h4>$1</h4>');
  h=h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h=h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/^# (.+)$/gm,'<h2>$1</h2>');
  // 6b) Blockquotes
  h=h.replace(/^&gt; (.+)$/gm,'<blockquote>$1</blockquote>');
  h=h.replace(/<\/blockquote>\n<blockquote>/g,'\n');
  // 6c) Horizontal rule
  h=h.replace(/^---+$/gm,'<hr>');
  // 6d) Bold + Italic + Links
  h=h.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>');
  h=h.replace(/\*([^*]+)\*/g,'<i>$1</i>');
  h=h.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  // 6e) Lists — group consecutive bullet/numbered lines into <ul>/<ol>
  {
    const lines=h.split('\n');
    const out=[];
    let mode=null; // 'ul' | 'ol' | null
    for(const line of lines){
      const bullet=line.match(/^\s*[-*•]\s+(.*)$/);
      const numbered=line.match(/^\s*\d+[.)]\s+(.*)$/);
      if(bullet){
        if(mode!=='ul'){ if(mode)out.push('</'+mode+'>'); out.push('<ul>'); mode='ul'; }
        out.push('<li>'+bullet[1]+'</li>');
      }else if(numbered){
        if(mode!=='ol'){ if(mode)out.push('</'+mode+'>'); out.push('<ol>'); mode='ol'; }
        out.push('<li>'+numbered[1]+'</li>');
      }else{
        if(mode){ out.push('</'+mode+'>'); mode=null; }
        out.push(line);
      }
    }
    if(mode)out.push('</'+mode+'>');
    h=out.join('\n');
  }
  // 7) Paragraphs / line breaks (skip block-level elements already produced)
  h=h.replace(/\n\n/g,'</p><p>');
  h=h.replace(/\n/g,'<br>');
  h='<p>'+h+'</p>';
  h=h.replace(/<p>\s*<\/p>/g,'');
  h=h.replace(/<p>\s*(<div style)/g,'$1');
  h=h.replace(/(<\/div>)\s*<\/p>/g,'$1');
  // Unwrap paragraphs that only wrap block-level elements (headings, lists, hr, blockquote)
  h=h.replace(/<p>\s*(<(?:h2|h3|h4|ul|ol|blockquote|hr)[^>]*>)/g,'$1');
  h=h.replace(/(<\/(?:h2|h3|h4|ul|ol|blockquote)>|<hr>)\s*<\/p>/g,'$1');
  h=h.replace(/<br>\s*(<(?:h2|h3|h4|ul|ol|li|blockquote|hr)[^>]*>)/g,'$1');
  h=h.replace(/(<\/(?:h2|h3|h4|ul|ol|li|blockquote)>|<hr>)\s*<br>/g,'$1');
  return h;
}
function escapeHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
// v1.1.1: colored file-type badges for the file browser — was a plain
// flat list where every file looked identical regardless of extension.
// Short letter + a per-language color, VSCode-style (without pulling in
// an icon font/spritesheet).
const FILE_TYPE_META = {
  py:{label:'PY',color:'#3B82F6'}, js:{label:'JS',color:'#F7DF1E'}, jsx:{label:'JS',color:'#F7DF1E'},
  ts:{label:'TS',color:'#3178C6'}, tsx:{label:'TS',color:'#3178C6'}, css:{label:'#',color:'#8B5CF6'},
  html:{label:'<>',color:'#E34C26'}, json:{label:'{}',color:'#F59E0B'}, md:{label:'MD',color:'#93C5FD'},
  toml:{label:'TM',color:'#9CA3AF'}, yml:{label:'YM',color:'#9CA3AF'}, yaml:{label:'YM',color:'#9CA3AF'},
  txt:{label:'TX',color:'#9CA3AF'}, sh:{label:'SH',color:'#4ADE80'}, sql:{label:'DB',color:'#22D3EE'},
  png:{label:'IMG',color:'#F472B6'}, jpg:{label:'IMG',color:'#F472B6'}, jpeg:{label:'IMG',color:'#F472B6'},
  svg:{label:'IMG',color:'#F472B6'}, gif:{label:'IMG',color:'#F472B6'},
};
function fileTypeBadge(name){
  const ext=(name.split('.').pop()||'').toLowerCase();
  const meta=FILE_TYPE_META[ext]||{label:(ext.slice(0,2)||'\u2022').toUpperCase(),color:'var(--text-muted)'};
  return `<span class="file-type-badge" style="color:${meta.color};border-color:${meta.color}66">${escapeHtml(meta.label)}</span>`;
}
function streamToken(token){const msgs=chatView.querySelectorAll('.msg');const last=msgs[msgs.length-1];if(!last)return;const body=last.querySelector('.msg-body');body._rawText=(body._rawText||'')+token;body.innerHTML=renderMarkdown(body._rawText);chatView.scrollTop=chatView.scrollHeight}
// v1.0.8: append a line of agent reasoning/tool activity to the current
// assistant message body. This runs ALONGSIDE the activity panel — the
// panel shows compact step labels, this function shows the full thought
// text in the chat itself, so the user sees what the agent is thinking
// without having to open the activity panel.
// Lines are separated by blank lines so markdown renders them as
// distinct paragraphs. Tool-call lines are styled as inline code.
function appendAgentText(text){
  if(!text) return;
  const msgs=chatView.querySelectorAll('.msg');
  const last=msgs[msgs.length-1];
  if(!last) return;
  const body=last.querySelector('.msg-body');
  if(!body) return;
  // Mark that we have provisional agent text — finalizeMessage will
  // REPLACE this with the real final_answer when the agent finishes.
  body._isAgentStream = true;
  const line = String(text).trim();
  if(!line) return;
  // Build the accumulated text. If the body already has provisional
  // agent text, append; otherwise start fresh.
  if(!body._agentText){
    body._agentText = '';
  }
  body._agentText += line + '\n\n';
  // Render: prefer the agent text buffer over the raw token buffer,
  // because the agent doesn't stream tokens — it streams thoughts.
  body._rawText = body._agentText;
  body.innerHTML = renderMarkdown(body._rawText);
  chatView.scrollTop = chatView.scrollHeight;
}

// v1.1.1: interactive plan/step rendering. Instead of flattening every
// thought/tool-call/tool-result into one plain-text blob (which becomes an
// unreadable wall of text on longer tasks), each assistant message keeps
// an ordered `_blocks` array of {type:'text',...} and {type:'step',...}
// entries. Steps render as collapsible cards with a live status
// (running → done/failed) and a duration timer, addressing the "plan
// view is just a flat list with no visibility into where the agent is
// stuck" feedback.
function isErrorResult(r){
  if(!r) return false;
  return /^\s*\[(REJECTED|SECURITY ERROR|TOOL ERROR|FILE NOT FOUND|COMMAND ERROR|CANCELLED|DIFF ERROR|GIT ERROR|REFUSED|TIMEOUT|SEARCH ERROR|LIST ERROR)/i.test(r);
}
function stepIcon(status){
  if(status==='running')return '<span class="agent-step-spinner"></span>';
  if(status==='done')return '<span class="agent-step-check">\u2713</span>';
  if(status==='failed')return '<span class="agent-step-x">\u2715</span>';
  return '<span class="agent-step-pending">\u25cb</span>';
}
function renderStepCard(b){
  const dur = b.endTime ? (((b.endTime-b.startTime)/1000).toFixed(1)+'s') : '';
  let argsPreview='';
  if(b.writeIntent){argsPreview=escapeHtml(b.writeIntent)}
  else if(b.args&&b.args.path){argsPreview=escapeHtml(String(b.args.path))}
  else if(b.args&&b.args.command){argsPreview=escapeHtml(String(b.args.command))}
  else if(b.args&&Object.keys(b.args).length){argsPreview=escapeHtml(JSON.stringify(b.args).slice(0,80))}
  return `<div class="agent-step agent-step-${b.status}" data-step-id="${b.id}">`
    +`<div class="agent-step-head">`
      +`<span class="agent-step-status">${stepIcon(b.status)}</span>`
      +`<span class="agent-step-tool">${escapeHtml(b.tool)}</span>`
      +(argsPreview?`<span class="agent-step-args">${argsPreview}</span>`:'')
      +`<span class="agent-step-dur">${dur}</span>`
      +`<span class="agent-step-chevron">\u25b8</span>`
    +`</div>`
    +`<div class="agent-step-detail" style="display:none"><pre class="agent-step-result">${escapeHtml(b.result||'(no output yet)')}</pre></div>`
  +`</div>`;
}
function renderBlocks(msgEl){
  const body=msgEl.querySelector('.msg-body');
  if(!body||!body._blocks)return;
  let html='';
  for(const b of body._blocks){
    if(b.type==='text')html+=renderMarkdown(b.text);
    else if(b.type==='step')html+=renderStepCard(b);
    else if(b.type==='divider')html+='<hr>';
  }
  body.innerHTML=html;
  body.querySelectorAll('.agent-step-head').forEach(head=>{
    head.addEventListener('click',()=>{
      const detail=head.nextElementSibling;
      const chevron=head.querySelector('.agent-step-chevron');
      const open=detail.style.display!=='none';
      detail.style.display=open?'none':'block';
      if(chevron)chevron.textContent=open?'\u25b8':'\u25be';
    });
  });
  wireCodeButtons(msgEl);
  chatView.scrollTop=chatView.scrollHeight;
}
function pushAgentBlock(msgEl,block){
  const body=msgEl.querySelector('.msg-body');
  if(!body)return;
  body._isAgentStream=true;
  if(!body._blocks)body._blocks=[];
  body._blocks.push(block);
  renderBlocks(msgEl);
}

function finalizeMessage(result){
  const msgs=chatView.querySelectorAll('.msg');const last=msgs[msgs.length-1];if(!last)return;
  const body=last.querySelector('.msg-body');body.classList.remove('stream-cursor');
  // v1.1.1: if we streamed structured blocks (thoughts/steps), append the
  // final answer as one more text block rather than re-flattening
  // everything back into a single string — keeps the step cards intact
  // instead of collapsing them back into plain text.
  // v1.1.3-fix: matches the appendAgentText revert above — append the
  // final answer to the plain-text buffer instead of the _blocks system.
  if(body._isAgentStream && body._agentText){
    var finalText = (result && result.text) ? result.text : '';
    if(finalText){
      body._agentText += '---\n\n' + finalText;
    }
    body._rawText = body._agentText;
    body.innerHTML = renderMarkdown(body._rawText);
    wireCodeButtons(last);
  } else if(result && result.text){
    body._rawText = result.text;
    body.innerHTML = renderMarkdown(body._rawText);
    wireCodeButtons(last);
  }
  const meta=last.querySelector('.msg-meta');
  if(result){
    const parts=[];
    if(result.tokens)parts.push(result.tokens+' tokens');
    if(result.elapsed)parts.push(result.elapsed.toFixed(1)+'s');
    if(result.cancelled)parts.push('cancelled');
    if(result.router)parts.push('router: '+(result.router.tier||'auto'));
    if(result.context)parts.push('ctx: '+result.context.files+' files');
    meta.textContent=parts.join(' \u00b7 ');
    // Update session token bar
    state.sessionTokens+=result.tokens||0;
    state.sessionRequests++;
    var costPerToken=0;
    var p=PROVIDER_META[state.activeProvider];
    if(p){var costs={groq:0.00008,openai:0.00003,anthropic:0.000015,deepseek:0.00001,zai:0.00002,gemini:0.00003,mistral:0.00003,openrouter:0.00003,together:0.00004};costPerToken=costs[state.activeProvider]||0.00003}
    state.sessionCost+=costPerToken*(result.tokens||0);
    updateSessionTokenBar();
  }
  state.isGenerating=false;updateSendButton();composerStatus.textContent='Ready';
  if(window.__activateNeuralPixels)window.__activateNeuralPixels(false);
  if(window.__activateSynapse)window.__activateSynapse(false);
}

async function loadChat(chatId){
  state.activeChatId=chatId;chatView.innerHTML='';
  if(isBackendAvailable()){
    try{
      const result=await callBridge('load_chat',chatId);
      if(result && result.ok && result.chat){
        chatBreadcrumb.textContent=result.chat.title||'Chat';
        const msgs=result.chat.messages||[];
        for(const m of msgs){
          const el=appendMessage(m.role,m.content);
          if(m.tokens||m.elapsed){
            const meta=el.querySelector('.msg-meta');
            const parts=[];
            if(m.tokens)parts.push(`${m.tokens} tokens`);
            if(m.elapsed)parts.push(`${m.elapsed.toFixed(1)}s`);
            if(m.cancelled)parts.push('cancelled');
            if(meta)meta.textContent=parts.join(' · ');
          }
        }
        if(msgs.length===0){chatView.innerHTML=emptyStateMarkup();bindEmptyStateSuggestions()}
      } else if(result && result.error){
        toast('Failed to load chat: '+result.error,'error');
        chatView.innerHTML=emptyStateMarkup();bindEmptyStateSuggestions();
      } else {
        // Backend returned nothing useful — show empty state instead of crashing
        chatView.innerHTML=emptyStateMarkup();bindEmptyStateSuggestions();
      }
    }catch(e){
      toast('Failed to load chat: '+(e&&e.message?e.message:String(e)),'error');
      chatView.innerHTML=emptyStateMarkup();
      bindEmptyStateSuggestions();
    }
  }
  document.querySelectorAll('.chat-item').forEach(c=>c.classList.remove('active'));
  const item=document.querySelector(`.chat-item[data-id="${chatId}"]`);if(item)item.classList.add('active');
}

let _newChatLock=false;
async function newChat(){
  if(_newChatLock)return;_newChatLock=true;
  try{
  state.activeChatId=null;
  chatView.innerHTML=emptyStateMarkup();
  chatBreadcrumb.textContent='New chat';composerInput.focus();
  document.querySelectorAll('.chat-item').forEach(c=>c.classList.remove('active'));
  bindEmptyStateSuggestions();
  // Don't pre-create chat — it will be auto-created on first send_message
  }finally{_newChatLock=false}
}

function hasAnyConfiguredProvider(){
  if(!state.providers||state.providers.length===0)return false;
  return state.providers.some(function(p){
    const meta=PROVIDER_META[p.id]||{};
    return p.api_key_set || meta.needsKey===false;
  });
}

function emptyStateMarkup(){
  // v1.1.4: if nobody has set up a provider yet, the person would
  // otherwise type a message and get a confusing "no providers
  // available" failure with no clue what to do next. Greet them with
  // the fix instead.
  const onboarding = (!isBackendAvailable() || hasAnyConfiguredProvider()) ? '' :
    '<div class="empty-state-onboarding" style="margin:0 auto var(--s-16);max-width:420px;padding:var(--s-12) var(--s-16);border:1px solid var(--border);border-radius:var(--r-md);background:var(--bg-hover);font-size:13px;text-align:left">'+
      '<strong>No AI provider set up yet.</strong> Add a free API key (Groq and Cerebras are fast and free to start) '+
      'or use a local model with LM Studio / Ollama — no key needed.'+
      '<div style="margin-top:8px"><button class="btn-primary" id="onboardingOpenSettings" style="font-size:12px">Set up a provider →</button></div>'+
    '</div>';
  return '<div class="empty-state" id="emptyState">'+
    '<div class="empty-state-glyph" aria-hidden="true"><span class="eg-prompt">&gt;</span><span class="eg-cursor"></span></div>'+
    '<h2>What are we building today?</h2>'+
    '<p>Describe a task in natural language. Tera Pilot structures your prompt, attaches a skill, and streams the response from your chosen provider.</p>'+
    onboarding+
    '<div class="empty-state-suggestions" id="emptyStateSuggestions">'+
      '<button class="es-chip" data-prompt="Explain what this codebase does and how it is structured">Explain this codebase</button>'+
      '<button class="es-chip" data-prompt="Find and fix a bug in ">Fix a bug</button>'+
      '<button class="es-chip" data-prompt="Write unit tests for ">Write tests</button>'+
      '<button class="es-chip" data-prompt="Refactor this for clarity and performance: ">Refactor code</button>'+
    '</div>'+
  '</div>';
}

function bindEmptyStateSuggestions(){
  const wrap=document.getElementById('emptyStateSuggestions');
  const onbBtn=document.getElementById('onboardingOpenSettings');
  if(onbBtn)onbBtn.addEventListener('click',()=>openSettings('providers'));
  if(!wrap)return;
  wrap.querySelectorAll('.es-chip').forEach(chip=>{
    chip.addEventListener('click',()=>{
      composerInput.value=chip.dataset.prompt||'';
      composerInput.focus();
      const len=composerInput.value.length;
      composerInput.setSelectionRange(len,len);
      composerInput.dispatchEvent(new Event('input'));
    });
  });
}
document.getElementById('newChatBtn').addEventListener('click',newChat);
// Sidebar collapse toggle
document.getElementById('sidebarToggle').addEventListener('click',()=>{document.querySelector('.app').classList.toggle('sidebar-collapsed')});
document.addEventListener('keydown',(e)=>{if((e.metaKey||e.ctrlKey)&&e.key==='b'){e.preventDefault();document.querySelector('.app').classList.toggle('sidebar-collapsed')}});

let _refreshTimer=null;
function debouncedRefreshChatList(){if(_refreshTimer)clearTimeout(_refreshTimer);_refreshTimer=setTimeout(refreshChatList,350)}
function formatChatTime(dateStr){
  if(!dateStr)return'';
  var d=new Date(dateStr);if(isNaN(d))return'';
  var now=new Date();
  var sameDay=d.toDateString()===now.toDateString();
  if(sameDay)return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  var yest=new Date(now);yest.setDate(now.getDate()-1);
  if(d.toDateString()===yest.toDateString())return'Yesterday';
  var sameYear=d.getFullYear()===now.getFullYear();
  return d.toLocaleDateString([],sameYear?{month:'short',day:'numeric'}:{month:'short',day:'numeric',year:'numeric'});
}
function chatDateGroup(dateStr){
  if(!dateStr)return'Older';
  var d=new Date(dateStr);if(isNaN(d))return'Older';
  var now=new Date();
  var startOfToday=new Date(now.getFullYear(),now.getMonth(),now.getDate());
  var startOfDate=new Date(d.getFullYear(),d.getMonth(),d.getDate());
  var diffDays=Math.round((startOfToday-startOfDate)/86400000);
  if(diffDays<=0)return'Today';
  if(diffDays===1)return'Yesterday';
  if(diffDays<=7)return'Previous 7 Days';
  if(diffDays<=30)return'Previous 30 Days';
  return'Older';
}
function renderChatRow(c,opts){
  opts=opts||{};
  var el=document.createElement('div');
  el.className='chat-item'+(opts.compact?' compact':'')+(c.id===state.activeChatId?' active':'');
  el.dataset.id=c.id;
  if(opts.compact){
    const statusDot='<span class="chat-status-dot chat-status-'+(c.status||'idle')+'" title="'+(c.status||'idle')+'"></span>';
    el.innerHTML=statusDot+'<svg class="chat-item-icon" width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M2 3h12v8H5l-3 3V3Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg><div class="chat-item-title">'+escapeHtml(c.title)+'</div><button class="chat-item-del" data-del="'+c.id+'" title="Delete"><svg width="8" height="8" viewBox="0 0 8 8" fill="none"><path d="M1 1l6 6M7 1l-6 6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></button>';
  }else{
    var time=formatChatTime(c.updated_at);
    var preview='';if(c.messages&&c.messages.length>0){var lm=c.messages[c.messages.length-1];preview=(lm.content||'').slice(0,60).replace(/\n/g,' ');}
    el.innerHTML='<div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:3px"><div class="chat-item-title">'+escapeHtml(c.title)+'</div><div class="chat-item-preview">'+escapeHtml(preview)+'</div></div><div style="display:flex;flex-direction:column;align-items:flex-end;gap:5px"><span class="chat-item-time">'+time+'</span><span class="chat-tag">'+c.message_count+' msgs</span></div><button class="chat-item-del" data-del="'+c.id+'" title="Delete"><svg width="8" height="8" viewBox="0 0 8 8" fill="none"><path d="M1 1l6 6M7 1l-6 6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></button>';
  }
  el.addEventListener('click',function(e){
    if(e.target.closest('[data-del]')){e.stopPropagation();deleteChat(c.id);return}
    loadChat(c.id);
    if(opts.closeModalOnClick)closeAllChats();
  });
  return el;
}
async function refreshChatList(){
  chatList.innerHTML='';if(!isBackendAvailable())return;
  try{
    const chats=await callBridge('list_chats');state.chats=chats;
    chatList.classList.add('anim-stagger');
    var q=(document.getElementById('chatSearchInput')||{}).value||'';
    q=q.trim().toLowerCase();
    var visible=q?chats.filter(function(c){return(c.title||'').toLowerCase().indexOf(q)!==-1}):chats;
    if(state.projectRoot){ var hdr=document.createElement('div');hdr.className='chat-group-header';hdr.textContent='Current Project';chatList.appendChild(hdr); }
    var COMPACT_LIMIT=6;
    var shown=q?visible:visible.slice(0,COMPACT_LIMIT);
    var lastGroup=null;
    shown.forEach(function(c){
      if(!q&&!state.projectRoot){
        var g=chatDateGroup(c.updated_at);
        if(g!==lastGroup){
          var gh=document.createElement('div');gh.className='chat-group-header';gh.textContent=g;chatList.appendChild(gh);
          lastGroup=g;
        }
      }
      chatList.appendChild(renderChatRow(c,{compact:true}));
    });
    if(q&&visible.length===0){
      var empty=document.createElement('div');empty.className='chat-search-empty';empty.textContent='No chats match "'+escapeHtml(q)+'"';chatList.appendChild(empty);
    }
    var allChatsBtnEl=document.getElementById('allChatsBtn');var allChatsCountEl=document.getElementById('allChatsCount');
    if(allChatsCountEl)allChatsCountEl.textContent=chats.length;
    if(allChatsBtnEl)allChatsBtnEl.style.display=(!q&&chats.length>0)?'':'none';
    var catalogCountEl=document.getElementById('catalogTriggerCount');
    if(catalogCountEl)catalogCountEl.textContent=chats.length;
    var catalogBtnEl=document.getElementById('catalogBtn');
    if(catalogBtnEl)catalogBtnEl.dataset.count=String(chats.length);
    if(document.getElementById('allChatsModal').classList.contains('open'))renderAllChatsModal();
  }catch(e){console.error('[tera_pilot] refreshChatList failed:',e)}
}
(function(){
  var input=document.getElementById('chatSearchInput');
  if(!input)return;
  var debounceId=null;
  input.addEventListener('input',function(){
    clearTimeout(debounceId);
    debounceId=setTimeout(refreshChatList,120);
  });
})();
function renderAllChatsModal(){
  // Catalog mode: render the full chat catalog with projects, tags, sorting
  Catalog.render();
}
function openAllChats(){
  var modal=document.getElementById('allChatsModal');
  modal.classList.add('catalog-mode');
  modal.style.maxWidth='';  // let .catalog-mode CSS take over
  document.getElementById('allChatsModalTitle').textContent='Chat Catalog';
  renderAllChatsModal();
  modal.classList.add('open');
  document.getElementById('allChatsBackdrop').classList.add('open');
}
function closeAllChats(){
  var modal=document.getElementById('allChatsModal');
  modal.classList.remove('open');
  modal.classList.remove('catalog-mode');
  modal.style.maxWidth='min(620px,92vw)';  // restore default
  document.getElementById('allChatsModalTitle').textContent='All Chats';
  document.getElementById('allChatsBackdrop').classList.remove('open');
}
(function(){
  var btn=document.getElementById('allChatsBtn');if(btn)btn.addEventListener('click',openAllChats);
  var bd=document.getElementById('allChatsBackdrop');if(bd)bd.addEventListener('click',closeAllChats);
  var cl=document.getElementById('allChatsClose');if(cl)cl.addEventListener('click',closeAllChats);
  var catBtn=document.getElementById('catalogBtn');
  if(catBtn)catBtn.addEventListener('click',openAllChats);
})();

/* ===================================================================
   CHAT CATALOG — projects, tags, sorting (frontend-only, localStorage)
   =================================================================== */
var Catalog = (function(){
  var PROJECTS_KEY = 'tera_pilot:projects';
  var TAGS_KEY     = 'tera_pilot:chatTags';
  var ASSIGN_KEY   = 'tera_pilot:chatProject';   // chatId -> projectId

  var PROJECT_COLORS = ['#D97757','#5B8DEF','#8FBC8F','#C4B5FD','#F4B942','#E57373','#A88FCC','#7BAED4','#E5B567','#8B5E3C'];
  var DEFAULT_COLOR  = '#D97757';

  // ── State ───────────────────────────────────────────────────────
  var filter = {
    search: '',
    sort: 'date',          // date | tags | project
    activeProject: 'all'   // 'all' | 'unassigned' | projectId
  };

  // ── Persistence helpers ─────────────────────────────────────────
  function loadProjects(){
    try{ var raw = localStorage.getItem(PROJECTS_KEY); return raw ? JSON.parse(raw) : []; }
    catch(e){ return []; }
  }
  function saveProjects(arr){
    try{ localStorage.setItem(PROJECTS_KEY, JSON.stringify(arr)); }catch(e){}
  }
  function loadTags(){
    try{ var raw = localStorage.getItem(TAGS_KEY); return raw ? JSON.parse(raw) : {}; }
    catch(e){ return {}; }
  }
  function saveTags(obj){
    try{ localStorage.setItem(TAGS_KEY, JSON.stringify(obj)); }catch(e){}
  }
  function loadAssignments(){
    try{ var raw = localStorage.getItem(ASSIGN_KEY); return raw ? JSON.parse(raw) : {}; }
    catch(e){ return {}; }
  }
  function saveAssignments(obj){
    try{ localStorage.setItem(ASSIGN_KEY, JSON.stringify(obj)); }catch(e){}
  }

  function getProjects(){ return loadProjects(); }
  function getTags(){ return loadTags(); }
  function getAssignments(){ return loadAssignments(); }

  function getTagsFor(chatId){ var t = getTags(); return t[chatId] || []; }
  function setTagsFor(chatId, arr){
    var t = getTags();
    t[chatId] = arr.slice(0, 12);
    if(t[chatId].length === 0) delete t[chatId];
    saveTags(t);
  }
  function addTag(chatId, tag){
    tag = (tag || '').trim().toLowerCase();
    if(!tag) return;
    var arr = getTagsFor(chatId);
    if(arr.indexOf(tag) !== -1) return;
    arr.push(tag);
    setTagsFor(chatId, arr);
  }
  function removeTag(chatId, tag){
    var arr = getTagsFor(chatId).filter(function(t){ return t !== tag; });
    setTagsFor(chatId, arr);
  }

  function getProjectFor(chatId){
    var a = getAssignments();
    return a[chatId] || null;
  }
  function setProjectFor(chatId, projectId){
    var a = getAssignments();
    if(projectId) a[chatId] = projectId;
    else delete a[chatId];
    saveAssignments(a);
  }

  function createProject(name, color){
    var arr = getProjects();
    var p = {
      id: 'p_' + Date.now().toString(36) + Math.random().toString(36).slice(2,5),
      name: name.trim().slice(0, 60),
      color: color || DEFAULT_COLOR,
      created_at: new Date().toISOString()
    };
    arr.push(p);
    saveProjects(arr);
    return p;
  }
  function deleteProject(id){
    var arr = getProjects().filter(function(p){ return p.id !== id; });
    saveProjects(arr);
    // Unassign all chats that were in this project
    var a = getAssignments();
    Object.keys(a).forEach(function(cid){
      if(a[cid] === id) delete a[cid];
    });
    saveAssignments(a);
  }
  function findProject(id){
    return getProjects().filter(function(p){ return p.id === id; })[0] || null;
  }

  // ── Rendering ───────────────────────────────────────────────────
  function getFilteredChats(){
    var chats = (state.chats || []).slice();
    var q = filter.search.toLowerCase().trim();
    var tags = getTags();
    var assigns = getAssignments();

    // Filter by project
    if(filter.activeProject === 'unassigned'){
      chats = chats.filter(function(c){ return !assigns[c.id]; });
    } else if(filter.activeProject !== 'all'){
      chats = chats.filter(function(c){ return assigns[c.id] === filter.activeProject; });
    }

    // Filter by search query (title + tags + preview)
    if(q){
      chats = chats.filter(function(c){
        var title = (c.title || '').toLowerCase();
        var preview = '';
        if(c.messages && c.messages.length){
          preview = (c.messages[c.messages.length-1].content || '').toLowerCase();
        }
        var chatTags = (tags[c.id] || []).join(' ').toLowerCase();
        return title.indexOf(q) !== -1 || preview.indexOf(q) !== -1 || chatTags.indexOf(q) !== -1;
      });
    }

    // Sort
    if(filter.sort === 'date'){
      chats.sort(function(a,b){
        var da = new Date(a.updated_at || 0).getTime();
        var db = new Date(b.updated_at || 0).getTime();
        return db - da;
      });
    } else if(filter.sort === 'tags'){
      // Chats with tags first, then by tag name alphabetically, then by date
      chats.sort(function(a,b){
        var ta = (tags[a.id] || []).slice().sort().join(',');
        var tb = (tags[b.id] || []).slice().sort().join(',');
        if(!ta && !tb){
          var da = new Date(a.updated_at || 0).getTime();
          var db = new Date(b.updated_at || 0).getTime();
          return db - da;
        }
        if(!ta) return 1;
        if(!tb) return -1;
        return ta < tb ? -1 : ta > tb ? 1 : 0;
      });
    } else if(filter.sort === 'project'){
      // Group by project (unassigned last), then by date within group
      chats.sort(function(a,b){
        var pa = assigns[a.id] || '~~unassigned';
        var pb = assigns[b.id] || '~~unassigned';
        if(pa !== pb) return pa < pb ? -1 : 1;
        var da = new Date(a.updated_at || 0).getTime();
        var db = new Date(b.updated_at || 0).getTime();
        return db - da;
      });
    }
    return chats;
  }

  function escapeHtml(s){
    if(s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function(ch){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch];
    });
  }

  function renderSidebar(){
    var projects = getProjects();
    var assigns = getAssignments();
    var chats = state.chats || [];
    var counts = {};
    chats.forEach(function(c){
      var pid = assigns[c.id];
      if(pid) counts[pid] = (counts[pid] || 0) + 1;
    });
    var unassignedCount = chats.filter(function(c){ return !assigns[c.id]; }).length;

    var html = '';
    html += '<div class="catalog-sidebar-section">Filters <span class="cs-count">'+chats.length+'</span></div>';
    html += '<div class="catalog-project-item'+(filter.activeProject==='all'?' active':'')+'" data-pid="all">';
    html += '<div class="catalog-project-dot" style="background:var(--text-secondary)"></div>';
    html += '<div class="catalog-project-name">All chats</div>';
    html += '<div class="catalog-project-count">'+chats.length+'</div>';
    html += '</div>';
    html += '<div class="catalog-project-item'+(filter.activeProject==='unassigned'?' active':'')+'" data-pid="unassigned">';
    html += '<div class="catalog-project-dot" style="background:var(--text-muted);opacity:0.5"></div>';
    html += '<div class="catalog-project-name">Unassigned</div>';
    html += '<div class="catalog-project-count">'+unassignedCount+'</div>';
    html += '</div>';

    if(projects.length){
      html += '<div class="catalog-sidebar-section">Projects <span class="cs-count">'+projects.length+'</span></div>';
      projects.forEach(function(p){
        var c = counts[p.id] || 0;
        html += '<div class="catalog-project-item'+(filter.activeProject===p.id?' active':'')+'" data-pid="'+escapeHtml(p.id)+'">';
        html += '<div class="catalog-project-dot" style="background:'+escapeHtml(p.color)+'"></div>';
        html += '<div class="catalog-project-name">'+escapeHtml(p.name)+'</div>';
        html += '<div class="catalog-project-count">'+c+'</div>';
        html += '<button class="catalog-project-del" data-del-project="'+escapeHtml(p.id)+'" title="Delete project"><svg width="8" height="8" viewBox="0 0 8 8" fill="none"><path d="M1 1l6 6M7 1l-6 6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></button>';
        html += '</div>';
      });
    }

    html += '<button class="catalog-new-project" id="catalogNewProjectBtn">';
    html += '<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><path d="M7 2v10M2 7h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
    html += 'New project</button>';
    return html;
  }

  function renderToolbar(resultCount){
    var html = '<div class="catalog-search">';
    html += '<span class="catalog-search-icon"><svg width="14" height="14" viewBox="0 0 16 16" fill="none"><circle cx="7" cy="7" r="5" stroke="currentColor" stroke-width="1.5"/><path d="M11 11l3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg></span>';
    html += '<input id="catalogSearchInput" type="text" placeholder="Search chats, tags…" value="'+escapeHtml(filter.search)+'">';
    html += '</div>';
    html += '<div class="catalog-sort">';
    html += '<button class="catalog-sort-btn'+(filter.sort==='date'?' active':'')+'" data-sort="date">Date</button>';
    html += '<button class="catalog-sort-btn'+(filter.sort==='tags'?' active':'')+'" data-sort="tags">Tags</button>';
    html += '<button class="catalog-sort-btn'+(filter.sort==='project'?' active':'')+'" data-sort="project">Project</button>';
    html += '</div>';
    html += '<div class="catalog-results-info">'+resultCount+' shown</div>';
    return html;
  }

  function renderCard(c){
    var tags = getTagsFor(c.id);
    var pid  = getProjectFor(c.id);
    var proj = pid ? findProject(pid) : null;
    var date = formatChatTime(c.updated_at);
    var title = (c.title || '').trim() || 'Untitled chat';
    var preview = '';
    if(c.messages && c.messages.length){
      preview = (c.messages[c.messages.length-1].content || '').slice(0, 160).replace(/\n/g,' ').trim();
    }

    var html = '<div class="catalog-card'+(proj?' has-project':'')+'" data-id="'+escapeHtml(c.id)+'"';
    if(proj) html += ' style="--project-color:'+escapeHtml(proj.color)+'"';
    html += '>';

    // Actions (assign / delete) — revealed on row hover
    html += '<div class="catalog-card-actions">';
    html += '<button class="catalog-action-btn" data-action="assign" title="Assign to project"><svg width="11" height="11" viewBox="0 0 16 16" fill="none"><path d="M2 4h4l1.5 1.5H14V13H2V4Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg></button>';
    html += '<button class="catalog-action-btn danger" data-action="delete" title="Delete chat"><svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M3 4.5h10M6.2 4.5V3.1c0-.6.5-1.1 1.1-1.1h1.4c.6 0 1.1.5 1.1 1.1v1.4M4.4 4.5l.6 8.4c0 .6.5 1.1 1.1 1.1h3.8c.6 0 1.1-.5 1.1-1.1l.6-8.4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/><path d="M6.4 7.2v4M9.6 7.2v4" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/></svg></button>';
    html += '</div>';

    // Leading icon
    html += '<div class="catalog-card-icon"><svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M2 4.6C2 3.2 3.2 2 4.6 2h5.8C11.8 2 13 3.2 13 4.6v3.3c0 1.4-1.2 2.6-2.6 2.6H7.3L4.4 13v-2.5h-.1C3.1 10.5 2 9.3 2 7.9V4.6Z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg></div>';

    // Main body: title/date row, preview, tags
    html += '<div class="catalog-card-body">';
    html += '<div class="catalog-card-header">';
    html += '<div class="catalog-card-title">'+escapeHtml(title)+'</div>';
    html += '<div class="catalog-card-date">'+escapeHtml(date||'—')+'</div>';
    html += '</div>';
    html += '<div class="catalog-card-preview'+(preview?'':' empty')+'">'+escapeHtml(preview || 'No messages yet')+'</div>';
    html += '<div class="catalog-card-tags">';
    tags.forEach(function(t){
      html += '<span class="catalog-tag" data-tag="'+escapeHtml(t)+'">'+escapeHtml(t);
      html += '<span class="catalog-tag-x" data-tag-x="'+escapeHtml(t)+'" title="Remove tag"><svg width="7" height="7" viewBox="0 0 8 8" fill="none"><path d="M1 1l6 6M7 1l-6 6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg></span>';
      html += '</span>';
    });
    html += '<button class="catalog-add-tag" data-action="add-tag">+ tag</button>';
    html += '</div>';
    html += '</div>';

    // Trailing meta: message count + project
    html += '<div class="catalog-card-footer">';
    html += '<div class="catalog-card-meta"><span>'+(c.message_count || (c.messages?c.messages.length:0) || 0)+' msgs</span></div>';
    if(proj){
      html += '<div class="catalog-card-project" title="'+escapeHtml(proj.name)+'">';
      html += '<div class="catalog-project-dot" style="background:'+escapeHtml(proj.color)+'"></div>';
      html += '<span>'+escapeHtml(proj.name)+'</span>';
      html += '</div>';
    }
    html += '</div>';

    html += '</div>';
    return html;
  }

  function renderGrid(){
    var chats = getFilteredChats();
    if(chats.length === 0){
      var icon = '<svg class="catalog-empty-icon" viewBox="0 0 24 24" fill="none"><path d="M2 3h5v5H2V3ZM9 3h5v5H9V3ZM2 9h5v5H2V9ZM9 9h5v5H9V9Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
      var isFiltered = filter.search || filter.activeProject !== 'all';
      var title = isFiltered ? 'No chats match your filter' : 'No chats yet';
      var desc  = isFiltered ? 'Try a different search or filter.' : 'Start a new chat — it will appear here in your catalog.';
      return '<div class="catalog-empty">'+icon+'<div class="catalog-empty-title">'+title+'</div><div class="catalog-empty-desc">'+desc+'</div></div>';
    }
    return chats.map(renderCard).join('');
  }

  function render(){
    var body = document.getElementById('allChatsBody');
    if(!body) return;
    var chats = getFilteredChats();
    var html = '<div class="catalog-layout">';
    html += '<aside class="catalog-sidebar" id="catalogSidebar">'+renderSidebar()+'</aside>';
    html += '<div class="catalog-main">';
    html += renderToolbar(chats.length);
    html += '<div class="catalog-grid" id="catalogGrid">'+renderGrid()+'</div>';
    html += '</div>';
    html += '</div>';
    body.innerHTML = html;
    wireEvents();
  }

  function refreshGridOnly(){
    var grid = document.getElementById('catalogGrid');
    if(!grid) return;
    grid.innerHTML = renderGrid();
    var info = document.querySelector('.catalog-results-info');
    if(info){
      var chats = getFilteredChats();
      info.textContent = chats.length + ' shown';
    }
    wireCardEvents();
  }

  function refreshSidebarOnly(){
    var sb = document.getElementById('catalogSidebar');
    if(!sb) return;
    sb.innerHTML = renderSidebar();
    wireSidebarEvents();
  }

  // ── Event wiring ────────────────────────────────────────────────
  function wireEvents(){
    wireSidebarEvents();
    wireToolbarEvents();
    wireCardEvents();
  }

  function wireSidebarEvents(){
    var sb = document.getElementById('catalogSidebar');
    if(!sb) return;
    sb.querySelectorAll('.catalog-project-item').forEach(function(el){
      el.addEventListener('click', function(e){
        if(e.target.closest('[data-del-project]')) return;
        filter.activeProject = el.dataset.pid;
        render();
      });
    });
    sb.querySelectorAll('[data-del-project]').forEach(function(btn){
      btn.addEventListener('click', function(e){
        e.stopPropagation();
        var pid = btn.dataset.delProject;
        var p = findProject(pid);
        if(!p) return;
        if(!confirm('Delete project "'+p.name+'"? Chats will not be deleted — only the project grouping is removed.')) return;
        deleteProject(pid);
        if(filter.activeProject === pid) filter.activeProject = 'all';
        render();
        toast('Project deleted','success');
      });
    });
    var newBtn = document.getElementById('catalogNewProjectBtn');
    if(newBtn){
      newBtn.addEventListener('click', function(e){
        e.preventDefault();
        showNewProjectForm();
      });
    }
  }

  function showNewProjectForm(){
    var sb = document.getElementById('catalogSidebar');
    if(!sb) return;
    if(document.getElementById('catalogProjectForm')) return;
    var form = document.createElement('div');
    form.className = 'catalog-project-form';
    form.id = 'catalogProjectForm';
    var swatchesHtml = PROJECT_COLORS.map(function(c, i){
      return '<div class="catalog-color-swatch'+(i===0?' selected':'')+'" data-color="'+c+'" style="background:'+c+'"></div>';
    }).join('');
    form.innerHTML =
      '<input type="text" id="catalogProjectName" placeholder="Project name…" maxlength="60">' +
      '<div class="catalog-project-color-picker">' + swatchesHtml + '</div>' +
      '<button class="cpf-add" id="cpfAdd">Add</button>' +
      '<button class="cpf-cancel" id="cpfCancel">Cancel</button>';
    sb.appendChild(form);
    var nameInput = document.getElementById('catalogProjectName');
    nameInput.focus();
    var selectedColor = PROJECT_COLORS[0];
    form.querySelectorAll('.catalog-color-swatch').forEach(function(sw){
      sw.addEventListener('click', function(){
        form.querySelectorAll('.catalog-color-swatch').forEach(function(s){ s.classList.remove('selected'); });
        sw.classList.add('selected');
        selectedColor = sw.dataset.color;
      });
    });
    function commit(){
      var name = nameInput.value.trim();
      if(!name){ form.remove(); return; }
      var p = createProject(name, selectedColor);
      form.remove();
      filter.activeProject = p.id;
      render();
      toast('Project "'+p.name+'" created','success');
    }
    document.getElementById('cpfAdd').addEventListener('click', commit);
    document.getElementById('cpfCancel').addEventListener('click', function(){ form.remove(); });
    nameInput.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter'){ ev.preventDefault(); commit(); }
      else if(ev.key === 'Escape'){ form.remove(); }
    });
  }

  function wireToolbarEvents(){
    var search = document.getElementById('catalogSearchInput');
    if(search){
      var t;
      search.addEventListener('input', function(){
        clearTimeout(t);
        t = setTimeout(function(){
          filter.search = search.value;
          refreshGridOnly();
        }, 180);
      });
      search.addEventListener('keydown', function(ev){
        if(ev.key === 'Escape'){ filter.search=''; search.value=''; refreshGridOnly(); }
      });
    }
    document.querySelectorAll('.catalog-sort-btn').forEach(function(btn){
      btn.addEventListener('click', function(){
        filter.sort = btn.dataset.sort;
        document.querySelectorAll('.catalog-sort-btn').forEach(function(b){ b.classList.remove('active'); });
        btn.classList.add('active');
        refreshGridOnly();
      });
    });
  }

  function wireCardEvents(){
    var grid = document.getElementById('catalogGrid');
    if(!grid) return;
    grid.querySelectorAll('.catalog-card').forEach(function(card){
      var chatId = card.dataset.id;
      // Card click → open chat
      card.addEventListener('click', function(e){
        if(e.target.closest('.catalog-card-actions')) return;
        if(e.target.closest('.catalog-tag-x')) return;
        if(e.target.closest('[data-action="add-tag"]')) return;
        if(e.target.closest('.catalog-assign-menu')) return;
        loadChat(chatId);
        closeAllChats();
      });
      // Delete button
      var delBtn = card.querySelector('[data-action="delete"]');
      if(delBtn){
        delBtn.addEventListener('click', function(e){
          e.stopPropagation();
          if(!confirm('Delete this chat?')) return;
          deleteChat(chatId);
        });
      }
      // Assign to project
      var assignBtn = card.querySelector('[data-action="assign"]');
      if(assignBtn){
        assignBtn.addEventListener('click', function(e){
          e.stopPropagation();
          openAssignMenu(card, chatId);
        });
      }
      // Tag remove
      card.querySelectorAll('.catalog-tag-x').forEach(function(x){
        x.addEventListener('click', function(e){
          e.stopPropagation();
          var tag = x.dataset.tagX;
          removeTag(chatId, tag);
          refreshGridOnly();
        });
      });
      // Add tag
      var addTagBtn = card.querySelector('[data-action="add-tag"]');
      if(addTagBtn){
        addTagBtn.addEventListener('click', function(e){
          e.stopPropagation();
          openTagInput(card, chatId, addTagBtn);
        });
      }
    });
  }

  function openTagInput(card, chatId, btn){
    if(card.querySelector('.catalog-tag-input')) return;
    var input = document.createElement('input');
    input.className = 'catalog-tag-input';
    input.type = 'text';
    input.placeholder = 'tag name…';
    input.maxLength = 20;
    btn.style.display = 'none';
    card.querySelector('.catalog-card-tags').appendChild(input);
    input.focus();
    function commit(){
      var v = input.value.trim();
      if(v){
        addTag(chatId, v);
        refreshGridOnly();
      } else {
        btn.style.display = '';
        input.remove();
      }
    }
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', function(ev){
      if(ev.key === 'Enter'){ ev.preventDefault(); input.blur(); }
      else if(ev.key === 'Escape'){ btn.style.display=''; input.remove(); }
    });
  }

  function openAssignMenu(card, chatId){
    // Close any other open menu
    document.querySelectorAll('.catalog-assign-menu').forEach(function(m){ m.remove(); });
    var menu = document.createElement('div');
    menu.className = 'catalog-assign-menu open';
    var projects = getProjects();
    var current = getProjectFor(chatId);
    var html = '';
    if(projects.length === 0){
      html += '<div style="padding:var(--s-12);font-size:11px;color:var(--text-muted);text-align:center;line-height:1.5">No projects yet.<br>Create one in the sidebar.</div>';
    } else {
      if(current){
        html += '<div class="catalog-assign-item" data-pid=""><div class="catalog-project-dot" style="background:var(--text-muted);opacity:0.5"></div><div class="catalog-assign-item-name">Unassign</div></div>';
        html += '<div class="catalog-assign-divider"></div>';
      }
      projects.forEach(function(p){
        html += '<div class="catalog-assign-item'+(current===p.id?' active':'')+'" data-pid="'+escapeHtml(p.id)+'">';
        html += '<div class="catalog-project-dot" style="background:'+escapeHtml(p.color)+'"></div>';
        html += '<div class="catalog-assign-item-name">'+escapeHtml(p.name)+'</div>';
        html += '</div>';
      });
    }
    html += '<div class="catalog-assign-divider"></div>';
    html += '<div class="catalog-assign-new" id="catalogAssignNew">';
    html += '<svg width="10" height="10" viewBox="0 0 14 14" fill="none"><path d="M7 2v10M2 7h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
    html += 'New project…</div>';
    menu.innerHTML = html;

    var actions = card.querySelector('.catalog-card-actions');
    actions.style.position = 'relative';
    actions.appendChild(menu);

    function close(){ menu.remove(); document.removeEventListener('click', onDocClick); }
    function onDocClick(e){
      if(!menu.contains(e.target) && !actions.contains(e.target)) close();
    }
    setTimeout(function(){ document.addEventListener('click', onDocClick); }, 0);

    menu.querySelectorAll('.catalog-assign-item').forEach(function(item){
      item.addEventListener('click', function(e){
        e.stopPropagation();
        var pid = item.dataset.pid;
        setProjectFor(chatId, pid || null);
        close();
        refreshGridOnly();
        refreshSidebarOnly();
        toast(pid ? 'Chat assigned to project' : 'Chat unassigned','success');
      });
    });
    var newP = menu.querySelector('#catalogAssignNew');
    if(newP){
      newP.addEventListener('click', function(e){
        e.stopPropagation();
        close();
        showNewProjectForm();
        // After form is shown, focus the name input
        setTimeout(function(){
          var nameInput = document.getElementById('catalogProjectName');
          if(nameInput){
            // Pre-assign: when project is created, immediately assign this chat
            var origCommit = showNewProjectForm._commit;
            nameInput.focus();
            // Override Enter to also assign chat
            nameInput.addEventListener('keydown', function(ev){
              if(ev.key === 'Enter'){
                setTimeout(function(){
                  // After project creation, find it and assign
                  var projects = getProjects();
                  if(projects.length){
                    var latest = projects[projects.length-1];
                    setProjectFor(chatId, latest.id);
                    refreshGridOnly();
                    refreshSidebarOnly();
                  }
                }, 50);
              }
            }, {once:true});
          }
        }, 50);
      });
    }
  }

  // ── Public API ──────────────────────────────────────────────────
  return {
    render: render,
    open: function(){ openAllChats(); },
    close: function(){ closeAllChats(); },
    getProjects: getProjects,
    getTagsFor: getTagsFor,
    getProjectFor: getProjectFor,
    addTag: addTag,
    setProjectFor: setProjectFor,
    createProject: createProject,
    deleteProject: deleteProject
  };
})();

async function deleteChat(id){
  if(!isBackendAvailable())return;
  try{await callBridge('delete_chat',id);if(state.activeChatId===id)newChat();refreshChatList();toast('Chat deleted')}catch(e){toast('Failed: '+e.message,'error')}
}

// Sidebar nav actions
document.querySelectorAll('.nav-item[data-action]').forEach(btn=>{
  btn.addEventListener('click',()=>{const action=btn.dataset.action;if(action==='snippets')openSettings('snippets');else if(action==='command')openCommandPalette()});
});

// Drawer
const drawer=document.getElementById('drawerOuter');const backdrop=document.getElementById('backdrop');
function openDrawer(kind){
  const grid=document.getElementById('drawerGrid');grid.innerHTML='';
  const items=kind==='templates'?state.templates:state.skills;
  if(kind==='templates'){
    document.getElementById('drawerTitle').textContent='Prompt Templates';
    document.getElementById('drawerSubtitle').textContent='Structure your intent — Tera Pilot fills the skeleton.';
    state.templates.forEach(t=>{const card=document.createElement('div');card.className='template-card';card.innerHTML=`<div class="template-icon"><svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M10 2v3h3" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg></div><div class="template-name">${escapeHtml(t.name)}</div><div class="template-desc">${escapeHtml(t.desc)}</div><div class="template-structure">${(t.sections||[]).map(s=>`<span class="template-section-tag">${s}</span>`).join('')}</div>`;card.addEventListener('click',()=>{document.querySelectorAll('.template-card').forEach(c=>c.classList.remove('selected'));card.classList.add('selected');setTimeout(()=>{closeDrawer();insertTemplateIntoComposer(t.id,t.name)},150)});grid.appendChild(card)})
  }else{
    document.getElementById('drawerTitle').textContent='Model Skills';
    document.getElementById('drawerSubtitle').textContent='A skill sharpens the model on one capability — no finetuning, just instructions.';
    state.skills.forEach(s=>{const card=document.createElement('div');card.className='skill-card'+(s.id===state.activeSkill?' selected':'');card.innerHTML=`<div class="skill-tag">${escapeHtml(s.tag)}</div><div class="skill-name">${escapeHtml(s.name)}</div><div class="skill-desc">${escapeHtml(s.desc)}</div>`;card.addEventListener('click',()=>{state.activeSkill=s.id;skillChipText.textContent='Skill · '+s.name;skillChip.style.display='inline-flex';document.querySelectorAll('.skill-card').forEach(c=>c.classList.remove('selected'));card.classList.add('selected');setTimeout(closeDrawer,200)});grid.appendChild(card)})
  }
  drawer.classList.add('open');backdrop.classList.add('open');drawer.scrollTop=0;
}
function closeDrawer(){drawer.classList.remove('open');backdrop.classList.remove('open')}
document.querySelectorAll('[data-drawer]').forEach(btn=>{btn.addEventListener('click',()=>openDrawer(btn.dataset.drawer))});
document.getElementById('drawerClose').addEventListener('click',closeDrawer);
backdrop.addEventListener('click',closeDrawer);

// Composer "+" button popup
const composerPlusBtn=document.getElementById('composerPlusBtn');
const composerPlusMenu=document.getElementById('composerPlusMenu');
const contextChip=document.getElementById('contextChip');
const contextChipText=document.getElementById('contextChipText');
let activeContextText='';

composerPlusBtn.addEventListener('click',(e)=>{e.stopPropagation();composerPlusMenu.classList.toggle('open')});
document.addEventListener('click',(e)=>{if(!composerPlusMenu.contains(e.target)&&e.target!==composerPlusBtn)composerPlusMenu.classList.remove('open')});

document.querySelectorAll('.cpm-item').forEach(item=>{
  item.addEventListener('click',()=>{
    const kind=item.dataset.cpm;
    composerPlusMenu.classList.remove('open');
    if(kind==='templates'||kind==='skills'){openDrawer(kind)}
    else if(kind==='context'){loadContextFile()}
  });
});

function loadContextFile(){
  const input=document.createElement('input');
  input.type='file';input.accept='.md,.txt,.text,.markdown';
  input.addEventListener('change',async(e)=>{
    const file=e.target.files[0];if(!file)return;
    try{
      const text=await file.text();
      activeContextText=text;
      const name=file.name.length>20?file.name.slice(0,17)+'...':file.name;
      contextChipText.textContent='Context · '+name;
      contextChip.style.display='inline-flex';
      toast('Context loaded: '+file.name,'success');
    }catch(err){toast('Failed to read file','error')}
  });
  input.click();
}

document.querySelectorAll('.chip-remove').forEach(rm=>{rm.addEventListener('click',(e)=>{e.stopPropagation();const chip=rm.closest('.attachment-chip');chip.style.display='none';if(chip.dataset.attach==='template')state.activeTemplate=null;if(chip.dataset.attach==='skill')state.activeSkill=null;if(chip.dataset.attach==='context'){activeContextText=''};updateComboHint()})});
  
function updateComboHint(){ document.querySelectorAll('.combo-hint').forEach(function(el){ el.remove(); }); if(state.activeSkill && state.autoRoute && skillChip && skillChip.style.display !== 'none'){ var h = document.createElement('span'); h.className = 'combo-hint'; h.innerHTML = 'Skill+Auto<span class="combo-hint-tooltip">Skill = instructions, Auto = model choice</span>'; skillChip.style.position = 'relative'; skillChip.appendChild(h); } }


// Enhance
document.getElementById('enhanceBtn').addEventListener('click',async()=>{
  const text=composerInput.value.trim();if(!text){toast('Type a prompt first','error');return}
  composerStatus.textContent='Enhancing…';
  if(window.__apiBase){
    const requestId='enhance_'+Date.now();composerStatus.textContent='Enhancing…';
    try{const resp=await fetch(window.__apiBase+'/api/chat/oneshot',{method:'POST',headers:_apiHeaders(),body:JSON.stringify({request_id:requestId,max_tokens:800,messages:[{role:'system',content:"You are a prompt engineer. Rewrite the user's request as a structured prompt with these sections: [INTENT], [CONTEXT], [CONSTRAINTS], [DELIVERABLES]. Be concise. Output only the structured prompt, nothing else."},{role:'user',content:text}]})});
      const reader=resp.body.getReader();const decoder=new TextDecoder();let buf='';
      while(true){const{done,value}=await reader.read();if(done)break;buf+=decoder.decode(value,{stream:true});const lines=buf.split('\n');buf=lines.pop()||'';
        for(const line of lines){if(!line.startsWith('data: '))continue;try{const d=JSON.parse(line.slice(6));if(d.type==='oneshot_done'){composerInput.value=d.text;autosize();composerStatus.textContent='Ready';toast('Prompt enhanced','success')}else if(d.type==='oneshot_error'){composerStatus.textContent='Ready';toast('Enhance failed: '+d.error,'error')}}catch(e){}}}
    }catch(e){toast('Enhance failed: '+e.message,'error');composerStatus.textContent='Ready'}
  }else if(isBackendAvailable()){const requestId='enhance_'+Date.now();try{window.bridge.enhance_prompt(requestId,text)}catch(e){toast('Enhance failed: '+e.message,'error');composerStatus.textContent='Ready'}}
  else{composerInput.value=`[INTENT]\n${text}\n\n[CONTEXT]\n- Project: Tera Pilot v1.0.2\n- Stack: Python 3.11+\n\n[CONSTRAINTS]\n- Production-grade\n- Typed\n\n[DELIVERABLES]\n1. Project structure\n2. Implementation\n3. Tests`;autosize();composerStatus.textContent='Ready';toast('Enhanced (demo mode)')}
});

// RAG search
document.getElementById('ragBtn').addEventListener('click',async()=>{
  const text=composerInput.value.trim();if(!text){toast('Type a query to search the project','error');return}
  if(!isBackendAvailable()){toast('Backend not connected','error');return}
  composerStatus.textContent='Searching project…';
  try{
    const results=await callBridge('rag_search',text);
    if(results.length===0){toast('No matches in project');composerStatus.textContent='Ready';return}
    // Inject context into composer
    const sourceLabel = (results[0]&&results[0].source==='grep') ? 'grep search' : 'RAG';
    const ctx=results.slice(0,5).map(r=>`// ${r.path}:${r.line}\n${r.text}`).join('\n\n');
    composerInput.value=`[PROJECT CONTEXT — ${sourceLabel}]\n${ctx}\n\n[MY REQUEST]\n${text}`;
    autosize();composerStatus.textContent='Ready';toast(`Injected ${results.length} matches (${sourceLabel})`,'success');
  }catch(e){toast('RAG failed: '+e.message,'error');composerStatus.textContent='Ready'}
});

// v2.0.0-tui: Launch TUI button — opens tera_pilot_tui in a new terminal window and closes the GUI
document.getElementById('launchTuiBtn').addEventListener('click', async () => {
  if (!isBackendAvailable()) {
    toast('Backend not connected — cannot launch TUI', 'error');
    return;
  }
  try {
    toast('Launching TUI in a new terminal window...', 'info');
    const result = await callBridge('launch_tui');
    if (result.ok) {
      toast('TUI launched — closing GUI', 'success');
      // Try to close the window immediately (works if opened by script)
      window.close();
      // If window.close() didn't work (typical for user-opened tabs),
      // immediately replace content with a "GUI closed" message
      setTimeout(() => {
        if (!window.closed) {
          document.body.innerHTML = `
            <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;text-align:center;padding:24px;font-family:Inter,system-ui,sans-serif;background:var(--bg-primary);color:var(--text-primary)">
              <svg width="64" height="64" viewBox="0 0 24 24" fill="none" style="color:var(--success);margin-bottom:16px">
                <rect x="1" y="2" width="22" height="20" rx="2" stroke="currentColor" stroke-width="1.5"/>
                <path d="M7 10l3 3 7-7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
              <h2 style="margin:0 0 8px;font-size:24px">TUI Launched</h2>
              <p style="color:var(--text-secondary);margin:0 0 24px;font-size:14px">
                The Tera Pilot TUI has been opened in a new terminal window.<br>
                This browser tab can now be closed.
              </p>
              <button onclick="window.close()" class="btn btn-primary" style="padding:10px 24px;font-size:14px;background:var(--success);color:#000;border:none;border-radius:var(--r-input);cursor:pointer">
                Close This Tab
              </button>
            </div>
          `;
        }
      }, 100);
    } else {
      toast('Failed to launch TUI: ' + (result.error || 'Unknown error'), 'error');
    }
  } catch (e) {
    toast('Failed to launch TUI: ' + e.message, 'error');
  }
});

// Settings modal
const modal=document.getElementById('settingsModal');const modalBackdrop=document.getElementById('modalBackdrop');
async function openSettings(tab='providers'){
  // v2.3.0: in Basic UI mode, advanced tabs (Tools/MCP/Agent/Project/
  // Snippets) are hidden — fall back to a visible tab if requested.
  if(document.documentElement.getAttribute('data-ui-mode')!=='advanced'){
    const t=document.querySelector('.modal-tab[data-tab="'+tab+'"]');
    if(t&&t.hasAttribute('data-advanced'))tab='appearance';
  }
  state.activeModalTab=tab;
  document.querySelectorAll('.modal-tab').forEach(t=>t.classList.toggle('active',t.dataset.tab===tab));
  await renderSettingsTab(tab);
  modal.classList.add('open');modalBackdrop.classList.add('open');
}
function closeSettings(){modal.classList.remove('open');modalBackdrop.classList.remove('open')}
document.getElementById('openSettingsBtn').addEventListener('click',()=>openSettings('providers'));
document.getElementById('modalClose').addEventListener('click',closeSettings);
document.getElementById('modalCancel').addEventListener('click',closeSettings);
modalBackdrop.addEventListener('click',closeSettings);

document.querySelectorAll('.modal-tab').forEach(t=>{t.addEventListener('click',async()=>{state.activeModalTab=t.dataset.tab;document.querySelectorAll('.modal-tab').forEach(x=>x.classList.remove('active'));t.classList.add('active');await renderSettingsTab(t.dataset.tab)})});

// ═══════════════════════════════════════════════════════════════
// v1.2: USAGE PANEL — tokens, cost, budget, provider breakdown
// ═══════════════════════════════════════════════════════════════
const usageModal=document.getElementById('usageModal');
const usageBackdrop=document.getElementById('usageBackdrop');
function fmtTok(n){n=n||0;if(n>=1000000)return(n/1000000).toFixed(2)+'M';if(n>=1000)return(n/1000).toFixed(1)+'k';return String(n)}
function fmtUsd(n){return '$'+(n||0).toFixed((n||0)<1?4:2)}
function fmtTimeAgo(ts){
  const s=Math.max(0,(Date.now()/1000)-ts);
  if(s<60)return 'just now';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
async function openUsage(){
  usageModal.classList.add('open');usageBackdrop.classList.add('open');
  await renderUsageModal();
}
function closeUsage(){usageModal.classList.remove('open');usageBackdrop.classList.remove('open')}
document.getElementById('openUsageBtn').addEventListener('click',openUsage);
document.getElementById('usageClose').addEventListener('click',closeUsage);
usageBackdrop.addEventListener('click',closeUsage);

// ═══════════════════════════════════════════════════════════════
// v1.3.1: ACCOUNT PANEL — first pass. Tera Pilot is local-first and has no
// real auth/account system yet, so this starts small: a display name
// (persisted locally) and the connection/status info we already have,
// so the "You" avatar in the sidebar actually goes somewhere instead
// of being decorative. Sign-in / plan / billing land here later.
// ═══════════════════════════════════════════════════════════════
const accountModal=document.getElementById('accountModal');
const accountBackdrop=document.getElementById('accountBackdrop');
const ACCOUNT_NAME_KEY='tera_pilot:accountName';
function getAccountName(){try{return localStorage.getItem(ACCOUNT_NAME_KEY)||'You'}catch(e){return 'You'}}
function setAccountName(name){
  name=(name||'').trim()||'You';
  try{localStorage.setItem(ACCOUNT_NAME_KEY,name)}catch(e){}
  var label=document.getElementById('profileNameLabel');if(label)label.textContent=name;
  var initials=document.getElementById('profileAvatarInitials');
  if(initials)initials.textContent=(name.trim()[0]||'U').toUpperCase();
}
async function openAccount(){
  accountModal.classList.add('open');accountBackdrop.classList.add('open');
  await renderAccountModal();
}
function closeAccount(){accountModal.classList.remove('open');accountBackdrop.classList.remove('open')}
document.getElementById('profileBtn').addEventListener('click',openAccount);
document.getElementById('accountClose').addEventListener('click',closeAccount);
accountBackdrop.addEventListener('click',closeAccount);
async function renderAccountModal(){
  const body=document.getElementById('accountBody');
  let status={};if(isBackendAvailable()){try{status=await callBridge('get_status')}catch(e){}}
  const name=getAccountName();
  body.innerHTML=`
    <div style="display:flex;align-items:center;gap:var(--s-16);padding:var(--s-8) 0 var(--s-20)">
      <div style="width:52px;height:52px;border-radius:50%;background:linear-gradient(135deg,var(--accent) 0%,var(--purple) 100%);display:flex;align-items:center;justify-content:center;color:#fff;font-size:19px;font-weight:600;font-family:'JetBrains Mono',monospace;flex-shrink:0" id="accountAvatarBig">${escapeHtml((name.trim()[0]||'U').toUpperCase())}</div>
      <div style="min-width:0;flex:1">
        <input id="accountNameInput" type="text" value="${escapeHtml(name)}" maxlength="40"
          style="width:100%;font-size:15px;font-weight:600;color:var(--text-primary);background:var(--bg-secondary);border:1px solid var(--border);border-radius:var(--r-sm);padding:6px 10px">
        <div style="font-size:11.5px;color:var(--text-muted);margin-top:4px">Local profile · not synced anywhere yet</div>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">Status</div>
      <div style="padding:var(--s-16);background:var(--bg-floating);border:1px solid var(--border);border-radius:var(--r-panel);font-size:12.5px;color:var(--text-secondary);line-height:1.9">
        <div><strong>Backend:</strong> ${isBackendAvailable()?'<span style="color:var(--success)">Connected</span>':'<span style="color:var(--danger)">Not connected</span>'}</div>
        <div><strong>Project:</strong> <code style="font-family:'JetBrains Mono',monospace;color:var(--info)">${escapeHtml(status.project||'none')}</code></div>
        <div><strong>Chats:</strong> ${(state.chats&&state.chats.length)||0}</div>
      </div>
    </div>
    <div style="font-size:11.5px;color:var(--text-muted);padding:var(--s-4) 0 var(--s-8)">
      Sign-in, plan, and billing aren't wired up yet — this panel is the starting point for them.
    </div>`;
  const input=document.getElementById('accountNameInput');
  if(input){
    input.addEventListener('change',()=>{
      setAccountName(input.value);
      const big=document.getElementById('accountAvatarBig');
      if(big)big.textContent=(input.value.trim()[0]||'U').toUpperCase();
    });
  }
}
(function(){ setAccountName(getAccountName()); })();

async function renderUsageModal(){
  const body=document.getElementById('usageBody');
  body.innerHTML='<div class="usage-empty">Loading usage…</div>';

  let stats=null, breakdown=[], pricing=null, budget=20, quota=null;
  if(isBackendAvailable()){
    try{ stats=await callBridge('get_token_stats'); }catch(e){}
    try{ breakdown=await callBridge('get_provider_breakdown')||[]; }catch(e){}
    try{ pricing=await callBridge('get_pricing_table'); }catch(e){}
    try{ quota=await callBridge('get_quota_stats'); }catch(e){}
  }
  if(!stats){
    // Demo/offline fallback so the panel is never empty in the browser preview
    stats={total_tokens:state.sessionTokens||0,total_tokens_in:0,total_tokens_out:state.sessionTokens||0,
      total_cost:state.sessionCost||0,request_count:state.sessionRequests||0,
      budget_usd:budget,budget_used_pct:Math.min(100,((state.sessionCost||0)/budget)*100),
      burn_rate_per_min:0,budget_minutes_left:null,entries:[]};
  }
  budget=stats.budget_usd||20;
  const pct=stats.budget_used_pct||0;
  const pctClass=pct>=100?'danger':pct>=75?'warn':'';

  let html='';
  html+=`<div class="usage-cards">
    <div class="usage-card"><div class="usage-card-label">Total tokens</div><div class="usage-card-value">${fmtTok(stats.total_tokens)}</div><div class="usage-card-sub">${fmtTok(stats.total_tokens_in)} in · ${fmtTok(stats.total_tokens_out)} out</div></div>
    <div class="usage-card"><div class="usage-card-label">Estimated cost</div><div class="usage-card-value">${fmtUsd(stats.total_cost)}</div><div class="usage-card-sub">${stats.request_count||0} request${stats.request_count===1?'':'s'}</div></div>
    <div class="usage-card"><div class="usage-card-label">Burn rate</div><div class="usage-card-value">${fmtUsd(stats.burn_rate_per_min||0)}<span style="font-size:12px;color:var(--text-muted);font-weight:500">/min</span></div><div class="usage-card-sub">${stats.budget_minutes_left?('~'+Math.round(stats.budget_minutes_left)+' min left at this rate'):'idle'}</div></div>
  </div>`;

  // v1.1.0: Quota card — per-section daily request limits
  if(quota && quota.ok){
    const sections = quota.sections || {};
    const resetAt = quota.reset_at ? new Date(quota.reset_at).toLocaleString() : '00:00 UTC';
    html += `<div class="quota-card">
      <div class="quota-card-title">Daily Quota (per-section)</div>`;
    for(const sec of ['general','heavy_code','office']){
      const s = sections[sec];
      if(!s) continue;
      const limit = s.limit === 0 ? '∞' : s.limit;
      const used = s.used || 0;
      const remaining = s.remaining === -1 ? '∞' : s.remaining;
      const pctUsed = s.limit > 0 ? Math.min(100, (used / s.limit) * 100) : 0;
      const fillClass = s.exhausted ? 'exhausted' : (pctUsed >= 80 ? 'warn' : '');
      html += `
        <div style="margin-bottom:var(--s-12)">
          <div class="quota-card-row">
            <span class="label">${sec.replace(/_/g,' ')}</span>
            <span class="value">${used} / ${limit}${s.limit > 0 ? ' ('+remaining+' left)' : ''}</span>
          </div>
          ${s.limit > 0 ? `<div class="quota-progress"><div class="quota-progress-fill ${fillClass}" style="width:${pctUsed}%"></div></div>` : ''}
        </div>`;
    }
    html += `<div style="font-size:10px;color:var(--text-muted);margin-top:var(--s-4)">Resets at: ${resetAt}</div>
    </div>`;
  }

  html+=`<div class="settings-section">
    <div class="settings-section-title">Monthly budget</div>
    <div class="provider-config-card" style="margin-bottom:0">
      <div class="usage-budget-head"><span class="usage-budget-label">${fmtUsd(stats.total_cost)} of ${fmtUsd(budget)} used</span><span class="usage-budget-num">${pct.toFixed(1)}%</span></div>
      <div class="usage-budget-track"><div class="usage-budget-fill ${pctClass}" style="width:${Math.min(100,pct)}%"></div></div>
      <div class="usage-budget-edit">
        <span style="font-size:12px;color:var(--text-secondary)">Limit</span>
        <input class="field-input" id="usageBudgetInput" type="number" min="0" step="1" value="${budget}">
        <button class="btn-secondary" id="usageBudgetSave" style="padding:6px 12px;font-size:12px">Save</button>
        <span style="font-size:11px;color:var(--text-muted);margin-left:auto">Local estimate — not a hard API cutoff</span>
      </div>
    </div>
  </div>`;

  html+='<div class="settings-section"><div class="settings-section-title">By provider</div><div class="provider-config-card" style="margin-bottom:0">';
  if(breakdown&&breakdown.length){
    const maxCost=Math.max(...breakdown.map(b=>b.cost||0),0.0001);
    const totalCost=breakdown.reduce((s,b)=>s+(b.cost||0),0)||0.0001;
    // Cycle through a small palette so each provider is visually distinct
    // instead of every bar looking identical — was a plain single-color
    // list before, hard to scan once there's more than one provider.
    const palette=['var(--info)','var(--purple)','var(--success)','var(--warning)','var(--danger)','var(--accent)'];
    breakdown.forEach((b,i)=>{
      const w=Math.max(3,((b.cost||0)/maxCost)*100);
      const share=(((b.cost||0)/totalCost)*100).toFixed(0);
      const color=palette[i%palette.length];
      html+=`<div class="usage-bar-row"><div class="usage-bar-name">${escapeHtml((PROVIDER_META[b.provider]||{}).label||b.provider)}</div><div class="usage-bar-track"><div class="usage-bar-fill" style="width:${w}%;background:${color};border-right-color:${color}"></div></div><div class="usage-bar-val">${fmtUsd(b.cost)} <span style="color:var(--text-muted);font-size:11px">(${share}%)</span></div></div>`;
    });
  }else{
    html+='<div class="usage-empty">No usage recorded yet — costs will appear here after your first request.</div>';
  }
  html+='</div></div>';

  const entries=(stats.entries||[]).slice().reverse().slice(0,12);
  html+='<div class="settings-section"><div class="settings-section-title">Recent activity</div>';
  if(entries.length){
    html+='<table class="usage-table"><thead><tr>'
      +'<th class="usage-sortable" data-sort="ts">When</th>'
      +'<th class="usage-sortable" data-sort="provider">Provider · model</th>'
      +'<th class="usage-sortable" style="text-align:right" data-sort="tokens">Tokens</th>'
      +'<th class="usage-sortable" style="text-align:right" data-sort="cost">Cost</th>'
      +'</tr></thead><tbody>';
    entries.forEach((e,i)=>{
      html+=`<tr class="usage-row" data-idx="${i}" title="Click for details"><td>${fmtTimeAgo(e.ts)}</td><td>${escapeHtml((PROVIDER_META[e.provider]||{}).label||e.provider)} · ${escapeHtml(e.model||'')}</td><td class="num">${fmtTok(e.tokens_in+e.tokens_out)}</td><td class="num">${fmtUsd(e.cost)}</td></tr>`;
      html+=`<tr class="usage-row-detail" id="usageDetail${i}" style="display:none"><td colspan="4"><div class="usage-row-detail-inner">`
        +`<div><span class="usage-detail-label">Prompt tokens</span> ${fmtTok(e.tokens_in)}</div>`
        +`<div><span class="usage-detail-label">Completion tokens</span> ${fmtTok(e.tokens_out)}</div>`
        +`<div><span class="usage-detail-label">Timestamp</span> ${escapeHtml(e.ts||'')}</div>`
        +`<div style="color:var(--text-muted)">Full request/response inspection isn't stored yet — only token counts and cost are logged per call.</div>`
        +`</div></td></tr>`;
    });
    html+='</tbody></table>';
  }else{
    html+='<div class="usage-empty">Nothing yet.</div>';
  }
  html+='</div>';

  const liveNote=pricing&&pricing.live?('Live pricing · fetched '+fmtTimeAgo(pricing.fetched_at)):'Using bundled pricing snapshot';
  html+=`<div class="settings-section" style="margin-bottom:0">
    <div class="usage-pricing-note">
      <span>${liveNote}</span>
      <button class="btn-secondary" id="usageRefreshPricing" style="padding:6px 12px;font-size:12px">Refresh pricing</button>
    </div>
  </div>`;

  body.innerHTML=html;

  // v1.1.1: expandable rows — click a row to toggle its detail panel.
  body.querySelectorAll('.usage-row').forEach(row=>{
    row.addEventListener('click',()=>{
      const detail=document.getElementById('usageDetail'+row.dataset.idx);
      if(detail)detail.style.display=detail.style.display==='none'?'table-row':'none';
    });
  });
  // v1.1.1: sortable columns — click a header to sort recent activity by
  // that field (toggles ascending/descending on repeated clicks).
  let sortState={key:null,dir:1};
  body.querySelectorAll('.usage-sortable').forEach(th=>{
    th.addEventListener('click',()=>{
      const key=th.dataset.sort;
      sortState.dir=(sortState.key===key)?-sortState.dir:1;
      sortState.key=key;
      const sorted=entries.slice().sort((a,b)=>{
        let av,bv;
        if(key==='ts'){av=a.ts||'';bv=b.ts||''}
        else if(key==='provider'){av=(a.provider||'')+String(a.model||'');bv=(b.provider||'')+String(b.model||'')}
        else if(key==='tokens'){av=(a.tokens_in||0)+(a.tokens_out||0);bv=(b.tokens_in||0)+(b.tokens_out||0)}
        else{av=a.cost||0;bv=b.cost||0}
        if(av<bv)return -1*sortState.dir; if(av>bv)return 1*sortState.dir; return 0;
      });
      const tbody=body.querySelector('.usage-table tbody');
      if(!tbody)return;
      tbody.innerHTML=sorted.map((e,i)=>
        `<tr class="usage-row" data-idx="s${i}" title="Click for details"><td>${fmtTimeAgo(e.ts)}</td><td>${escapeHtml((PROVIDER_META[e.provider]||{}).label||e.provider)} · ${escapeHtml(e.model||'')}</td><td class="num">${fmtTok(e.tokens_in+e.tokens_out)}</td><td class="num">${fmtUsd(e.cost)}</td></tr>`
        +`<tr class="usage-row-detail" id="usageDetails${i}" style="display:none"><td colspan="4"><div class="usage-row-detail-inner">`
        +`<div><span class="usage-detail-label">Prompt tokens</span> ${fmtTok(e.tokens_in)}</div>`
        +`<div><span class="usage-detail-label">Completion tokens</span> ${fmtTok(e.tokens_out)}</div>`
        +`<div><span class="usage-detail-label">Timestamp</span> ${escapeHtml(e.ts||'')}</div>`
        +`</div></td></tr>`
      ).join('');
      tbody.querySelectorAll('.usage-row').forEach(row=>{
        row.addEventListener('click',()=>{
          const detail=document.getElementById('usageDetail'+row.dataset.idx);
          if(detail)detail.style.display=detail.style.display==='none'?'table-row':'none';
        });
      });
    });
  });

  const budgetInput=document.getElementById('usageBudgetInput');
  document.getElementById('usageBudgetSave').addEventListener('click',async()=>{
    const val=parseFloat(budgetInput.value)||0;
    if(isBackendAvailable()){
      try{await callBridge('set_budget',val);toast('Budget updated','success');await renderUsageModal();}
      catch(e){toast('Failed: '+e.message,'error')}
    }else{toast('Budget saved (demo mode)','success')}
  });
  const refreshBtn=document.getElementById('usageRefreshPricing');
  refreshBtn.addEventListener('click',async()=>{
    if(!isBackendAvailable()){toast('Backend not connected','error');return}
    refreshBtn.textContent='Refreshing…';refreshBtn.disabled=true;
    try{
      const r=await callBridge('fetch_live_pricing');
      if(r.ok)toast(`Pricing updated for ${r.count} models`,'success');
      else toast('Could not fetch live pricing — kept snapshot','error');
      await renderUsageModal();
    }catch(e){toast('Failed: '+e.message,'error')}
    finally{refreshBtn.textContent='Refresh pricing';refreshBtn.disabled=false}
  });

  // v2.0.0 — Request queue stats panel
  const queuePanel = document.getElementById('usageQueuePanel');
  if (queuePanel) {
    queuePanel.innerHTML = '<div class="settings-section-title">Request Queues</div><div class="dim" style="font-size:12px">Loading…</div>';
    if (isBackendAvailable()) {
      try {
        const r = await callBridge('get_queue_stats');
        if (r && r.ok) {
          const stats = r.stats || {};
          const ids = Object.keys(stats);
          if (ids.length === 0) {
            queuePanel.innerHTML = '<div class="settings-section-title">Request Queues</div>' +
              '<div class="dim" style="font-size:12px">No provider queues registered yet (queues are created on first provider call).</div>';
          } else {
            const rows = ids.map(pid => {
              const s = stats[pid] || {};
              const cooldown = s.cooldown_until ? new Date(s.cooldown_until).toLocaleTimeString() : '-';
              return `<tr><td>${pid}</td><td>${s.in_flight||0}/${s.max_in_flight||1}</td><td>${s.pending||0}</td><td>${s.total_retries||0}</td><td>${s.total_errors||0}</td><td>${cooldown}</td></tr>`;
            }).join('');
            queuePanel.innerHTML = '<div class="settings-section-title">Request Queues</div>' +
              '<table style="width:100%;font-size:12px;border-collapse:collapse;margin-top:8px">' +
              '<thead><tr><th align="left">Provider</th><th>In-flight</th><th>Pending</th><th>Retries</th><th>Errors</th><th>Cooldown</th></tr></thead>' +
              '<tbody>' + rows + '</tbody></table>';
          }
        }
      } catch (e) {
        queuePanel.innerHTML = '<div class="settings-section-title">Request Queues</div>' +
          '<div class="dim" style="font-size:12px;color:var(--text-muted)">Queue stats unavailable: ' + e.message + '</div>';
      }
    } else {
      queuePanel.innerHTML = '<div class="settings-section-title">Request Queues</div>' +
        '<div class="dim" style="font-size:12px;color:var(--text-muted)">Backend not connected.</div>';
    }
  }

  // v2.0.0 — Persistence backend selector
  const persPanel = document.getElementById('usagePersistencePanel');
  if (persPanel) {
    let current = 'json';
    if (isBackendAvailable()) {
      try { const r = await callBridge('get_persistence_backend'); if (r && r.ok) current = r.backend || 'json'; } catch (e) {}
    }
    persPanel.innerHTML = `
      <div class="settings-section-title">Chat storage backend</div>
      <div class="dim" style="font-size:12px;margin-bottom:8px">
        Choose where new chats are persisted. Existing chats are not migrated.
      </div>
      <select id="usagePersistenceSelect" class="hc-role-select" style="min-width:240px">
        <option value="json"${current==='json'?' selected':''}>JSON files (~/.tera_pilot/chats/*.json)</option>
        <option value="sqlite"${current==='sqlite'?' selected':''}>SQLite database (~/.tera_pilot/chats.sqlite3)</option>
      </select>`;
    const sel = document.getElementById('usagePersistenceSelect');
    if (sel) {
      sel.addEventListener('change', async () => {
        if (!isBackendAvailable()) { toast('Backend not connected', 'error'); return; }
        try {
          const r = await callBridge('set_persistence_backend', sel.value);
          if (r && r.ok) toast('Storage backend set to ' + sel.value, 'success');
          else toast('Failed: ' + (r && r.error), 'error');
        } catch (e) { toast('Failed: ' + e.message, 'error'); }
      });
    }
  }
}
if(window.bridge&&window.bridge.token_stats_updated){
  window.bridge.token_stats_updated.connect(function(){ if(usageModal.classList.contains('open'))renderUsageModal(); });
}

// ===== Token Optimization =====
(function(){
  var indicator = document.getElementById('tokenOptIndicator');
  var optText = document.getElementById('tokenOptText');
  var tips = [];
  var lastLevel = 'ok';

  function loadTips(){
    if(!isBackendAvailable()) return;
    callBridge('token_optimization_tips').then(function(res){
      if(!res || !res.ok) return;
      tips = res.tips || [];
      updateIndicator(res.total_potential_savings || 0);
    });
  }

  function updateIndicator(savings){
    if(!indicator) return;
    // Determine level
    var hasWarning = tips.some(function(t){ return t.type === 'warning'; });
    var hasInfo = tips.some(function(t){ return t.type === 'info'; });
    lastLevel = hasWarning ? 'warning' : hasInfo ? 'info' : 'ok';

    indicator.setAttribute('data-level', lastLevel);
    indicator.style.display = 'inline-flex';

    if(savings > 0){
      optText.textContent = '~' + savings + ' tokens wasted';
    } else if(hasWarning){
      optText.textContent = 'tips';
    } else {
      optText.textContent = 'lean';
    }
  }

  // Show tips when clicking the indicator
  if(indicator){
    indicator.addEventListener('click', function(){
      // Open usage modal and show tips
      var usageModal = document.getElementById('usageModal');
      var usageBackdrop = document.getElementById('usageBackdrop');
      if(usageModal) usageModal.style.display = 'flex';
      if(usageBackdrop) usageBackdrop.style.display = 'block';
      renderTipsInUsage();
    });
  }

  function renderTipsInUsage(){
    var usageBody = document.getElementById('usageBody');
    if(!usageBody) return;

    var tipsHtml = '<div class="token-opt-header">' +
      '<h4>Token Optimization</h4>' +
      '<span class="token-opt-savings-total"></span>' +
    '</div>';

    if(tips.length === 0){
      tipsHtml += '<div style="padding:20px;text-align:center;color:var(--text-muted);font-size:12px">Loading tips...</div>';
    } else {
      tipsHtml += '<div class="token-opt-tips">';
      tips.forEach(function(t){
        var iconClass = t.type;
        var icon = t.type === 'ok' ? '✓' : t.type === 'warning' ? '!' : 'i';
        var savingsHtml = t.savings > 0 ? '<span class="token-opt-savings">-' + t.savings + ' tokens/request</span>' : '';
        tipsHtml += '<div class="token-opt-tip">' +
          '<div class="token-opt-tip-icon ' + iconClass + '">' + icon + '</div>' +
          '<div><div class="token-opt-tip-title">' + esc(t.title) + savingsHtml + '</div>' +
          '<div class="token-opt-tip-detail">' + esc(t.detail) + '</div></div>' +
        '</div>';
      });
      tipsHtml += '</div>';
    }

    // Prepend to usage body
    var existing = usageBody.innerHTML;
    usageBody.innerHTML = tipsHtml + existing;
  }

  function esc(s){
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  // Refresh periodically
  setInterval(loadTips, 15000);
  // Initial load
  setTimeout(loadTips, 2000);

  // Also refresh when chat messages change
  window.__refreshTokenOpt = loadTips;
})();

async function renderSettingsTab(tab){
  const body=document.getElementById('settingsBody');
  const footer=document.getElementById('modalFooter');
  if(tab==='appearance'){footer.style.display='none';body.innerHTML='';renderAppearanceTab(body)}
  else if(tab==='providers'){footer.style.display='';body.innerHTML='';await renderProvidersTab(body)}
  else if(tab==='tools'){footer.style.display='none';body.innerHTML='';await renderToolsTab(body)}
  else if(tab==='agent'){footer.style.display='none';body.innerHTML='';await renderAgentTab(body)}
      else if(tab==='project'){footer.style.display='none';body.innerHTML='';await renderProjectTab(body)}
      else if(tab==='snippets'){footer.style.display='none';body.innerHTML='';await renderSnippetsTab(body)}
  else if(tab==='about'){footer.style.display='none';body.innerHTML='';await renderAboutTab(body)}
}

/* ===================================================================
   v2.2.3 — Tools tab inside Settings
   ===================================================================
   Replaces the old sidebar Tools section. Renders a grid of tool
   cards; clicking a card renders that tool's content directly inside
   the Settings modal body, using the RENDERERS exported by
   tools_panels.js. A "← Back to Tools" button at the top returns to
   the grid view.                                                  */
async function renderToolsTab(body){
  // If a sub-tool was previously selected, clear it so we always land
  // on the grid view when the user clicks the Tools tab.
  state.__toolsSubTab = null;
  await _renderToolsGrid(body);
}

async function _renderToolsGrid(body){
  const meta = window.__teraPilotToolMeta || {};
  const renderers = window.__teraPilotToolsRenderers || {};
  // Categorize tools so the grid reads better than a flat 17-card list.
  const groups = [
    { title: 'Agent runtime', tools: ['capabilities','hooks','checkpoints','handoffs','learnings','persona'] },
    { title: 'Code & collaboration', tools: ['github','github_actions','consensus','second_opinion','verify'] },
    { title: 'Operations', tools: ['audit','spend','notify','daemon'] },
    { title: 'Extensions', tools: ['mcp_server','providers'] },
  ];
  const icons = {
    capabilities:   '<path d="M3 2.5h10A1.5 1.5 0 0 1 14.5 4v8A1.5 1.5 0 0 1 13 13.5H3A1.5 1.5 0 0 1 1.5 12V4A1.5 1.5 0 0 1 3 2.5Z" stroke="currentColor" stroke-width="1.2"/><path d="M4 5.5h8M4 8h8M4 10.5h5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
    hooks:          '<path d="M5 2.5h6v3a3 3 0 0 1-6 0V2.5Z" stroke="currentColor" stroke-width="1.2"/><path d="M5 4H3a1 1 0 0 0-1 1v3a2 2 0 0 0 2 2h2M11 4h2a1 1 0 0 1 1 1v3a2 2 0 0 1-2 2H9" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
    checkpoints:    '<path d="M3 8a5 5 0 1 0 1.5-3.5L3 6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/><path d="M3 3v3h3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>',
    handoffs:       '<path d="M2.5 4h6v6h-6zM8.5 6.5h5V14h-5z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M5 14h7" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
    github:         '<path d="M8 1.5A6.5 6.5 0 0 0 5.93 14c.33.06.45-.14.45-.32v-1.13c-1.83.4-2.22-.88-2.22-.88-.3-.76-.73-.96-.73-.96-.6-.4.04-.4.04-.4.66.05 1 .68 1 .68.59 1 1.55.72 1.93.55.06-.43.23-.72.42-.88-1.47-.17-3.01-.74-3.01-3.27 0-.72.26-1.31.68-1.78-.07-.17-.3-.84.07-1.75 0 0 .56-.18 1.82.68a6.3 6.3 0 0 1 3.3 0c1.26-.86 1.82-.68 1.82-.68.37.91.14 1.58.07 1.75.42.47.68 1.06.68 1.78 0 2.54-1.55 3.1-3.02 3.26.24.2.45.62.45 1.25v1.85c0 .18.12.39.46.32A6.5 6.5 0 0 0 8 1.5Z" stroke="currentColor" stroke-width="1.0" stroke-linejoin="round"/>',
    audit:          '<path d="M8 1.5L13.5 3.5V8c0 3-2.5 5.5-5.5 6.5C5 13.5 2.5 11 2.5 8V3.5L8 1.5Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M6 8L7.5 9.5L10 7" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>',
    spend:          '<path d="M2 8c0-3.3 2.7-6 6-6s6 2.7 6 6-2.7 6-6 6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/><path d="M8 4.5V8L10.5 9.5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="4" cy="12" r="1.2" fill="currentColor"/>',
    consensus:      '<circle cx="4" cy="4" r="2.2" stroke="currentColor" stroke-width="1.2"/><circle cx="12" cy="4" r="2.2" stroke="currentColor" stroke-width="1.2"/><circle cx="8" cy="12" r="2.2" stroke="currentColor" stroke-width="1.2"/><path d="M5.7 5.2L7 10M10.3 5.2L9 10M5.5 4h5" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>',
    second_opinion: '<circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.2"/><path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M5 7h4M7 5v4" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>',
    verify:         '<path d="M3 8l3 3 7-7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>',
    learnings:      '<path d="M3 3.5h6v6h-6zM9 5.5h4V13H9z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M5 6.5h2M5 8h2M10.5 8.5h1.5M10.5 10.5h1.5" stroke="currentColor" stroke-width="1.0" stroke-linecap="round"/>',
    github_actions: '<path d="M2 8h3l1.5-3 2 5 1.5-2h4" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>',
    notify:         '<path d="M3 7a5 5 0 0 1 10 0v3l1.5 2H1.5L3 10V7Z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M6 13a2 2 0 0 0 4 0" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
    daemon:         '<rect x="2" y="3" width="12" height="10" rx="1.5" stroke="currentColor" stroke-width="1.2"/><path d="M5 6h6M5 8.5h6M5 11h3" stroke="currentColor" stroke-width="1.0" stroke-linecap="round"/>',
    mcp_server:     '<circle cx="8" cy="8" r="2" stroke="currentColor" stroke-width="1.2"/><path d="M8 1v3M8 12v3M1 8h3M12 8h3M3 3l2 2M11 11l2 2M13 3l-2 2M5 11l-2 2" stroke="currentColor" stroke-width="1.1" stroke-linecap="round"/>',
    persona:        '<circle cx="8" cy="6" r="2.5" stroke="currentColor" stroke-width="1.2"/><path d="M3 14c0-2.8 2.2-5 5-5s5 2.2 5 5" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>',
    providers:      '<path d="M2 5l2-2 4 4 4-4 2 2-4 4 4 4-2 2-4-4-4 4-2-2 4-4-4-4z" stroke="currentColor" stroke-width="1.1" stroke-linejoin="round"/>',
  };
  let html = '<div class="settings-tools-intro">All backend capabilities previously on the sidebar — now collected here. Click any card to open its panel inside this window.</div>';
  for(const g of groups){
    html += '<div class="settings-tools-group"><div class="settings-tools-group-title">'+g.title+'</div><div class="settings-tools-grid">';
    for(const id of g.tools){
      const m = meta[id] || {title:id, subtitle:''};
      const svgInner = icons[id] || '';
      html += '<button class="settings-tool-card" data-tool="'+id+'">'+
        '<div class="settings-tool-card-icon"><svg viewBox="0 0 16 16" fill="none">'+svgInner+'</svg></div>'+
        '<div class="settings-tool-card-body">'+
          '<div class="settings-tool-card-title">'+escapeHtml(m.title)+'</div>'+
          '<div class="settings-tool-card-sub">'+escapeHtml(m.subtitle)+'</div>'+
        '</div>'+
        '<svg class="settings-tool-card-arrow" viewBox="0 0 14 14" fill="none"><path d="M3 7h8M7 3l4 4-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'+
      '</button>';
    }
    html += '</div></div>';
  }
  // Add a hint about MCP and Custom Providers also being available elsewhere
  html += '<div class="settings-tools-note">Tip: MCP server connections live under the <strong>MCP</strong> tab. Adding custom OpenAI-compatible endpoints lives under the <strong>Providers</strong> tab → Custom providers.</div>';
  body.innerHTML = html;
  body.querySelectorAll('.settings-tool-card').forEach(function(card){
    card.addEventListener('click', async function(){
      const id = card.dataset.tool;
      if(!renderers[id]){ toast('Tool "'+id+'" not loaded yet','error'); return; }
      state.__toolsSubTab = id;
      // Render the sub-tool content inside the same modal body
      body.innerHTML = '<div class="settings-tools-subheader">'+
        '<button class="btn-secondary" id="toolsBackBtn">'+
          '<svg width="12" height="12" viewBox="0 0 14 14" fill="none"><path d="M11 7H3M7 3L3 7l4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'+
          'Back to Tools'+
        '</button>'+
        '<div class="settings-tools-subheader-title">'+escapeHtml((meta[id]||{}).title || id)+'</div>'+
        '<button class="icon-btn" id="toolsSubRefresh" title="Refresh">'+
          '<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 8a6 6 0 0 1 10.5-4M14 8A6 6 0 0 1 3.5 12" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><path d="M12 2v3h-3M4 14v-3h3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>'+
        '</button>'+
      '</div>'+
      '<div class="settings-tools-subbody" id="toolsSubBody">'+
        '<div class="tools-loading">Loading…</div>'+
      '</div>';
      document.getElementById('toolsBackBtn').addEventListener('click', async function(){
        state.__toolsSubTab = null;
        await _renderToolsGrid(body);
      });
      document.getElementById('toolsSubRefresh').addEventListener('click', async function(){
        await _renderToolsSub(body, id, meta, renderers);
      });
      await _renderToolsSub(body, id, meta, renderers);
    });
  });
}

async function _renderToolsSub(body, id, meta, renderers){
  const subBody = document.getElementById('toolsSubBody');
  if(!subBody) return;
  subBody.innerHTML = '<div class="tools-loading">Loading…</div>';
  try {
    const fn = renderers[id];
    if(!fn) throw new Error('No renderer for '+id);
    await fn(subBody);
  } catch(e){
    console.error('[tools-tab] render error', e);
    subBody.innerHTML = '<div class="tools-error">'+escapeHtml(e.message)+'</div>';
  }
}

// v1.1.1: this tab did not exist before — get_agent_autonomy/
// set_agent_autonomy and the diff_review config flag were wired on the
// backend but had ZERO UI control anywhere, so autonomy silently sat on
// its hardcoded default forever and diff-review could never be turned
// off. This is the first actual UI surface for both.
async function renderAgentTab(body){
  let autonomy='always_ask', diffReview=true;
  let guardianLevel='off';
  if(isBackendAvailable()){
    try{autonomy=await callBridge('get_agent_autonomy')}catch(e){}
    try{const s=await callBridge('get_settings');if(s&&typeof s.diff_review==='boolean')diffReview=s.diff_review}catch(e){}
    try{const g=await callBridge('get_guardian_level');if(g&&g.ok)guardianLevel=g.level||'off'}catch(e){}
  }
  const levels=[
    {id:'always_ask',label:'Always ask',desc:'Confirm before every command, delete, rename, patch, or commit. Safest — the agent pauses for your Allow/Deny on anything side-effecting.'},
    {id:'new_files_only',label:'New files only',desc:'Auto-approve creating brand-new files; still asks before deleting, renaming, running commands, or committing.'},
    {id:'never_ask',label:'Never ask',desc:'Run everything without confirmation. Fastest, but there\u2019s no safety net if the agent gets something wrong.'},
  ];
  const guardianLevels=[
    {id:'off',label:'Off',desc:'Guardian disabled. The agent uses only the autonomy rules above. Fastest, but no LLM safety review.'},
    {id:'dangerous_only',label:'Dangerous tools only (recommended)',desc:'An LLM reviewer inspects only high-risk tool calls (file paths in /etc, /usr, rm -rf, etc.) and can Approve / Reject / propose a safer alternative.'},
    {id:'all',label:'All tools',desc:'An LLM reviewer inspects every medium+ risk tool call. Most cautious; adds latency to each side-effecting action.'},
  ];
  body.innerHTML=`
    <div class="settings-section">
      <div class="settings-section-title">Agent autonomy</div>
      <div class="provider-config-card" style="margin-bottom:0">
        <div style="display:flex;flex-direction:column;gap:var(--s-8)">
          ${levels.map(l=>`
            <label class="autonomy-option${autonomy===l.id?' selected':''}" data-level="${l.id}" style="display:flex;gap:var(--s-12);align-items:flex-start;padding:var(--s-12);border:1px solid var(--border);border-radius:var(--r-sm);cursor:pointer">
              <input type="radio" name="autonomyLevel" value="${l.id}" ${autonomy===l.id?'checked':''} style="margin-top:2px">
              <div><div style="font-weight:600;font-size:13px">${l.label}</div><div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${l.desc}</div></div>
            </label>`).join('')}
        </div>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">Guardian safety review</div>
      <div class="provider-config-card" style="margin-bottom:0">
        <div style="display:flex;flex-direction:column;gap:var(--s-8)">
          ${guardianLevels.map(l=>`
            <label class="autonomy-option${guardianLevel===l.id?' selected':''}" data-guardian="${l.id}" style="display:flex;gap:var(--s-12);align-items:flex-start;padding:var(--s-12);border:1px solid var(--border);border-radius:var(--r-sm);cursor:pointer">
              <input type="radio" name="guardianLevel" value="${l.id}" ${guardianLevel===l.id?'checked':''} style="margin-top:2px">
              <div><div style="font-weight:600;font-size:13px">${l.label}</div><div style="font-size:12px;color:var(--text-secondary);margin-top:2px">${l.desc}</div></div>
            </label>`).join('')}
        </div>
      </div>
    </div>
    <div class="settings-section">
      <div class="settings-section-title">File writes</div>
      <div class="toggle-row">
        <div><div style="font-weight:500;font-size:13px">Show diff before writing files</div><div style="font-size:12px;color:var(--text-muted)">Review a green/red diff and approve it before write_file or str_replace touches disk.</div></div>
        <button class="btn-secondary" id="diffReviewToggle">${diffReview?'Disable':'Enable'}</button>
      </div>
    </div>`;
  body.querySelectorAll('input[name="autonomyLevel"]').forEach(inp=>{
    inp.addEventListener('change',async()=>{
      const level=inp.value;
      body.querySelectorAll('.autonomy-option[data-level]').forEach(el=>el.classList.toggle('selected',el.dataset.level===level));
      if(!isBackendAvailable()){toast('Backend not connected','error');return}
      try{await callBridge('set_agent_autonomy',level);toast('Autonomy set to '+level.replace(/_/g,' '),'success')}
      catch(e){toast('Failed: '+e.message,'error')}
    });
  });
  body.querySelectorAll('input[name="guardianLevel"]').forEach(inp=>{
    inp.addEventListener('change',async()=>{
      const level=inp.value;
      body.querySelectorAll('.autonomy-option[data-guardian]').forEach(el=>el.classList.toggle('selected',el.dataset.guardian===level));
      if(!isBackendAvailable()){toast('Backend not connected','error');return}
      try{await callBridge('set_guardian_level',level);toast('Guardian level set to '+level.replace(/_/g,' '),'success')}
      catch(e){toast('Failed: '+e.message,'error')}
    });
  });
  const diffBtn=document.getElementById('diffReviewToggle');
  diffBtn.addEventListener('click',async()=>{
    diffReview=!diffReview;
    diffBtn.textContent=diffReview?'Disable':'Enable';
    if(!isBackendAvailable()){toast('Backend not connected','error');return}
    try{await callBridge('set_diff_review',diffReview);toast('Diff review '+(diffReview?'enabled':'disabled'),'success')}
    catch(e){toast('Failed: '+e.message,'error')}
  });
}

function renderAppearanceTab(body){
  const current=document.documentElement.getAttribute('data-theme')||'dark';
  const themes=[
    {id:'noir',label:'Noir',desc:'Black launch-pad minimalism — hairline edges, mono readouts, one white accent.',preview:'tc-preview-noir',bar:'tc-bar-noir',dot:'tc-bar-dot-noir'},
    {id:'ember',label:'Ember',desc:'Warm charcoal dark with a soft clay accent. Quiet and neutral.',preview:'tc-preview-ember',bar:'tc-bar-ember',dot:'tc-bar-dot-ember'},
    {id:'dark',label:'Dark',desc:'Easy on the eyes. For late-night sessions.',preview:'tc-preview-dark',bar:'tc-bar-dark',dot:'tc-bar-dot-dark'},
    {id:'light',label:'Light',desc:'Clean and bright. For daytime work.',preview:'tc-preview-light',bar:'tc-bar-light',dot:'tc-bar-dot-light'},
    {id:'espresso',label:'Espresso',desc:'Warm dark with a terracotta accent. Cozy and focused.',preview:'tc-preview-espresso',bar:'tc-bar-espresso',dot:'tc-bar-dot-espresso'},
    {id:'aurora',label:'Aurora',desc:'Vivid gradients and glass. A bold, modern look.',preview:'tc-preview-aurora',bar:'tc-bar-aurora',dot:'tc-bar-dot-aurora'},
    {id:'graphite',label:'Graphite',desc:'Soft dark neutral with a cool blue accent.',preview:'tc-preview-graphite',bar:'tc-bar-graphite',dot:'tc-bar-dot-graphite'},
    {id:'clay',label:'Clay',desc:'Warm cream and terracotta. A cozy daytime palette.',preview:'tc-preview-clay',bar:'tc-bar-clay',dot:'tc-bar-dot-clay'},
    {id:'linen',label:'Linen',desc:'Airy cream glass with a soft teal-gold glow and a confident blue accent. Frosted, daylight, alive.',preview:'tc-preview-linen',bar:'tc-bar-linen',dot:'tc-bar-dot-linen'},
    {id:'flat',label:'Flat',desc:'Dark and flat, no blur or gradients anywhere. A single quiet accent, modest corners, calm empty space.',preview:'tc-preview-flat',bar:'tc-bar-flat',dot:'tc-bar-dot-flat'},
    {id:'auto',label:'System',desc:'Follows your OS preference.',preview:'tc-preview-auto',bar:'tc-bar-dark',dot:'tc-bar-dot-dark'},
  ];
  let html='<div class="settings-section"><div class="settings-section-title">Theme</div><div class="theme-grid">';
  for(const t of themes){
    const isActive=current===t.id||(current===null&&t.id==='auto');
    html+=`<div class="theme-card${isActive?' active':''}" data-theme-id="${t.id}">
      <div class="tc-check">&#10003;</div>
      <div class="tc-preview ${t.preview}"><div class="tc-bar ${t.bar}"><div class="tc-bar-dot ${t.dot}"></div><div class="tc-bar-dot ${t.dot}" style="width:14px"></div><div class="tc-bar-dot ${t.dot}" style="width:6px;margin-left:auto"></div></div></div>
      <div class="tc-label">${t.label}</div>
      <div class="tc-desc">${t.desc}</div>
    </div>`;
  }
  html+='</div></div>';
  // Text size (segmented control)
  const curSize=document.documentElement.getAttribute('data-textsize')||'medium';
  const sizes=[{id:'small',label:'Small'},{id:'medium',label:'Medium'},{id:'large',label:'Large'}];
  html+=`<div class="settings-section"><div class="settings-section-title">Text size</div>
  <div class="textsize-row">
    <div class="textsize-copy"><div class="toggle-row-label">Message text size</div><div class="toggle-row-desc">Affects the size of assistant &amp; your messages in the chat.</div></div>
    <div class="textsize-seg" id="textsizeSeg">${sizes.map(s=>`<button class="textsize-btn${curSize===s.id?' active':''}" data-size="${s.id}">${s.label}</button>`).join('')}</div>
  </div></div>`;
  // Additional settings
  html+=`<div class="settings-section"><div class="settings-section-title">Interface</div>
  <div style="padding:var(--s-16);background:var(--bg-floating);border:1px solid var(--border);border-radius:var(--r-panel)">
    <div class="toggle-row"><div><div class="toggle-row-label">Neural background animation</div><div class="toggle-row-desc">Floating particle network behind the UI. Disable for lower GPU usage.</div></div><div class="toggle ${!state.neuralBgDisabled?'on':''}" id="toggleNeural"></div></div>
  </div></div>`;
  body.innerHTML=html;
  // Wire text size buttons
  body.querySelectorAll('.textsize-btn').forEach(btn=>{
    btn.addEventListener('click',()=>{
      setTextSize(btn.dataset.size);
      body.querySelectorAll('.textsize-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
    });
  });
  // Wire theme cards
  body.querySelectorAll('.theme-card').forEach(card=>{
    card.addEventListener('click',()=>{
      const themeId=card.dataset.themeId;
      setTheme(themeId);
      body.querySelectorAll('.theme-card').forEach(c=>c.classList.remove('active'));
      card.classList.add('active');
    });
  });
  // Wire neural toggle
  const neuralToggle=document.getElementById('toggleNeural');
  if(neuralToggle){
    neuralToggle.addEventListener('click',()=>{
      neuralToggle.classList.toggle('on');
      state.neuralBgDisabled=!neuralToggle.classList.contains('on');
      document.querySelector('.neural-bg').style.display=state.neuralBgDisabled?'none':'';
      localStorage.setItem('tera_pilot:neuralBg',state.neuralBgDisabled?'off':'on');
    });
  }
}

function setTheme(id){
  if(id==='auto'){
    document.documentElement.removeAttribute('data-theme');
    const mq=window.matchMedia('(prefers-color-scheme: light)');
    if(mq.matches)document.documentElement.setAttribute('data-theme','light');
    mq.addEventListener('change',function h(){
      if(!document.documentElement.getAttribute('data-theme')||document.documentElement.getAttribute('data-theme')!=='dark'&&document.documentElement.getAttribute('data-theme')!=='light'){
        document.documentElement.removeAttribute('data-theme');
        if(mq.matches)document.documentElement.setAttribute('data-theme','light');
      }
    });
  }else{
    document.documentElement.setAttribute('data-theme',id);
  }
  localStorage.setItem('tera_pilot:theme',id);
  if(isBackendAvailable())callBridge('save_settings',{ui:{theme:id}}).catch(function(){});
}

// v1.2: Text size — Small / Medium / Large (segmented control)
function setTextSize(id){
  if(id==='medium')document.documentElement.removeAttribute('data-textsize');
  else document.documentElement.setAttribute('data-textsize',id);
  localStorage.setItem('tera_pilot:textsize',id);
  if(isBackendAvailable())callBridge('save_settings',{ui:{text_size:id}}).catch(function(){});
}

// Restore theme on load
(function(){
  let saved=localStorage.getItem('tera_pilot:theme');
  // v2.3.1: theme ids were renamed to drop third-party brand names
  // (old ids 'spacex'/'cursor' -> 'noir'/'flat'). Migrate any
  // previously-saved id so existing users keep their theme instead of
  // falling back.
  if(saved==='spacex'){saved='noir';localStorage.setItem('tera_pilot:theme','noir')}
  else if(saved==='cursor'){saved='flat';localStorage.setItem('tera_pilot:theme','flat')}
  // v2.3.0: Noir is the flagship default theme — applied whenever no
  // theme has been saved yet (fresh installs and users who never touched
  // Appearance). Previously-saved themes are always respected.
  if(saved)setTheme(saved); else setTheme('noir');
  const savedSize=localStorage.getItem('tera_pilot:textsize');
  if(savedSize&&savedSize!=='medium')document.documentElement.setAttribute('data-textsize',savedSize);
  const neuralSaved=localStorage.getItem('tera_pilot:neuralBg');
  if(neuralSaved!=='on'){state.neuralBgDisabled=true;var nb=document.querySelector('.neural-bg');if(nb)nb.style.opacity='0';var np=document.getElementById('neuralPixels');if(np)np.classList.remove('active');var sc=document.getElementById('synapseCanvas');if(sc){sc.style.display='none';sc.classList.remove('active')}}
})();

// ── Basic / Advanced UI mode (v2.3.0) ───────────────────────────────
// Basic mode is the default: it hides every .advanced-only control
// (agent/plan toggles, RAG, enhance, swarm, git bar, heavy-code/office/
// tools panes, token optimizations, router indicator) and the advanced
// Settings tabs, keeping the surface clean for almost everyone.
// Advanced mode exposes the full control surface. The choice persists
// in localStorage ('tera_pilot:uiMode').
function setUiMode(mode){
  mode=(mode==='advanced')?'advanced':'basic';
  document.documentElement.setAttribute('data-ui-mode',mode);
  try{localStorage.setItem('tera_pilot:uiMode',mode)}catch(e){}
  const btn=document.getElementById('uiModeToggle');
  if(btn){
    btn.setAttribute('data-mode',mode);
    btn.setAttribute('aria-pressed',String(mode==='advanced'));
    const label=document.getElementById('uiModeLabel');
    if(label)label.textContent=mode==='advanced'?'Advanced':'Basic';
    btn.title=mode==='advanced'
      ?'Advanced mode — full control surface. Click for Basic.'
      :'Basic mode — clean interface. Click for Advanced.';
  }
  // Hide advanced Settings tabs in basic mode.
  document.querySelectorAll('.modal-tab[data-advanced]').forEach(function(tab){
    tab.style.display=(mode==='advanced')?'':'none';
  });
  // If the active tab just got hidden, fall back to a visible one.
  const activeTab=document.querySelector('.modal-tab.active');
  if(activeTab&&activeTab.style.display==='none'){
    const fallback='appearance';
    document.querySelectorAll('.modal-tab').forEach(function(t){t.classList.toggle('active',t.dataset.tab===fallback)});
    renderSettingsTab(fallback).catch(function(){});
  }
}
(function initUiMode(){
  let saved='basic';
  try{saved=localStorage.getItem('tera_pilot:uiMode')||'basic'}catch(e){}
  setUiMode(saved);
  const btn=document.getElementById('uiModeToggle');
  if(btn){
    btn.addEventListener('click',function(){
      const next=document.documentElement.getAttribute('data-ui-mode')==='advanced'?'basic':'advanced';
      setUiMode(next);
      toast(next==='advanced'?'Advanced mode — full control surface':'Basic mode — clean interface','info');
    });
  }
})();

async function renderProvidersTab(body){
  let providers=state.providers;
  if(providers.length===0){providers=Object.entries(PROVIDER_META).map(([id,m])=>({id,label:m.label,model:m.model,api_key_set:false,temperature:0.2,max_tokens:4096,active:id===state.activeProvider}))}
  // v1.0.4: auto-router toggle at top of providers tab
  const arCard=document.createElement('div');arCard.className='provider-config-card';arCard.style.marginBottom='var(--s-16)';
  arCard.innerHTML=`<div class="provider-config-header"><div class="provider-config-name">Auto-Router <span style="font-size:10px;color:var(--text-muted);font-weight:400">Automatically pick the best model per task</span></div><div class="provider-config-actions"><button class="btn-secondary" id="arToggleBtn">${state.autoRoute?'Disable':'Enable'}</button></div></div><div class="provider-config-body"><div class="full" style="font-size:12px;color:var(--text-secondary);line-height:1.6">When enabled, Tera Pilot classifies your prompt complexity (trivial \u2192 expert) and routes to the optimal provider. A brief decision badge appears in the status bar showing the chosen model and estimated cost.</div><div id="arClassification" style="margin-top:var(--s-12);display:none;padding:var(--s-12);background:var(--bg-hover);border-radius:var(--r-sm);font-size:12px"></div></div>`;
  body.appendChild(arCard);
  document.getElementById('arToggleBtn').addEventListener('click',async()=>{if(!isBackendAvailable()){toast('Backend not connected','error');return}state.autoRoute=!state.autoRoute;try{await callBridge('toggle_auto_router',state.autoRoute);document.getElementById('arToggleBtn').textContent=state.autoRoute?'Disable':'Enable';toast('Auto-router '+(state.autoRoute?'enabled':'disabled'),'success')}catch(e){toast(e.message,'error')}});
  // Live classification preview as user types in composer
  const arClassEl=document.getElementById('arClassification');
  const updateArPreview=async()=>{const text=composerInput.value.trim();if(!text||!isBackendAvailable()){arClassEl.style.display='none';return}try{const info=await callBridge('classify_prompt',text);arClassEl.style.display='block';arClassEl.innerHTML=`<span style="color:var(--purple);font-weight:500">${info.complexity.toUpperCase()}</span> \u2014 ${escapeHtml(info.explanation)}${info.signals.length?'<br><span style="color:var(--text-muted)">'+info.signals.join(' · ')+'</span>':''}`}catch(e){arClassEl.style.display='none'}};
  let _arDebounce;composerInput.addEventListener('input',()=>{clearTimeout(_arDebounce);_arDebounce=setTimeout(updateArPreview,600)});
  // v2.2.1: Custom providers card — opens the unified Tools panel
  // (Providers tab) where the user can add Nvidia NIM, OpenAI-compatible
  // endpoints, and any other custom provider via a wizard.
  const cpCard=document.createElement('div');cpCard.className='provider-config-card';cpCard.style.marginBottom='var(--s-16)';
  cpCard.innerHTML=`<div class="provider-config-header"><div class="provider-config-name">Custom providers <span style="font-size:10px;color:var(--text-muted);font-weight:400">Nvidia NIM · OpenAI-compat · Ollama · LM Studio</span></div><div class="provider-config-actions"><button class="btn-primary" id="cpManageBtn">Manage →</button></div></div><div class="provider-config-body"><div class="full" style="font-size:12px;color:var(--text-secondary);line-height:1.6">Add your own providers without touching source code. Supports Nvidia NIM (free key at build.nvidia.com), any OpenAI-compatible endpoint, and local Ollama / LM Studio servers. Configurations are stored at <code style="font-family:var(--font-mono,monospace);font-size:11px;background:var(--bg-floating);padding:1px 5px;border-radius:3px">~/.tera_pilot/providers.yaml</code>.</div></div>`;
  body.appendChild(cpCard);
  document.getElementById('cpManageBtn').addEventListener('click',async function(){
    // v2.2.3: open Tools tab → providers sub-tab inside this same Settings modal
    // (previously this opened the separate Tools drawer)
    const toolsTab = document.querySelector('.modal-tab[data-tab="tools"]');
    if(toolsTab){ toolsTab.click(); }
    // After the Tools grid renders, click the providers card
    setTimeout(async function(){
      const card = document.querySelector('.settings-tool-card[data-tool="providers"]');
      if(card){ card.click(); }
    }, 80);
  });
  // v2.2.3: With 30+ providers, group them by category so the user can
  // scan the list quickly. Categories are derived from the structure of
  // PROVIDER_META — the same provider id appears in exactly one category.
  const PROVIDER_CATEGORIES = [
    { label: 'Local (no API key needed)', ids: ['lmstudio','ollama','vllm','koboldcpp','llamafile'] },
    { label: 'Major cloud providers',     ids: ['openai','anthropic','gemini','deepseek','zai','mistral','xai','cohere','perplexity','ai21'] },
    { label: 'Fast inference (open models hosted)', ids: ['groq','cerebras','sambanova'] },
    { label: 'Open-model hosting & aggregators', ids: ['openrouter','together','fireworks','novita','hyperbolic','lepton','siliconflow','friendli'] },
    { label: 'ML platforms / model hubs', ids: ['huggingface','replicate'] },
    { label: 'Enterprise cloud', ids: ['azure_openai','vertex_ai','bedrock'] },
    { label: 'Nvidia NIM', ids: ['nvidia_nim'] },
    { label: 'Generic / custom', ids: ['openai_compat'] },
  ];
  // Sort the providers list so they render in category order; any provider
  // not in a category lands at the end under "Other".
  const seen = new Set();
  const ordered = [];
  for(const cat of PROVIDER_CATEGORIES){
    for(const id of cat.ids){
      const p = providers.find(x => x.id === id);
      if(p && !seen.has(id)){ ordered.push({p, cat}); seen.add(id); }
    }
  }
  // Any providers not matched to a category go under "Other"
  const others = providers.filter(p => !seen.has(p.id));
  if(others.length){ ordered.push({cat:{label:'Other', ids:[]}, p:null, isOtherHeader:true}); for(const p of others) ordered.push({cat:{label:''}, p}); }
  let lastCat = null;
  for(const item of ordered){
    if(item.isOtherHeader){
      const lbl=document.createElement('div');lbl.className='provider-category-label';lbl.textContent='Other';body.appendChild(lbl);lastCat='Other';continue;
    }
    if(item.cat && item.cat.label && item.cat.label !== lastCat){
      const lbl=document.createElement('div');lbl.className='provider-category-label';lbl.textContent=item.cat.label;body.appendChild(lbl);lastCat=item.cat.label;
    }
    const p = item.p;
    if(!p) continue;
    const meta=PROVIDER_META[p.id]||{needsKey:true};
    const isActive=p.id===state.activeProvider;
    // v1.1.1: accordion — with 6-8 providers this used to be an endless
    // scroll of fully-expanded cards. Only the active provider (or one the
    // user just expanded) is open; the rest collapse to just the header.
    const card=document.createElement('div');card.className='provider-config-card accordion'+(isActive?' active open':'');card.dataset.id=p.id;
    card.innerHTML=`<div class="provider-config-header" data-toggle><span class="provider-accordion-chevron">\u25b8</span><div class="provider-config-name">${escapeHtml(p.label)} ${isActive?'<span class="provider-config-active">Active</span>':''}</div><div class="provider-config-actions"><button class="btn-secondary" data-action="activate" data-id="${p.id}" ${isActive?'disabled':''}>Set active</button><button class="btn-secondary" data-action="test" data-id="${p.id}">Test</button></div></div><div class="provider-config-body"><div class="full"><div class="field-label">Model</div><input class="field-input" data-field="model" data-id="${p.id}" value="${escapeHtml(p.model||'')}" placeholder="model name"></div>${meta.needsKey?`<div class="full"><div class="field-label">API Key ${p.api_key_set?'<span style="color:var(--success)">· set</span>':'<span style="color:var(--danger)">· not set</span>'}</div><input class="field-input password" data-field="api_key" data-id="${p.id}" placeholder="${p.api_key_set?'•••••••• (leave empty to keep)':'sk-...'}"></div>`:'<div class="full"><div class="field-label">No API key needed — runs on your machine.</div></div>'}${meta.keyUrl?`<div class="full" style="margin-top:2px"><a href="${meta.keyUrl}" target="_blank" rel="noopener" style="font-size:11px;color:var(--accent)">${meta.needsKey?'Get an API key →':'Download →'}</a>${meta.keyHint?` <span style="font-size:11px;color:var(--text-muted)">— ${escapeHtml(meta.keyHint)}</span>`:''}</div>`:''}</div><div class="field-status" id="test-status-${p.id}"></div>`;
    body.appendChild(card);
  }
  // Accordion toggle — clicking the header (but not its buttons) expands/
  // collapses that card. Multiple cards can be open at once; nothing
  // forces a single-open behavior since comparing configs side by side
  // is often useful.
  body.querySelectorAll('.provider-config-card.accordion .provider-config-header').forEach(header=>{
    header.addEventListener('click',(e)=>{
      if(e.target.closest('button'))return;
      header.closest('.provider-config-card').classList.toggle('open');
    });
  });
  body.querySelectorAll('[data-action="activate"]').forEach(btn=>{btn.addEventListener('click',async()=>{const id=btn.dataset.id;state.activeProvider=id;if(isBackendAvailable()){try{await callBridge('set_provider',id);toast(`Switched to ${PROVIDER_META[id].label}`)}catch(e){toast(e.message,'error')}}renderSettingsTab('providers');providerTriggerText.textContent=PROVIDER_META[id].statusLabel;statusProvider.textContent=PROVIDER_META[id].modelDisplay||PROVIDER_META[id].model})});
  body.querySelectorAll('[data-action="test"]').forEach(btn=>{btn.addEventListener('click',async(e)=>{e.stopPropagation();const id=btn.dataset.id;const status=document.getElementById('test-status-'+id);status.className='field-status testing';status.innerHTML='<span class="field-status-spinner"></span> Checking…';
    if(isBackendAvailable()){try{const r=await callBridge('health_check',id);if(r.ok){status.className='field-status ok';status.textContent='\u2713 Connected ('+r.latency_ms+'ms)'}else if(r.rate_limited){status.className='field-status warn';status.textContent='\u26A0 Rate limited'}else if(!r.key_valid){status.className='field-status warn';status.textContent='\u2717 Invalid API key'}else{status.className='field-status warn';status.textContent='\u2717 '+(r.error||'failed').slice(0,80)}}catch(e){status.className='field-status warn';status.textContent='\u2717 '+e.message}}
    else{status.className='field-status warn';status.textContent='Backend not connected'}
  })});
}



async function renderSnippetsTab(body){
  let snippets=[];
  if(isBackendAvailable()){try{snippets=await callBridge('list_snippets')}catch(e){}}
  state.snippets=snippets;
  {const _snipBadge=document.getElementById('navSnippetsBadge');if(_snipBadge)_snipBadge.textContent=snippets.length;}
  body.innerHTML=`
    <div class="settings-section">
      <div class="settings-section-title">Saved snippets <button class="btn-primary" id="newSnippetBtn" style="float:right">+ New</button></div>
      <div id="snippetsList"></div>
    </div>
    <div id="snippetEditor" style="display:none">
      <div class="settings-section">
        <div class="settings-section-title">Editor</div>
        <div style="margin-bottom:var(--s-12)"><input class="field-input" id="snippetName" placeholder="Snippet name"></div>
        <div style="margin-bottom:var(--s-12)"><input class="field-input" id="snippetLang" placeholder="Language (python, markdown, ...)"></div>
        <textarea class="field-input" id="snippetContent" placeholder="Snippet content…" style="min-height:160px;font-family:'JetBrains Mono',monospace;resize:vertical"></textarea>
        <div style="margin-top:var(--s-12);display:flex;gap:var(--s-8)"><button class="btn-primary" id="saveSnippetBtn">Save snippet</button><button class="btn-secondary" id="cancelSnippetBtn">Cancel</button></div>
      </div>
    </div>
  `;
  const list=document.getElementById('snippetsList');
  if(snippets.length===0){list.innerHTML='<div style="padding:var(--s-24);text-align:center;color:var(--text-muted);font-size:13px">No snippets yet. Click "+ New" to create one.</div>'}
  for(const s of snippets){
    const el=document.createElement('div');el.className='snippet-item';
    el.innerHTML=`<div style="flex:1"><div class="snippet-name">${escapeHtml(s.name)}</div><div class="snippet-preview">${escapeHtml(s.content.slice(0,100))}</div></div><span class="snippet-lang">${escapeHtml(s.language||'text')}</span><button class="btn-danger" data-del="${escapeHtml(s.name)}">Delete</button>`;
    el.addEventListener('click',(e)=>{if(e.target.closest('[data-del]'))return;document.getElementById('snippetEditor').style.display='';document.getElementById('snippetName').value=s.name;document.getElementById('snippetLang').value=s.language||'';document.getElementById('snippetContent').value=s.content});
    list.appendChild(el);
  }
  list.querySelectorAll('[data-del]').forEach(btn=>{btn.addEventListener('click',async(e)=>{e.stopPropagation();const name=btn.dataset.del;if(isBackendAvailable()){await callBridge('delete_snippet',name);toast('Deleted');renderSettingsTab('snippets')}})});
  document.getElementById('newSnippetBtn').addEventListener('click',()=>{document.getElementById('snippetEditor').style.display='';document.getElementById('snippetName').value='';document.getElementById('snippetLang').value='python';document.getElementById('snippetContent').value=''});
  document.getElementById('cancelSnippetBtn').addEventListener('click',()=>{document.getElementById('snippetEditor').style.display='none'});
  document.getElementById('saveSnippetBtn').addEventListener('click',async()=>{
    const name=document.getElementById('snippetName').value.trim();const lang=document.getElementById('snippetLang').value.trim();const content=document.getElementById('snippetContent').value;
    if(!name||!content){toast('Name and content required','error');return}
    if(isBackendAvailable()){try{await callBridge('save_snippet',name,content,lang);toast('Saved','success');renderSettingsTab('snippets')}catch(e){toast('Failed: '+e.message,'error')}}
  });
}

// v1.3.1: TERA_PILOT.md used to live behind its own icon in the sidebar rail.
// It's project-level config, not a chat action reached constantly, so it
// moved here — one less icon competing for attention in the rail, same
// editor underneath (openModal from the TERA_PILOT.md IIFE, exposed globally).
async function renderProjectTab(body){
  let status={};if(isBackendAvailable()){try{status=await callBridge('get_status')}catch(e){}}
  body.innerHTML=`
    <div class="settings-section">
      <div class="settings-section-title">Project Instructions</div>
      <div style="padding:var(--s-24);background:var(--bg-floating);border:1px solid var(--border);border-radius:var(--r-panel);font-size:13px;line-height:1.7;color:var(--text-secondary)">
        <div style="font-size:14px;font-weight:600;color:var(--text-primary);margin-bottom:var(--s-8)">TERA_PILOT.md — Collective Memory</div>
        <div style="color:var(--text-muted);margin-bottom:var(--s-16)">
          Persistent rules and lessons Tera Pilot reads at the start of every chat in this project:
          conventions, gotchas, anything worth remembering across sessions. Preserved even
          when you clear conversation history.
        </div>
        <div style="margin-bottom:var(--s-16)"><strong>Project:</strong> <code style="font-family:'JetBrains Mono',monospace;color:var(--info)">${escapeHtml(status.project||'none')}</code></div>
        <button class="btn-primary" id="projectOpenClaudeMd">Open TERA_PILOT.md editor</button>
      </div>
    </div>`;
  const openBtn=document.getElementById('projectOpenClaudeMd');
  if(openBtn)openBtn.addEventListener('click',()=>{closeSettings();if(window.__openClaudeMdModal)window.__openClaudeMdModal()});
}
async function renderAboutTab(body){
  let status={};if(isBackendAvailable()){try{status=await callBridge('get_status')}catch(e){}}
  body.innerHTML=`
    <div class="settings-section">
      <div class="settings-section-title">About Tera Pilot</div>
      <div style="padding:var(--s-24);background:var(--bg-floating);border:1px solid var(--border);border-radius:var(--r-panel);font-size:13px;line-height:1.8;color:var(--text-secondary)">
        <div style="font-size:18px;font-weight:600;color:var(--text-primary);margin-bottom:var(--s-8)">Tera Pilot v1.1.0</div>
        <div style="color:var(--text-muted);margin-bottom:var(--s-16)">A native, local-first AI workspace. Built for thinking.</div>
        <div><strong>Config:</strong> <code style="font-family:'JetBrains Mono',monospace;color:var(--info)">${escapeHtml(status.config_path||'~/.tera_pilot/config.json')}</code></div>
        <div><strong>Chats:</strong> <code style="font-family:'JetBrains Mono',monospace;color:var(--info)">${escapeHtml(status.chats_dir||'~/.tera_pilot/chats/')}</code></div>
        <div><strong>Project:</strong> <code style="font-family:'JetBrains Mono',monospace;color:var(--info)">${escapeHtml(status.project||'none')}</code></div>
        ${Array.isArray(state.templates)?`<div><strong>Templates:</strong> ${state.templates.length}</div>`:''}
        ${Array.isArray(state.skills)?`<div><strong>Skills:</strong> ${state.skills.length}</div>`:''}
        <div><strong>Snippets:</strong> ${status.snippets_count||0}</div>
        <div><strong>Auto-router:</strong> ${status.auto_route?'enabled':'disabled'}</div>
        <div style="margin-top:var(--s-16)"><button class="btn-primary" id="aboutCheckUpdate">Check for Updates</button> <span id="aboutUpdateStatus" style="font-size:11px;color:var(--text-muted);margin-left:var(--s-8)"></span></div>
      </div>
    </div>`;
  // Wire update check button
  const updateBtn=document.getElementById('aboutCheckUpdate');
  if(updateBtn){
    updateBtn.addEventListener('click',async()=>{
      const statusEl=document.getElementById('aboutUpdateStatus');
      statusEl.textContent='Checking...';
      if(isBackendAvailable()){
        try{
          const r=await callBridge('check_for_updates');
          if(r.ok){statusEl.textContent='Checking in background...';
            setTimeout(()=>{statusEl.textContent='Up to date'},5000)}
        }catch(e){statusEl.textContent='Check failed'}
      }else{
        statusEl.textContent='Update check requires Tera Pilot desktop app';
      }
    });
  }
  body.innerHTML+=`
    <div class="settings-section">
      <div class="settings-section-title">Keyboard shortcuts</div>
      <div style="padding:var(--s-16);background:var(--bg-floating);border:1px solid var(--border);border-radius:var(--r-panel);font-size:12px;color:var(--text-secondary);line-height:2">
        <div><span class="kbd">⌘</span> <span class="kbd">↵</span> Send message</div>
        <div><span class="kbd">⌘</span> <span class="kbd">K</span> Command palette</div>
        <div><span class="kbd">⌘</span> <span class="kbd">\\</span> Toggle code viewer</div>
        <div><span class="kbd">⌘</span> <span class="kbd">O</span> Open project</div>
        <div><span class="kbd">⌘</span> <span class="kbd">,</span> Settings</div>
        <div><span class="kbd">⌘</span> <span class="kbd">Q</span> Quit</div>
      </div>
    </div>
  `;
}

document.getElementById('modalSave').addEventListener('click',async()=>{
  const tab=state.activeModalTab;
  if(tab==='providers'){
    const providers={};
    document.querySelectorAll('#settingsBody .provider-config-card').forEach(card=>{const id=card.dataset.id;const fields={};card.querySelectorAll('[data-field]').forEach(inp=>{const f=inp.dataset.field;if(f==='temperature')fields[f]=parseFloat(inp.value);else if(f==='max_tokens')fields[f]=parseInt(inp.value);else fields[f]=inp.value});providers[id]=fields});
    const currentTheme=document.documentElement.getAttribute('data-theme')||'dark';
    if(isBackendAvailable()){try{const result=await callBridge('save_settings',{active_provider:state.activeProvider,providers:providers,ui:{theme:currentTheme}});if(result.ok){toast('Settings saved','success');closeSettings()}else toast(result.error||'Save failed','error')}catch(e){toast('Save failed: '+e.message,'error')}}else{toast('Saved (demo mode)','success');closeSettings()}
  }else{closeSettings()}
});

// ═══════════════════════════════════════════════════════════════
// v2.2.4: QUICK SETTINGS — simplified two-tier settings
// ═══════════════════════════════════════════════════════════════
// Only the top 6 most popular providers + API key + model + theme.
// An "Advanced…" button opens the full 8-tab settings modal.
const QUICK_PROVIDERS = [
  { id:'openai',    label:'OpenAI',       icon:'🟢', model:'gpt-4o',           keyUrl:'https://platform.openai.com/api-keys' },
  { id:'anthropic', label:'Anthropic',    icon:'🟠', model:'claude-sonnet-4',  keyUrl:'https://console.anthropic.com/settings/keys' },
  { id:'gemini',    label:'Google Gemini',icon:'🔵', model:'gemini-2.5-pro',   keyUrl:'https://aistudio.google.com/apikey' },
  { id:'deepseek',  label:'DeepSeek',     icon:'🟣', model:'deepseek-chat',    keyUrl:'https://platform.deepseek.com/api_keys' },
  { id:'groq',      label:'Groq',         icon:'⚡', model:'llama-3.3-70b',    keyUrl:'https://console.groq.com/keys' },
  { id:'ollama',    label:'Ollama (local)',icon:'🦙', model:'llama3.3',        keyUrl:'https://ollama.com/download', needsKey:false },
];

const quickSettingsModal = document.getElementById('quickSettingsModal');
const quickSettingsBackdrop = document.getElementById('quickSettingsBackdrop');

function openQuickSettings() {
  renderQuickSettings();
  quickSettingsModal.classList.add('open');
  quickSettingsBackdrop.classList.add('open');
}
function closeQuickSettings() {
  quickSettingsModal.classList.remove('open');
  quickSettingsBackdrop.classList.remove('open');
}

function renderQuickSettings() {
  const body = document.getElementById('quickSettingsBody');
  const activeId = state.activeProvider || 'openai';
  const providers = state.providers || [];
  const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';

  // Find current provider info
  const activeP = providers.find(p => p.id === activeId) || { id: activeId, model: '', api_key_set: false };
  const activeMeta = PROVIDER_META[activeId] || { needsKey: true };

  let html = '<div class="qs-section">';
  html += '<div class="qs-section-title">Provider</div>';
  html += '<div class="qs-provider-grid">';
  for (const p of QUICK_PROVIDERS) {
    const isActive = p.id === activeId;
    const pState = providers.find(x => x.id === p.id);
    const hasKey = pState && pState.api_key_set;
    html += `<button class="qs-provider-btn${isActive ? ' active' : ''}" data-qs-provider="${p.id}">`;
    html += `<span class="qs-provider-icon">${p.icon}</span>`;
    html += `<span class="qs-provider-name">${p.label}</span>`;
    if (p.needsKey !== false) {
      html += `<span class="qs-provider-key${hasKey ? ' set' : ''}">${hasKey ? '✓' : '—'}</span>`;
    }
    html += '</button>';
  }
  html += '</div></div>';

  // Model input
  html += '<div class="qs-section">';
  html += '<div class="qs-section-title">Model</div>';
  html += `<input class="field-input qs-model-input" id="qsModel" value="${escapeHtml(activeP.model || activeMeta.model || '')}" placeholder="e.g. gpt-4o, claude-sonnet-4">`;
  html += '</div>';

  // API key (only if provider needs one)
  if (activeMeta.needsKey !== false) {
    html += '<div class="qs-section">';
    html += `<div class="qs-section-title">API Key ${activeP.api_key_set ? '<span style="color:var(--success)">· set</span>' : '<span style="color:var(--danger)">· not set</span>'}</div>`;
    html += `<input class="field-input qs-key-input" id="qsApiKey" type="password" placeholder="${activeP.api_key_set ? '•••••••• (leave empty to keep)' : 'sk-...'}">`;
    const quickP = QUICK_PROVIDERS.find(x => x.id === activeId);
    if (quickP && quickP.keyUrl) {
      html += `<div class="qs-key-hint"><a href="${quickP.keyUrl}" target="_blank" rel="noopener">Get an API key →</a></div>`;
    }
    html += '</div>';
  }

  // Theme
  html += '<div class="qs-section">';
  html += '<div class="qs-section-title">Theme</div>';
  html += '<div class="qs-theme-toggle">';
  html += `<button class="qs-theme-btn${currentTheme === 'dark' || currentTheme === 'ember' ? ' active' : ''}" data-qs-theme="dark">🌙 Dark</button>`;
  html += `<button class="qs-theme-btn${currentTheme === 'light' || currentTheme === 'linen' ? ' active' : ''}" data-qs-theme="light">☀️ Light</button>`;
  html += `<button class="qs-theme-btn" data-qs-theme="auto">🖥 Auto</button>`;
  html += '</div></div>';

  body.innerHTML = html;

  // Provider selection
  body.querySelectorAll('.qs-provider-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.qsProvider;
      state.activeProvider = id;
      // Update model placeholder
      const meta = PROVIDER_META[id] || {};
      const modelInput = document.getElementById('qsModel');
      if (modelInput && !modelInput.value) {
        modelInput.value = meta.model || '';
      }
      renderQuickSettings();
    });
  });

  // Theme buttons
  body.querySelectorAll('.qs-theme-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const theme = btn.dataset.qsTheme;
      if (theme === 'auto') {
        const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
        setTheme(prefersDark ? 'dark' : 'light');
      } else {
        setTheme(theme);
      }
      renderQuickSettings();
    });
  });
}

// Wire up Quick Settings buttons
document.getElementById('openSettingsBtn').removeEventListener('click', ()=>openSettings('providers'));
document.getElementById('openSettingsBtn').addEventListener('click', openQuickSettings);
document.getElementById('quickSettingsClose').addEventListener('click', closeQuickSettings);
quickSettingsBackdrop.addEventListener('click', closeQuickSettings);
document.getElementById('quickSettingsAdvanced').addEventListener('click', () => {
  closeQuickSettings();
  openSettings('providers');
});
document.getElementById('quickSettingsSave').addEventListener('click', async () => {
  const activeId = state.activeProvider || 'openai';
  const model = document.getElementById('qsModel')?.value || '';
  const apiKey = document.getElementById('qsApiKey')?.value || '';
  const currentTheme = document.documentElement.getAttribute('data-theme') || 'dark';

  const providers = {};
  providers[activeId] = { model: model };
  if (apiKey) providers[activeId].api_key = apiKey;

  if (isBackendAvailable()) {
    try {
      const result = await callBridge('save_settings', {
        active_provider: activeId,
        providers: providers,
        ui: { theme: currentTheme }
      });
      if (result.ok) {
        toast('Settings saved', 'success');
        // Update UI
        const meta = PROVIDER_META[activeId] || {};
        if (providerTriggerText) providerTriggerText.textContent = meta.statusLabel || meta.label;
        if (statusProvider) statusProvider.textContent = meta.modelDisplay || meta.model || model;
        closeQuickSettings();
      } else {
        toast(result.error || 'Save failed', 'error');
      }
    } catch (e) {
      toast('Save failed: ' + e.message, 'error');
    }
  } else {
    toast('Saved (demo mode)', 'success');
    closeQuickSettings();
  }
});

// Open project
document.getElementById('openProjectBtn').addEventListener('click', async () => {
  if (!isBackendAvailable()) {
    toast('Backend not connected', 'error');
    return;
  }

  // Use the native file picker via backend (tkinter) which gives full paths
  try {
    const result = await callBridge('native_file_picker');
    if (result && result.ok && result.path) {
      await openProject(result.path);
    } else if (result && result.error) {
      toast('File picker error: ' + result.error, 'error');
    } else if (result && result.cancelled) {
      // User cancelled, do nothing
    } else {
      // Fallback to manual path entry modal
      showOpenProjectModal();
    }
  } catch (e) {
    console.warn('Native file picker failed:', e);
    // Fallback to manual path entry modal
    showOpenProjectModal();
  }
});

function showOpenProjectModal() {
  // Create a simple modal for path input
  const modal = document.createElement('div');
  modal.className = 'modal-overlay';
  modal.innerHTML = `
    <div class="modal" style="max-width:500px">
      <div class="modal-header">
        <h3>Open Project</h3>
        <button class="modal-close" id="openProjectModalClose">&times;</button>
      </div>
      <div class="modal-body">
        <p style="color:var(--text-secondary);margin-bottom:12px;font-size:13px">
          Enter the full path to your project directory, or use the file picker below.
        </p>
        <div style="display:flex;gap:8px;margin-bottom:12px">
          <input type="text" id="openProjectPathInput" class="input" placeholder="/path/to/project" style="flex:1" />
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn btn-ghost" id="openProjectCancel">Cancel</button>
          <button class="btn btn-primary" id="openProjectConfirm">Open</button>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  const input = modal.querySelector('#openProjectPathInput');
  const close = () => modal.remove();

  modal.querySelector('#openProjectModalClose').addEventListener('click', close);
  modal.querySelector('#openProjectCancel').addEventListener('click', close);
  modal.querySelector('#openProjectConfirm').addEventListener('click', async () => {
    const path = input.value.trim();
    if (!path) {
      toast('Please enter a path', 'error');
      return;
    }
    close();
    await openProject(path);
  });

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      modal.querySelector('#openProjectConfirm').click();
    } else if (e.key === 'Escape') {
      close();
    }
  });

  input.focus();
}

async function openProject(path) {
  try {
    const result = await callBridge('open_project', { path });
    if (result.ok) {
      toast('Project opened: ' + path, 'success');
      // Refresh the project breadcrumb and file tree
      state.projectRoot = path;
      const breadcrumb = document.getElementById('projectBreadcrumb');
      if (breadcrumb) {
        breadcrumb.textContent = path;
        breadcrumb.style.display = 'inline-block';
      }
      // Reload file tree if open
      if (document.getElementById('codeViewer')?.classList.contains('open')) {
        refreshFileTree();
      }
      // Reload chat list since it's project-specific
      loadChats();
    } else {
      toast('Failed to open project: ' + (result.error || 'Unknown error'), 'error');
    }
  } catch (e) {
    toast('Failed to open project: ' + e.message, 'error');
  }
}

// Code Viewer
const codeViewer=document.getElementById('codeViewer');const cvToggle=document.getElementById('cvToggle');const cvTree=document.getElementById('cvTree');const cvTabs=document.getElementById('cvTabs');const cvCode=document.getElementById('cvCode');const cvBreadcrumb=document.getElementById('cvBreadcrumb');const cvCount=document.getElementById('cvCount');

async function refreshFileTree(){
  cvTree.innerHTML='';if(isBackendAvailable()&&state.projectRoot){try{const files=await callBridge('list_files');state.files.app=files.filter(f=>f.section!=='Root'&&f.section!=='Tests');state.files.tests=files.filter(f=>f.section==='Tests');state.files.root=files.filter(f=>f.section==='Root')}catch(e){console.warn('list_files failed',e)}}
  const sections=[{label:'App',files:state.files.app},{label:'Tests',files:state.files.tests},{label:'Root',files:state.files.root}];
  let total=0;sections.forEach(sec=>{if(sec.files.length===0)return;const sl=document.createElement('div');sl.className='cv-tree-section';sl.textContent=sec.label;cvTree.appendChild(sl);sec.files.forEach(f=>{total++;const el=document.createElement('div');el.className='cv-file'+(f.status?' '+f.status:'');el.dataset.path=f.path;el.innerHTML=`${fileTypeBadge(f.name)}${escapeHtml(f.name)}`;el.addEventListener('click',()=>openFile(f.path));cvTree.appendChild(el)})});cvCount.textContent=total;cvCount.style.display=total>0?'':'none';
}

async function openFile(path){
  if(state.openTabs.has(path)){setActiveTab(path);return}
  let content='';if(isBackendAvailable()){try{const result=await callBridge('read_file',path);if(result.exists){content=result.content}else{toast('File not found: '+path,'error');return}}catch(e){toast('Read failed: '+e.message,'error');return}}
  const name=path.split('/').pop();state.openTabs.set(path,{name,content});renderTabs();setActiveTab(path);
  document.querySelectorAll('.cv-file').forEach(f=>f.classList.remove('active'));const treeEl=document.querySelector(`.cv-file[data-path="${CSS.escape(path)}"]`);if(treeEl)treeEl.classList.add('active');
}
function renderTabs(){cvTabs.innerHTML='';state.openTabs.forEach((tab,path)=>{const el=document.createElement('div');el.className='cv-tab'+(path===state.activeTab?' active':'');el.dataset.path=path;el.innerHTML=`${escapeHtml(tab.name)}<span class="cv-tab-close" data-close="${path}"><svg width="8" height="8" viewBox="0 0 8 8" fill="none"><path d="M1 1l6 6M7 1l-6 6" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/></svg></span>`;el.addEventListener('click',(e)=>{if(e.target.closest('[data-close]')){e.stopPropagation();closeTab(path)}else{setActiveTab(path)}});cvTabs.appendChild(el)})}
function setActiveTab(path){state.activeTab=path;renderTabs();renderCode(path);document.querySelectorAll('.cv-file').forEach(f=>f.classList.remove('active'));const treeEl=document.querySelector(`.cv-file[data-path="${CSS.escape(path)}"]`);if(treeEl)treeEl.classList.add('active')}
function closeTab(path){state.openTabs.delete(path);if(state.activeTab===path){state.activeTab=state.openTabs.keys().next().value||null}renderTabs();if(state.activeTab)renderCode(state.activeTab);else{cvCode.innerHTML=`<div class="cv-empty"><svg class="cv-empty-icon" viewBox="0 0 24 24" fill="none"><path d="M8 6l-4 6 4 6M16 6l4 6-4 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg><div>No file open.</div></div>`;cvBreadcrumb.innerHTML='<span style="color:var(--text-muted)">No file selected</span><div class="cv-status"></div>'}}
function renderCode(path){const tab=state.openTabs.get(path);if(!tab)return;const parts=path.split('/');cvBreadcrumb.innerHTML=`${parts.map((p,i)=>i===parts.length-1?`<span style="color:var(--text-secondary)">${escapeHtml(p)}</span>`:`<span>${escapeHtml(p)}</span><span class="sep">/</span>`).join('')}<div class="cv-status"><span style="color:var(--text-muted);font-size:10px">${tab.content.split('\n').length} lines</span></div>`;const lines=tab.content.split('\n');const highlighted=highlight(tab.content,path);const highlightedLines=highlighted.split('\n');cvCode.innerHTML=lines.map((_,i)=>`<div class="cv-line"><div class="cv-line-num">${i+1}</div><div class="cv-line-content">${highlightedLines[i]||''}</div></div>`).join('')}

function highlightPython(code){const kw=/\b(?:from|import|def|class|return|if|elif|else|for|while|try|except|finally|with|as|yield|async|await|lambda|pass|raise|in|not|and|or|is|None|True|False|self|cls)\b/g;const esc=code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');return esc.replace(/(#[^\n]*)/g,'<span class="tok-comment">$1</span>').replace(/("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')/g,'<span class="tok-string">$1</span>').replace(/^(\s*@[\w.]+)/gm,'<span class="tok-decorator">$1</span>').replace(kw,'<span class="tok-keyword">$&</span>').replace(/\b(\d+(?:\.\d+)?)\b/g,'<span class="tok-number">$1</span>').replace(/\b(def|class)\s+(\w+)/g,'<span class="tok-keyword">$1</span> <span class="tok-$1">$2</span>')}
function highlightMarkdown(code){const esc=code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');return esc.replace(/^(#{1,6}\s[^\n]+)/gm,'<span class="tok-class">$1</span>').replace(/(\*\*[^*]+\*\*)/g,'<span class="tok-keyword">$1</span>').replace(/(`[^`]+`)/g,'<span class="tok-string">$1</span>').replace(/^(\s*[-*]\s)/gm,'<span class="tok-decorator">$1</span>')}
function highlightToml(code){const esc=code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');return esc.replace(/(^|\n)(\[[^\]]+\])/g,'$1<span class="tok-class">$2</span>').replace(/(^|\n)([\w_.-]+)(\s*=)/g,'$1<span class="tok-keyword">$2</span>$3').replace(/("[^"]*")/g,'<span class="tok-string">$1</span>')}
function highlight(code,path){if(path.endsWith('.py'))return highlightPython(code);if(path.endsWith('.md'))return highlightMarkdown(code);if(path.endsWith('.toml'))return highlightToml(code);return code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}

function openViewer(){codeViewer.classList.add('open');cvToggle.classList.add('hidden');refreshFileTree();if(!state.activeTab&&state.files.app.length>0){openFile(state.files.app[0].path)}}
function closeViewer(){codeViewer.classList.remove('open','wide');cvToggle.classList.remove('hidden')}
cvToggle.addEventListener('click',openViewer);
document.getElementById('cvClose').addEventListener('click',closeViewer);
document.getElementById('cvWide').addEventListener('click',()=>{codeViewer.classList.toggle('wide');document.getElementById('cvWide').classList.toggle('active')});
document.getElementById('cvRefresh').addEventListener('click',()=>{refreshFileTree();toast('Files refreshed')});


var fileTreePanel = document.getElementById('fileTreePanel');
var fileTreeToggleBtn = document.getElementById('fileTreeToggle');
var ftpTree = document.getElementById('ftpTree');
var ftpEditor = document.getElementById('ftpEditor');
var ftpBreadcrumb = document.getElementById('ftpBreadcrumb');
var ftpCode = document.getElementById('ftpCode');

function toggleFileTreePanel(){
  if(!fileTreePanel) return;
  state.fileTreePanelOpen = !state.fileTreePanelOpen;
  fileTreePanel.classList.toggle('open', state.fileTreePanelOpen);
  document.querySelector('.app').classList.toggle('file-tree-visible', state.fileTreePanelOpen);
  if(fileTreeToggleBtn) fileTreeToggleBtn.classList.toggle('active', state.fileTreePanelOpen);
  if(state.fileTreePanelOpen && state.projectRoot) refreshFileTreePanel();
  localStorage.setItem('tera_pilot:fileTreePanel', state.fileTreePanelOpen ? 'on' : 'off');
}

function refreshFileTreePanel(){
  if(!ftpTree || !isBackendAvailable()) return;
  ftpTree.innerHTML = '';
  callBridge('list_files').then(function(files){
    var app = files.filter(function(f){return f.section!=='Root'&&f.section!=='Tests'});
    var tests = files.filter(function(f){return f.section==='Tests'});
    var root = files.filter(function(f){return f.section==='Root'});
    [['App',app],['Tests',tests],['Root',root]].forEach(function(sec){
      if(sec[1].length===0) return;
      var sl = document.createElement('div'); sl.className = 'ftp-tree-section'; sl.textContent = sec[0]; ftpTree.appendChild(sl);
      sec[1].forEach(function(f){
        var el = document.createElement('div'); el.className = 'ftp-file'; el.dataset.path = f.path;
        el.innerHTML = fileTypeBadge(f.name) + escapeHtml(f.name);
        el.addEventListener('click', function(){ openFtpFile(f.path); });
        ftpTree.appendChild(el);
      });
    });
  }).catch(function(e){});
}

function openFtpFile(path){
  if(!isBackendAvailable()) return;
  callBridge('read_file', path).then(function(result){
    if(!result.exists){ toast('File not found','error'); return; }
    if(ftpEditor){ ftpEditor.style.display = 'flex'; ftpEditor.style.flexDirection = 'column'; ftpEditor.style.flex = '1'; ftpEditor.style.overflow = 'hidden'; }
    if(ftpBreadcrumb){ var parts = path.split('/'); ftpBreadcrumb.innerHTML = parts.map(function(p,i){ return i===parts.length-1 ? '<span style="color:var(--text-secondary)">'+escapeHtml(p)+'</span>' : '<span>'+escapeHtml(p)+'</span><span style="color:var(--text-muted);margin:0 4px">/</span>'; }).join(''); }
    if(ftpCode){ var lines = result.content.split('\n'); var h = highlight(result.content, path); var hl = h.split('\n'); ftpCode.innerHTML = lines.map(function(_,i){ return '<div style="display:flex"><div style="width:40px;text-align:right;padding-right:12px;color:var(--text-muted);font-size:11px;user-select:none;flex-shrink:0;border-right:1px solid var(--border);margin-right:12px">'+(i+1)+'</div><div style="flex:1;white-space:pre-wrap;word-break:break-all">'+(hl[i]||'')+'</div></div>'; }).join(''); }
    ftpTree.querySelectorAll('.ftp-file').forEach(function(el){ el.classList.remove('active'); });
    var te = ftpTree.querySelector('.ftp-file[data-path="'+CSS.escape(path)+'"]'); if(te) te.classList.add('active');
  }).catch(function(e){ toast('Read failed','error'); });
}

if(fileTreeToggleBtn){
  fileTreeToggleBtn.addEventListener('click', toggleFileTreePanel);
  if(localStorage.getItem('tera_pilot:fileTreePanel')==='on'){ state.fileTreePanelOpen=true; fileTreePanel.classList.add('open'); document.querySelector('.app').classList.add('file-tree-visible'); fileTreeToggleBtn.classList.add('active'); }
}
var _ftpRefreshEl = document.getElementById('ftpRefresh');
if(_ftpRefreshEl) _ftpRefreshEl.addEventListener('click', function(){ refreshFileTreePanel(); toast('Files refreshed'); });
var _ftpCollapseEl = document.getElementById('ftpCollapse');
if(_ftpCollapseEl) _ftpCollapseEl.addEventListener('click', toggleFileTreePanel);

// Activity
function showActivity(){activitySteps.innerHTML='';activityPanel.classList.add('visible');statusbar.classList.add('visible')}
function hideActivity(){activityPanel.classList.remove('visible');statusbar.classList.remove('visible')}
function addActivityStep(step){const el=document.createElement('div');el.className='activity-step active';el.innerHTML=`<div class="activity-step-icon"></div><div class="activity-step-label">${escapeHtml(step.label||'')}<div class="activity-step-type">${escapeHtml(step.detail||step.type||'')}</div></div>`;activitySteps.appendChild(el);activitySteps.scrollTop=activitySteps.scrollHeight;setTimeout(()=>{el.classList.remove('active');el.classList.add('done')},600)}
document.getElementById('activityClose').addEventListener('click',()=>{activityPanel.classList.remove('visible')});
document.getElementById('undoAgentBtn').addEventListener('click',async()=>{if(!isBackendAvailable()){toast('Backend not connected','error');return}try{const r=await callBridge('undo_last_agent');if(r.ok){toast('Agent changes undone ('+r.method+')','success');refreshFileTree()}else{toast(r.error||'Undo failed','error')}}catch(e){toast(e.message,'error')}});

// Command palette
const cmdPalette=document.getElementById('cmdPalette');const cmdInput=document.getElementById('cmdInput');const cmdResults=document.getElementById('cmdResults');
let cmdSelectedIndex=0;let cmdItems=[];

function openCommandPalette(){cmdPalette.classList.add('open');cmdInput.value='';cmdInput.focus();renderCommandResults('')}
function closeCommandPalette(){cmdPalette.classList.remove('open')}
function getCommandItems(){const items=[{group:'Actions',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',title:'New chat',desc:'Start a fresh conversation',action:()=>newChat()},{group:'Actions',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 3h5v5H2V3ZM9 3h5v5H9V3ZM2 9h5v5H2V9ZM9 9h5v5H9V9Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',title:'Open chat catalog',desc:'Browse all chats, projects, and tags',action:()=>{closeCommandPalette();openAllChats()}},{group:'Actions',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="2" stroke="currentColor" stroke-width="1.5"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',title:'Open settings',desc:'Configure providers, models, inference',shortcut:'⌘,',action:()=>{closeCommandPalette();openSettings('providers')}},{group:'Actions',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 4h4l1.5 1.5H14V13H2V4Z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>',title:'Open project',desc:'Open a folder for code viewer',shortcut:'⌘O',action:()=>{closeCommandPalette();toast('Press ⌘+O to open a folder')}},{group:'Actions',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M5 3l-3 4 3 4M11 3l3 4-3 4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',title:'Toggle code viewer',desc:'Show/hide the file browser',shortcut:'⌘\\',action:()=>{closeCommandPalette();if(codeViewer.classList.contains('open'))closeViewer();else openViewer()}},{group:'Actions',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M3 8l3 3 7-7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',title:'Enhance prompt',desc:'Restructure the current prompt',action:()=>{closeCommandPalette();document.getElementById('enhanceBtn').click()}}];
  // Providers
  for(const [id,m] of Object.entries(PROVIDER_META)){items.push({group:'Switch provider',icon:`<div style="width:8px;height:8px;border-radius:50%;background:var(--accent)"></div>`,title:m.label,desc:m.statusLabel,action:()=>{closeCommandPalette();state.activeProvider=id;providerTriggerText.textContent=m.statusLabel;statusProvider.textContent=m.modelDisplay||m.model;if(isBackendAvailable())callBridge('set_provider',id);toast(`Switched to ${m.label}`)}})}
  // Templates
  for(const t of state.templates){items.push({group:'Templates',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',title:t.name,desc:t.desc,action:()=>{closeCommandPalette();insertTemplateIntoComposer(t.id,t.name)}})}
  // Skills
  for(const s of state.skills){items.push({group:'Skills',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 2l2 4 4 1-3 3 1 4-4-2-4 2 1-4-3-3 4-1 2-4Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',title:s.name,desc:s.desc,action:()=>{closeCommandPalette();state.activeSkill=s.id;skillChipText.textContent='Skill · '+s.name;skillChip.style.display='inline-flex';toast(`Skill: ${s.name}`)}})}
  // Chats
  for(const c of state.chats.slice(0,10)){items.push({group:'Recent chats',icon:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M2 4h12v8H4l-2 2V4Z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/></svg>',title:c.title,desc:`${c.message_count} messages`,action:()=>{closeCommandPalette();loadChat(c.id)}})}
  return items;
}
function renderCommandResults(query){
  const all=getCommandItems();
  const q=query.toLowerCase().trim();
  cmdItems=q?all.filter(i=>i.title.toLowerCase().includes(q)||i.desc.toLowerCase().includes(q)||i.group.toLowerCase().includes(q)):all;
  cmdSelectedIndex=0;
  cmdResults.innerHTML='';
  if(cmdItems.length===0){cmdResults.innerHTML='<div style="padding:var(--s-24);text-align:center;color:var(--text-muted);font-size:13px">No results</div>';return}
  let lastGroup='';
  cmdItems.forEach((item,i)=>{
    if(item.group!==lastGroup){const label=document.createElement('div');label.className='cmd-group-label';label.textContent=item.group;cmdResults.appendChild(label);lastGroup=item.group}
    const el=document.createElement('div');el.className='cmd-item'+(i===cmdSelectedIndex?' selected':'');
    el.innerHTML=`<div class="cmd-item-icon">${item.icon}</div><div class="cmd-item-text"><div class="cmd-item-title">${escapeHtml(item.title)}</div><div class="cmd-item-desc">${escapeHtml(item.desc)}</div></div>${item.shortcut?`<span class="cmd-item-shortcut">${item.shortcut}</span>`:''}`;
    el.addEventListener('click',()=>{item.action()});
    el.addEventListener('mouseenter',()=>{cmdSelectedIndex=i;updateCmdSelection()});
    cmdResults.appendChild(el);
  });
}
function updateCmdSelection(){document.querySelectorAll('.cmd-item').forEach((el,i)=>el.classList.toggle('selected',i===cmdSelectedIndex))}
cmdInput.addEventListener('input',()=>renderCommandResults(cmdInput.value));
cmdInput.addEventListener('keydown',(e)=>{
  if(e.key==='Escape'){e.preventDefault();closeCommandPalette()}
  else if(e.key==='ArrowDown'){e.preventDefault();cmdSelectedIndex=Math.min(cmdSelectedIndex+1,cmdItems.length-1);updateCmdSelection();scrollCmdIntoView()}
  else if(e.key==='ArrowUp'){e.preventDefault();cmdSelectedIndex=Math.max(cmdSelectedIndex-1,0);updateCmdSelection();scrollCmdIntoView()}
  else if(e.key==='Enter'){e.preventDefault();if(cmdItems[cmdSelectedIndex])cmdItems[cmdSelectedIndex].action()}
});
function scrollCmdIntoView(){const sel=cmdResults.querySelector('.cmd-item.selected');if(sel)sel.scrollIntoView({block:'nearest'})}

// Keyboard shortcuts
document.addEventListener('keydown',(e)=>{
  if((e.metaKey||e.ctrlKey)&&e.key==='\\'){e.preventDefault();if(codeViewer.classList.contains('open'))closeViewer();else openViewer()}
  if((e.metaKey||e.ctrlKey)&&e.key==='k'){e.preventDefault();if(cmdPalette.classList.contains('open'))closeCommandPalette();else openCommandPalette()}
  if((e.metaKey||e.ctrlKey)&&e.key===','){e.preventDefault();openSettings('providers')}
  if(e.key==='Escape'){if(cmdPalette.classList.contains('open'))closeCommandPalette();if(modal.classList.contains('open'))closeSettings();var acm=document.getElementById('allChatsModal');if(acm&&acm.classList.contains('open'))closeAllChats();if(usageModal.classList.contains('open'))closeUsage()}
});

// Bridge wiring
// v1.1.4-fix (bug C-API-7): GET /api/plugins/inject existed in api_server.py
// (and PluginManager.inject_js/inject_css was documented as a public plugin
// API, see plugins/_example.py) but nothing in the frontend ever fetched it
// — a plugin's inject_js()/inject_css() never actually reached the page.
var __pluginAssetsLoaded=false;
function _loadPluginAssets(){
  if(__pluginAssetsLoaded||!window.__apiBase)return;
  __pluginAssetsLoaded=true;
  fetch(window.__apiBase+'/api/plugins/inject',{headers:_apiHeaders()})
    .then(function(r){return r.ok?r.json():null})
    .then(function(data){
      if(!data)return;
      if(data.css){
        var styleEl=document.createElement('style');
        styleEl.id='tera-pilot-plugin-css';
        styleEl.textContent=data.css;
        document.head.appendChild(styleEl);
      }
      if(data.js){
        var scriptEl=document.createElement('script');
        scriptEl.id='tera-pilot-plugin-js';
        scriptEl.textContent=data.js;
        document.body.appendChild(scriptEl);
      }
    })
    .catch(function(e){console.warn('[tera_pilot] plugin asset load failed',e)});
}
window.__teraPilotReady=function(status){console.log('[tera_pilot] backend ready',status);if(status.api_base)window.__apiBase=status.api_base;else if(status.api_port)window.__apiBase='http://127.0.0.1:'+status.api_port;if(status.api_token)window.__apiToken=status.api_token;if(status.project){state.projectRoot=status.project;updateProjectBreadcrumb();updateAgentModeToggleUI()}_loadPluginAssets()};

// v1.0.4: called from main_window.py after Ctrl+O picks a new project folder
window.__teraPilotProjectOpened=function(result){
  console.log('[tera_pilot] project opened',result);
  state.projectRoot=result.root;
  state.openTabs.clear();
  state.activeTab=null;
  renderTabs();
  updateProjectBreadcrumb();
  updateAgentModeToggleUI();
  refreshFileTree();
  if(!codeViewer.classList.contains('open'))openViewer();
  if(state.fileTreePanelOpen)refreshFileTreePanel();
  cvCode.innerHTML='<div class="cv-empty"><svg class="cv-empty-icon" viewBox="0 0 24 24" fill="none"><path d="M8 6l-4 6 4 6M16 6l4 6-4 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg><div>No file open.</div></div>';
  cvBreadcrumb.innerHTML='<span style="color:var(--text-muted)">No file selected</span><div class="cv-status"></div>';
  toast('Project opened: '+result.root.split('/').slice(-1)[0],'success');
};

window.addEventListener('tera_pilot:bridge_ready',async()=>{
  console.log('[tera_pilot] bridge connected — wiring signals');

  // ── Wire signals in their own try/catch so a single bad signal
  //    doesn't prevent the rest of init (chat list, providers, etc.) ──
  try{
  window.bridge.token_streamed.connect(function(token){streamToken(token)});
  window.bridge.agent_step.connect(function(step){addActivityStep(step);if(step.label)statusContext.textContent=step.label});
  window.bridge.agent_done.connect(function(result){finalizeMessage(result);if(result.text){const msgs=chatView.querySelectorAll('.msg');const last=msgs[msgs.length-1];if(last){const body=last.querySelector('.msg-body');if(body.textContent.length<result.text.length)body.textContent=result.text}}debouncedRefreshChatList();_maybeAutoTitle();refreshContextIndicator()});
  window.bridge.agent_error.connect(function(err){toast(err,'error');finalizeMessage(null);statusContext.innerHTML='<span class="err">Error</span>'});
  // v1.0.5: Wire agent-specific signals (used by send_agent_message / tool-use mode)
  // v1.0.8: THOUGHT and PLAN_CREATED events now stream their text INTO the
  // chat message body (not just the activity panel). This fixes the "agent
  // writes file but shows nothing in chat" problem — the user now sees the
  // agent's reasoning and plan in real time, then the final summary.
  try{
    window.bridge.agent_step_signal.connect(function(step){
      addActivityStep(step);
      if(step.label) statusContext.textContent = step.label;
      if(window.__onVerifyEvent) window.__onVerifyEvent(step);
      // v1.1.3-fix: the v1.1.1 "structured blocks" renderer
      // (pushAgentBlock/renderBlocks) is suspected of silently failing to
      // reach the screen in the real WebEngine/QWebChannel runtime (the
      // backend logs show every step firing, but the message body stays
      // empty until the page is reloaded). Reverted to the simpler,
      // previously-proven appendAgentText mechanism used before v1.1.1.
      if(step.type === 'thought' && step.thought){
        appendAgentText(step.thought);
      } else if(step.type === 'plan_created' && step.plan){
        appendAgentText('## Plan\n' + step.plan);
        if(window.__onPlanCreated) window.__onPlanCreated(step.plan);
      } else if(step.type === 'tool_called' && step.tool){
        var toolLine = '→ ' + step.tool;
        if(step.args && step.args.path) toolLine += ' ' + step.args.path;
        if(step.write_intent) toolLine = '[WRITE_FILE] ' + step.write_intent + '\n' + toolLine;
        appendAgentText(toolLine);
      } else if(step.type === 'tool_result' && step.tool){
        appendAgentText('  ✓ ' + step.tool + ' done');
      }
      if(step.detail === 'tool_result' || step.tool){ refreshFileTree(); }
    });
    window.bridge.agent_tool_result.connect(function(data){if(data&&data.tool)refreshFileTree()});
    window.bridge.agent_final.connect(function(result){
      if(window.__onVerifyReset) window.__onVerifyReset();
      // v1.0.8: the final answer replaces any streamed provisional text
      // v1.1.2-fix: use real token counts from the backend instead of
      // the iteration count (previously result.iterations was passed as
      // "tokens", showing e.g. "5 tokens" for 5 iterations).
      var totalTok = (result.tokens_in||0) + (result.tokens_out||0);
      console.log('[tera_pilot] agent_final — tokens_in='+result.tokens_in+' tokens_out='+result.tokens_out+' total='+totalTok+' iterations='+result.iterations);
      finalizeMessage({text:result.text||'',tokens: totalTok || result.iterations||0});
      debouncedRefreshChatList();_maybeAutoTitle();refreshFileTree();refreshContextIndicator();
      if(result.error){toast('Agent error: '+result.error,'error')}
    });
  }catch(agentSigErr){console.warn('[tera_pilot] agent signal wiring (non-fatal):',agentSigErr)}
  window.bridge.file_changed.connect(function(path,type){refreshFileTree()});
  window.bridge.provider_changed.connect(function(id,info){state.activeProvider=id;providerTriggerText.textContent=(PROVIDER_META[id]||{}).statusLabel||id;statusProvider.textContent=(PROVIDER_META[id]||{}).modelDisplay||(PROVIDER_META[id]||{}).model||id});
  window.bridge.chat_list_changed.connect(function(){debouncedRefreshChatList()});
  window.bridge.chat_saved.connect(function(meta){debouncedRefreshChatList()});
  window.bridge.settings_saved.connect(function(settings){if(settings.providers){state.providers=Object.entries(settings.providers).map(([id,p])=>({id,label:(PROVIDER_META[id]||{}).label||id,model:p.model,api_key_set:p.api_key_set,temperature:p.temperature,max_tokens:p.max_tokens,active:id===settings.active_provider}))}if(settings.theme){setTheme(settings.theme)}});
  window.bridge.oneshot_done.connect(function(result){const rid=result.request_id||"";if(rid.startsWith("enhance_")){composerInput.value=result.text;autosize();composerStatus.textContent="Ready";toast("Prompt enhanced","success")}else if(rid.startsWith("test_")){const pending=window.__pendingTests||{};if(pending[rid]){const stEl=pending[rid].statusEl;stEl.className="field-status ok";stEl.textContent="\u2713 "+result.text;delete pending[rid]}}else if(rid.startsWith("commit_msg_")){var msg=(result.text||"").trim();if(msg){toast("AI suggests: "+msg.slice(0,120),"success");composerStatus.textContent="Ready"}else{composerStatus.textContent="Ready"}}});
  window.bridge.oneshot_error.connect(function(err){const [rid,...msgParts]=err.split(':');const msg=msgParts.join(':');if(rid.startsWith('enhance_')){composerStatus.textContent='Ready';toast('Enhance failed: '+msg,'error')}else if(rid.startsWith('test_')){const pending=window.__pendingTests||{};if(pending[rid]){const {statusEl}=pending[rid];statusEl.className='field-status warn';statusEl.textContent='✗ '+msg;delete pending[rid]}}else if(rid.startsWith('commit_msg_')){toast('Commit msg failed: '+msg,'error')}else{toast(msg,'error')}});
  // v1.1 signals
  window.bridge.token_stats_updated.connect(function(stats){updateTokenBar(stats);if(typeof updateSbTokens==='function')updateSbTokens(stats)});
  // v1.1.1: git_status_changed is not yet emitted by the bridge — listen safely
  try{window.bridge.git_status_changed.connect(function(git){updateGitBar(git);if(typeof updateSbGit==='function')updateSbGit(git)})}catch(e){}
  window.bridge.title_generated.connect(function(data){
    if(data.chat_id===state.activeChatId){
      chatBreadcrumb.textContent=data.title;
    }
    debouncedRefreshChatList();
  });
  // apply_result signal — bridge has it defined but does not currently emit it
  try{window.bridge.apply_result.connect(function(result){if(result.ok)toast('Applied: '+result.diff.file_path,'success');else toast('Apply failed: '+(result.error||''),'error')})}catch(e){}
  // v1.0.4: auto-router decision
  window.bridge.router_decision.connect(function(decision){showRouterDecision(decision)});
  // v1.0.4: diff review modal
  window.bridge.diff_review_requested.connect(function(info){showDiffReview(info)});
  // v1.1.1: generic action confirmation modal (execute_command, delete_file, etc.)
  window.bridge.action_confirm_requested.connect(function(info){showActionConfirm(info)});
  }catch(sigErr){console.error('[tera_pilot] signal wiring error (non-fatal):',sigErr)}

  // ── Load chat list IMMEDIATELY and independently of other RPC calls ──
  refreshChatList();

  try{
    const [providers,templates,skills,status]=await Promise.all([callBridge('list_providers'),callBridge('list_templates'),callBridge('list_skills'),callBridge('get_status')]);
    state.providers=providers;state.templates=templates;state.skills=skills;
    if(status.project)state.projectRoot=status.project;
    if(status.project)updateProjectBreadcrumb();
    updateAgentModeToggleUI();
    if(status.active_chat_id)state.activeChatId=status.active_chat_id;
        {const _snipBadge=document.getElementById('navSnippetsBadge');if(_snipBadge)_snipBadge.textContent=status.snippets_count||0;}
    const activeP=providers.find(p=>p.active);
    if(activeP){state.activeProvider=activeP.id;const _m=PROVIDER_META[activeP.id]||{};providerTriggerText.textContent=_m.statusLabel||activeP.label;statusProvider.textContent=_m.modelDisplay||_m.model||activeP.model;profilePlan.textContent=_m.modelDisplay||_m.model||activeP.model}
    // Refresh again after we know the active chat id
    refreshChatList();
    if(state.activeChatId)loadChat(state.activeChatId);
    if(state.projectRoot)refreshFileTree();
    // v1.1: init token bar, git bar, context
    if(status.token_stats)updateTokenBar(status.token_stats);
    if(status.git_status)updateGitBar(status.git_status);
    if(status.git_available&&status.git_status)document.getElementById('gitBar').style.display='flex';
    if(status.git_status&&typeof updateSbGit==='function')updateSbGit(status.git_status);
    if(status.context_stats)updateContextIndicator(status.context_stats);
    // v1.0.4: restore auto-route state
    state.autoRoute=!!status.auto_route;
    toast('Tera Pilot v1.1.0 ready · '+(activeP?activeP.label:'Ollama'));
  }catch(e){console.error('[tera_pilot] init failed',e);toast('Init failed: '+e.message,'error')}
});

// Safety-net: if bridge_ready never fires or RPC hangs, retry chat list after 1s
setTimeout(function(){if(isBackendAvailable()&&chatList.children.length===0){console.log('[tera_pilot] safety-net: retrying chat list load');refreshChatList()}},1000);

// v1.0.4: Router decision indicator in statusbar
function showRouterDecision(decision){
  const el=document.getElementById('routerIndicator');
  if(!el)return;
  if(!decision||!decision.provider_id){
    el.style.display='none';
    // v1.1.4-fix: this used to fail silently — the person would just
    // see the message never send, with no clue why. Surface the
    // router's actual guidance instead.
    if(decision&&decision.reasoning)toast(decision.reasoning,'error');
    return;
  }
  const modelShort=decision.model.split('/').pop();
  el.textContent=decision.complexity+' \u2192 '+decision.provider_id+'/'+modelShort+' (~$'+decision.cost_estimate+')';
  el.title=decision.reasoning;
  el.style.display='inline';
  // Fade out after 8 seconds
  clearTimeout(el._hideTimer);
  el._hideTimer=setTimeout(()=>{el.style.display='none'},8000);
}

// v1.0.4: Diff review modal
function showDiffReview(info){
  const modal=document.getElementById('diffModal');
  const content=document.getElementById('diffContent');
  const pathEl=document.getElementById('diffFilePath');
  const statsEl=document.getElementById('diffStats');
  if(!modal||!content)return;
  pathEl.textContent=info.path;
  statsEl.textContent='+'+info.lines_added+' / -'+info.lines_removed+' lines';
  const html=escapeHtml(info.diff).split('\n').map(line=>{
    if(line.startsWith('+++')||line.startsWith('---'))return '<span style="color:var(--text-muted);font-weight:500">'+line+'</span>';
    if(line.startsWith('+'))return '<span style="color:#86EFAC;background:rgba(134,239,172,0.08)">'+line+'</span>';
    if(line.startsWith('-'))return '<span style="color:#FCA5A5;background:rgba(252,165,165,0.08)">'+line+'</span>';
    if(line.startsWith('@@'))return '<span style="color:#93C5FD">'+line+'</span>';
    return line;
  }).join('\n');
  content.innerHTML=html;
  modal.style.display='flex';
  const applyBtn=document.getElementById('diffApply');
  const rejectBtn=document.getElementById('diffReject');
  const newApply=applyBtn.cloneNode(true);applyBtn.parentNode.replaceChild(newApply,applyBtn);
  const newReject=rejectBtn.cloneNode(true);rejectBtn.parentNode.replaceChild(newReject,rejectBtn);
  newApply.addEventListener('click',()=>{modal.style.display='none';if(isBackendAvailable())callBridge('respond_diff_review',true);toast('Change applied','success')});
  newReject.addEventListener('click',()=>{modal.style.display='none';if(isBackendAvailable())callBridge('respond_diff_review',false);toast('Change rejected','warning')});
}

// v1.1.1: generic action confirmation modal — shown for execute_command /
// delete_file / rename_file / apply_diff / write_binary_file / git_commit,
// gated by the agent_autonomy setting (Settings → Agent → Autonomy).
function showActionConfirm(info){
  const modal=document.getElementById('actionConfirmModal');
  const typeEl=document.getElementById('actionConfirmType');
  const summaryEl=document.getElementById('actionConfirmSummary');
  if(!modal||!summaryEl)return;
  typeEl.textContent=(info.action||'action').replace(/_/g,' ');
  summaryEl.textContent=info.summary||'';
  modal.style.display='flex';
  const allowBtn=document.getElementById('actionConfirmAllow');
  const denyBtn=document.getElementById('actionConfirmDeny');
  // Clone to strip any stale listeners from a previous prompt, same
  // pattern as the diff-review modal above.
  const newAllow=allowBtn.cloneNode(true);allowBtn.parentNode.replaceChild(newAllow,allowBtn);
  const newDeny=denyBtn.cloneNode(true);denyBtn.parentNode.replaceChild(newDeny,denyBtn);
  newAllow.addEventListener('click',()=>{modal.style.display='none';if(isBackendAvailable())callBridge('respond_action_confirm',true)});
  newDeny.addEventListener('click',()=>{modal.style.display='none';if(isBackendAvailable())callBridge('respond_action_confirm',false);toast('Action denied','warning')});
}

// v1.2: Guardian Safety Review modal — shown for risky tool calls with MODIFY verdict
// Returns: "approve" | "reject" | "use_fix"
function showGuardianConfirm(info){
  const modal=document.getElementById('guardianConfirmModal');
  const actionEl=document.getElementById('guardianAction');
  const verdictEl=document.getElementById('guardianVerdict');
  const summaryEl=document.getElementById('guardianSummary');
  const rationaleEl=document.getElementById('guardianRationale');
  const fixSection=document.getElementById('guardianFixSection');
  const fixArgsEl=document.getElementById('guardianFixArgs');
  const useFixBtn=document.getElementById('guardianUseFix');
  const approveBtn=document.getElementById('guardianApprove');
  const rejectBtn=document.getElementById('guardianReject');
  if(!modal||!summaryEl)return;

  actionEl.textContent=(info.action||'action').replace(/_/g,' ');
  verdictEl.textContent='VERDICT: '+(info.guardian_verdict||'MODIFY');
  summaryEl.textContent=info.summary||'';

  // Rationale (optional)
  if(info.rationale){
    rationaleEl.textContent='Rationale: '+info.rationale;
    rationaleEl.style.display='block';
  }else{
    rationaleEl.style.display='none';
  }

  // Proposed fix args (optional)
  if(info.suggested_args){
    fixArgsEl.textContent=JSON.stringify(info.suggested_args,null,2);
    fixSection.style.display='block';
    useFixBtn.style.display='inline-flex';
  }else{
    fixSection.style.display='none';
    useFixBtn.style.display='none';
  }

  modal.style.display='flex';

  // Clone buttons to strip stale listeners
  const newApprove=approveBtn.cloneNode(true);approveBtn.parentNode.replaceChild(newApprove,approveBtn);
  const newReject=rejectBtn.cloneNode(true);rejectBtn.parentNode.replaceChild(newReject,rejectBtn);
  const newUseFix=useFixBtn.cloneNode(true);useFixBtn.parentNode.replaceChild(newUseFix,useFixBtn);

  newApprove.addEventListener('click',()=>{
    modal.style.display='none';
    if(isBackendAvailable())callBridge('respond_guardian_review','approve');
  });
  newReject.addEventListener('click',()=>{
    modal.style.display='none';
    if(isBackendAvailable())callBridge('respond_guardian_review','reject');toast('Guardian review rejected','warning');
  });
  newUseFix.addEventListener('click',()=>{
    modal.style.display='none';
    if(isBackendAvailable())callBridge('respond_guardian_review','use_fix');
  });
}

// Connect Guardian signal from bridge
// v2.2.2: guard for missing window.bridge (browser-only mode without
//         bridge_shim.js — should never happen now, but defensive).
if(window.bridge&&window.bridge.guardian_review_requested){
  window.bridge.guardian_review_requested.connect(function(info){showGuardianConfirm(info)});
}

// ── Project breadcrumb ──────────────────────────────────────────
function updateProjectBreadcrumb(){
  const el=document.getElementById('projectBreadcrumb');if(!el)return;
  if(state.projectRoot){
    const parts=state.projectRoot.replace(/\/$/,'').split('/');
    el.textContent=parts.slice(-2).join('/')+'/';
    el.title=state.projectRoot;
    el.style.display='inline';
  }else{el.style.display='none'}
}

// ── Session token bar (client-side tracker) ────────────────────
function updateSessionTokenBar(){
  const bar=document.getElementById('tokenBar');if(!bar)return;
  if(state.sessionTokens===0){bar.style.display='none';return}
  bar.style.display='flex';
  const fmt=t=>t>=1000000?(t/1000000).toFixed(1)+'M':t>=1000?(t/1000).toFixed(1)+'K':String(t);
  document.getElementById('tbTokens').textContent=fmt(state.sessionTokens)+' tokens \u00b7 '+state.sessionRequests+' req';
  document.getElementById('tbCost').textContent='$'+state.sessionCost.toFixed(3);
  document.getElementById('tbBurn').style.display='none';
}

// ── v1.1: Statusbar token/git update (safe no-ops until statusbar elements exist) ──
function updateSbTokens(stats){
  const el=document.getElementById('sbTokens');if(!el)return;
  if(!stats||stats.request_count===0){el.style.display='none';return}
  el.style.display='inline';
  const fmt=t=>t>=1000000?(t/1000000).toFixed(1)+'M':t>=1000?(t/1000).toFixed(1)+'K':t;
  el.textContent=fmt(stats.total_tokens||0)+' tokens · $'+(stats.total_cost||0).toFixed(2);
}
function updateSbGit(git){
  const el=document.getElementById('sbGit');if(!el)return;
  if(!git||!git.branch||git.error){el.style.display='none';return}
  el.style.display='inline';
  el.textContent=git.branch+(git.staged_count?' +'+git.staged_count:'')+(git.unstaged_count?' ~'+git.unstaged_count:'');
}

function updateTokenBar(stats){
  const bar=document.getElementById('tokenBar');if(!bar)return;
  if(!stats||stats.request_count===0){bar.style.display='none';return}
  bar.style.display='flex';
  const fmt=t=>t>=1000000?(t/1000000).toFixed(1)+'M':t>=1000?(t/1000).toFixed(1)+'K':t;
  document.getElementById('tbTokens').textContent=fmt(stats.total_tokens)+' tokens';
  document.getElementById('tbCost').textContent='$'+(stats.total_cost||0).toFixed(2);
  const burn=document.getElementById('tbBurn');
  if(stats.budget_minutes_left!=null&&stats.budget_minutes_left<120){
    burn.style.display='inline';burn.textContent='· '+Math.round(stats.budget_minutes_left)+'min left';
  }else{burn.style.display='none'}
}

// ── v1.1: Git bar ────────────────────────────────────────────────
function updateGitBar(git){
  const bar=document.getElementById('gitBar');if(!bar)return;
  if(!git||!git.branch||git.error){bar.style.display='none';return}
  bar.style.display='flex';
  document.getElementById('gbBranchName').textContent=git.branch;
  document.getElementById('gbStagedCount').textContent=(git.staged_count||0)+' staged';
  document.getElementById('gbUnstagedCount').textContent=(git.unstaged_count||0)+' unstaged';
  document.getElementById('gbStagedDot').style.display=git.staged_count?'':'none';
  document.getElementById('gbUnstagedDot').style.display=git.unstaged_count?'':'none';
}
// Git bar buttons — disabled until backend adds git_commit/git_stage bridge slots
document.getElementById('gbCommitBtn').addEventListener('click',async()=>{
  toast('Git commit is not yet available','warning');
});
document.getElementById('gbStageAllBtn').addEventListener('click',async()=>{
  toast('Git stage is not yet available','warning');
});

// ── v1.1: Context indicator ──────────────────────────────────────
// v1.1.4-fix (bug 4.1): this used to read ctxStats.total_files /
// ctxStats.token_budget / ctxStats.utilization_pct — fields that only
// ever existed on the unused ContextManager.stats() shape. The real
// backend (AgentRuntime.context_status()) has always returned
// {memory:{message_count,total_tokens,max_tokens,utilization}, ...},
// so ctxStats.total_files was always undefined and the indicator was
// permanently hidden. Wired to the real shape below.
function updateContextIndicator(ctxStats){
  const el=document.getElementById('contextIndicator');if(!el)return;
  const mem=ctxStats&&ctxStats.memory;
  if(!mem||!mem.max_tokens){el.style.display='none';return}
  el.style.display='flex';
  const usedK=(mem.total_tokens||0)/1000;
  const maxK=(mem.max_tokens||0)/1000;
  document.getElementById('ciTokens').textContent=usedK.toFixed(1)+'K/'+maxK.toFixed(0)+'K';
  const pct=(mem.utilization||0)*100;
  document.getElementById('ciFill').style.width=Math.min(100,pct)+'%';
  el.classList.toggle('warn', pct>=75 && pct<90);
  el.classList.toggle('exhausted', pct>=90);
}

// v1.1.4-fix: the indicator was only ever populated once, at
// bridge_ready — it never reflected the conversation actually
// growing. Refresh it after every agent turn.
async function refreshContextIndicator(){
  if(!isBackendAvailable())return;
  try{
    const status=await callBridge('get_context_status');
    updateContextIndicator(status);
  }catch(e){/* non-fatal — indicator just stays at last known state */}
}

// v1.0.2: Wire Apply/Copy buttons on code blocks
function wireCodeButtons(msgEl){
  msgEl.querySelectorAll('.apply-btn[data-codeblock]').forEach(function(btn){
    btn.addEventListener('click',async function(){
      var id=btn.dataset.codeblock;var pre=document.getElementById(id);if(!pre)return;
      var code=pre.textContent;
      var firstLine=code.split('\n')[0].trim();
      var filePath=firstLine.replace(/^(#|\/\/|<!--)\s*/,'').trim();
      if(!isBackendAvailable()){navigator.clipboard.writeText(code);toast('Copied (no backend)','success');return}
      try{var r=await callBridge('write_file',filePath,code);if(r.ok){btn.classList.add('applied');btn.textContent='Applied';toast('Applied to '+filePath,'success')}else toast('Apply failed: '+(r.error||''),'error')}catch(e){toast('Apply error: '+e.message,'error')}
    });
  });
  msgEl.querySelectorAll('.apply-btn[data-copyblock]').forEach(function(btn){
    btn.addEventListener('click',function(){
      var id=btn.dataset.copyblock;var pre=document.getElementById(id);if(!pre)return;
      navigator.clipboard.writeText(pre.textContent);btn.textContent='Copied';setTimeout(function(){btn.textContent='Copy'},1500);
    });
  });
}

// v1.0.2: Token bar click shows breakdown
document.getElementById('tokenBar').addEventListener('click',async function(){
  if(!isBackendAvailable())return;
  // v1.0.4 fix: token_provider_breakdown does not exist. Show stored stats instead.
  try{var b=await callBridge('get_token_stats');if(b&&b.total_tokens){toast(b.total_tokens+' tokens \u00b7 $'+(b.total_cost||0).toFixed(4));return}}catch(e){}
  toast('No token usage yet');
});

// v1.0.2: AI-generated commit messages — handled by addEventListener above

// v1.0.2: Re-wire loaded chat messages with markdown
var _origLoadChat = window.loadChat || null;

// Fallback
setTimeout(()=>{if(!isBackendAvailable()){console.log('[tera_pilot] no backend — browser demo mode');state.templates=DEFAULT_TEMPLATES;state.skills=DEFAULT_SKILLS;state.providers=Object.entries(PROVIDER_META).map(([id,m])=>({id,label:m.label,model:m.model,api_key_set:false,temperature:0.2,max_tokens:4096,active:id===state.activeProvider}))}},1500);

autosize();setTimeout(()=>composerInput.focus(),400);
bindEmptyStateSuggestions();

/* ===================================================================
   v1.0.3 — AUTO-GENERATE CHAT TITLES + DOUBLE-CLICK RENAME
   v1.0.5 — fire auto-title on the FIRST assistant reply, not after
            1-3 messages. The user sees a sensible title in the sidebar
            immediately, instead of a long truncated excerpt.
   =================================================================== */
var _autoTitleRequested = {};

function _maybeAutoTitle() {
  if(!state.activeChatId || !isBackendAvailable()) return;
  // Only request title generation once per chat.
  if(_autoTitleRequested[state.activeChatId]) return;
  // v1.0.5: fire as soon as we have at least one user message AND
  // at least one assistant reply — i.e. as soon as there's enough
  // signal for the model to generate a meaningful title.
  var msgs = chatView.querySelectorAll('.msg');
  var userMsgCount = 0, assistantMsgCount = 0;
  for(var i = 0; i < msgs.length; i++) {
    if(msgs[i].querySelector('.msg-role.user')) userMsgCount++;
    else if(msgs[i].querySelector('.msg-role.assistant')) assistantMsgCount++;
  }
  if(userMsgCount >= 1 && assistantMsgCount >= 1) {
    _autoTitleRequested[state.activeChatId] = true;
    callBridge('generate_title', state.activeChatId).catch(function(){});
  }
}

// Double-click to rename chat item
document.addEventListener('dblclick', function(e) {
  var item = e.target.closest('.chat-item');
  if(!item || !item.dataset.id) return;
  var titleEl = item.querySelector('.chat-item-title');
  if(!titleEl) return;
  var chatId = item.dataset.id;
  var currentTitle = titleEl.textContent;

  // Replace with input
  var input = document.createElement('input');
  input.type = 'text';
  input.value = currentTitle;
  input.style.cssText = 'width:100%;background:var(--bg-primary);border:1px solid var(--accent);border-radius:4px;padding:2px 6px;font-size:12px;color:var(--text-primary);outline:none;font-family:inherit';
  titleEl.style.display = 'none';
  titleEl.parentNode.insertBefore(input, titleEl.nextSibling);
  input.focus();
  input.select();

  function finish() {
    var newTitle = input.value.trim() || currentTitle;
    input.remove();
    titleEl.style.display = '';
    titleEl.textContent = newTitle;
    chatBreadcrumb.textContent = newTitle;
    if(isBackendAvailable() && newTitle !== currentTitle) {
      callBridge('rename_chat', chatId, newTitle).catch(function(){});
      // Mark this as manually renamed — don't auto-title it again
      _autoTitleRequested[chatId] = true;
    }
  }

  input.addEventListener('blur', finish);
  input.addEventListener('keydown', function(ev) {
    if(ev.key === 'Enter') { ev.preventDefault(); input.blur(); }
    if(ev.key === 'Escape') { input.value = currentTitle; input.blur(); }
  });
});

/* ===================================================================
   v2.0.0 — SECTION SWITCHER (General / Heavy Code / Office Worker)
   The orphaned heavycodeOverlay / officeOverlay logic was removed;
   section switching is fully handled by the HC-pane / Office-pane
   modules further below. This block now only syncs the active state
   of the section buttons.
   =================================================================== */
(function(){
  const switcher = document.getElementById('sectionSwitcher');
  if(!switcher) return;
  switcher.querySelectorAll('.section-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      switcher.querySelectorAll('.section-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
    });
  });
})();

/* ===================================================================
   v1.0.3 — AUTO-GENERATE CHAT TITLES + DOUBLE-CLICK RENAME
   =================================================================== */

/* ===================================================================
   v1.0.3 — CONTEXT USAGE COUNTER IN DIALOG
   =================================================================== */
(function(){
  const _origFinalizeMessage = window.finalizeMessage;
  window.finalizeMessage = function(result) {
    if(_origFinalizeMessage) _origFinalizeMessage(result);
    if(!result) return;
    // Add context info to the last message's meta
    const msgs = chatView.querySelectorAll('.msg');
    const last = msgs[msgs.length - 1];
    if(!last) return;
    const meta = last.querySelector('.msg-meta');
    if(!meta) return;
    if(result.context && result.context.utilization_pct !== undefined) {
      const pct = Math.min(100, result.context.utilization_pct || 0);
      const budget = result.context.budget || 128000;
      const used = result.context.total_tokens || 0;
      const ctxEl = document.createElement('span');
      ctxEl.className = 'msg-context-info';
      ctxEl.title = `Context: ${used} / ${budget} tokens (${pct}%)`;
      ctxEl.innerHTML = `ctx ${pct}%<span class="ctx-bar"><span class="ctx-fill" style="width:${pct}%"></span></span>`;
      meta.appendChild(ctxEl);
    }
    // v1.0.3: Persist session context to tera_pilot_memory.md (debounced)
    if(isBackendAvailable() && result.text && result.text.length > 20) {
      clearTimeout(window._memorySaveTimer);
      window._memorySaveTimer = setTimeout(async () => {
        try {
          var title = chatBreadcrumb.textContent || 'Untitled';
          var summary = result.text.slice(0, 2000);
          await callBridge('save_memory', state.activeChatId || 'session', title, summary);
        } catch(e) {}
      }, 5000);
    }
  };
})();

/* ===================================================================
   v1.0.3 — AUTO-UPDATE CHECK ON STARTUP
   =================================================================== */
(function(){
  // Check for updates 3 seconds after bridge connects
  window.addEventListener('tera_pilot:bridge_ready', () => {
    if(!isBackendAvailable()) return;
    setTimeout(async () => {
      try {
        const result = await callBridge('check_for_updates');
        if(result.ok) {
          toast('Checking for updates...');
        }
      } catch(e) {}
    }, 3000);
  });

  // Listen for update signal
  if(window.bridge) {
    window.bridge.update_check_result.connect(function(data) {
      if(data.update_available) {
        toast('Update available: ' + data.latest + ' (current: ' + data.current + ')', 'success');
      }
    });
  }
  // Wire after bridge is ready
  window.addEventListener('tera_pilot:bridge_ready', () => {
    if(window.bridge && window.bridge.update_check_result) {
      window.bridge.update_check_result.connect(function(data) {
        if(data.update_available) {
          const msg = 'Update ' + data.latest + ' available! Current: ' + data.current;
          toast(msg, 'success');
        }
      });
    }
  });
})();


/* Brand Status Indicator (replaces mascot) */
(function initBrandStatus(){
  var dot = document.getElementById('brandStatusDot');
  if(!dot) return;
  function updateStatus(){
    if(isBackendAvailable()){
      dot.className = state.isGenerating ? 'brand-status-dot generating' : 'brand-status-dot connected';
    } else {
      dot.className = 'brand-status-dot error';
    }
  }
  updateStatus();
  setInterval(updateStatus, 2000);
  var origShow = window.showActivity;
  window.showActivity = function(){ updateStatus(); if(origShow) origShow(); };
  var origHide = window.hideActivity;
  window.hideActivity = function(){ updateStatus(); if(origHide) origHide(); };
})();

/* ===================================================================
   TERA_PILOT MASCOT (kept for ref, not rendered) — pixel art character drawn on brand canvas
   =================================================================== */
(function initMascot(){
  var canvas = document.getElementById('mascotCanvas');
  if(!canvas) return;
  var ctx = canvas.getContext('2d');
  // 20x20 logical pixels
  var S = 20;

  // Color palette
  var C = {
    _  : null,            // transparent
    o  : '#3D2B1F',       // dark brown outline
    f  : '#F5E6C8',       // light cream face
    pk : '#F2A0A0',       // pink cheeks
    ey : '#2C2C2C',       // dark eyes (half-closed)
    br : '#3D2B1F',       // dark eyebrows
    ns : '#B8956A',       // small nose
    mo : '#D4956A',       // mouth line
    ht : '#C4652A',       // terracotta hat
    hd : '#A04520',       // hat darker shade
    wh : '#E8D8B8',       // wing/antenna lighter
    wc : '#C4B498',       // wing/antenna
    lg : '#5C4033',       // legs
  };

  // Draw the mascot programmatically for clean pixel art
  function drawMascot() {
    ctx.clearRect(0, 0, S, S);

    // -- Hat (terracotta, stepped) --
    // Top of hat: row 0-1, centered
    ctx.fillStyle = C.ht;
    for(var y = 1; y <= 2; y++)
      for(var x = 5; x <= 9; x++) ctx.fillRect(x, y, 1, 1);
    // Hat step wider: row 2-3
    for(var y = 2; y <= 3; y++)
      for(var x = 4; x <= 11; x++) ctx.fillRect(x, y, 1, 1);
    // Hat darker accent line
    ctx.fillStyle = C.hd;
    for(var x = 7; x <= 9; x++) ctx.fillRect(x, 2, 1, 1);
    for(var x = 8; x <= 10; x++) ctx.fillRect(x, 3, 1, 1);

    // -- Face (cream, square) --
    ctx.fillStyle = C.f;
    for(var y = 5; y <= 12; y++)
      for(var x = 4; x <= 11; x++) ctx.fillRect(x, y, 1, 1);

    // -- Face outline (dark brown) --
    ctx.fillStyle = C.o;
    // Top
    for(var x = 4; x <= 11; x++) ctx.fillRect(x, 5, 1, 1);
    // Bottom
    for(var x = 4; x <= 11; x++) ctx.fillRect(x, 12, 1, 1);
    // Left
    for(var y = 5; y <= 12; y++) ctx.fillRect(4, y, 1, 1);
    // Right
    for(var y = 5; y <= 12; y++) ctx.fillRect(11, y, 1, 1);

    // -- Wings/antennas (symmetric) --
    ctx.fillStyle = C.wc;
    ctx.fillRect(2, 5, 1, 2); ctx.fillRect(17, 5, 1, 2); // main
    ctx.fillRect(3, 6, 1, 1); ctx.fillRect(16, 6, 1, 1); // inner
    ctx.fillStyle = C.wh;
    ctx.fillRect(2, 5, 1, 1); ctx.fillRect(17, 5, 1, 1); // tips lighter

    // -- Eyebrows (dark) --
    ctx.fillStyle = C.br;
    ctx.fillRect(6, 7, 3, 1);  // left brow
    ctx.fillRect(10, 7, 2, 1); // right brow

    // -- Eyes (half-closed) --
    ctx.fillStyle = C.ey;
    ctx.fillRect(7, 8, 2, 1);  // left eye
    ctx.fillRect(11, 8, 1, 1); // right eye
    // Eyelid (half closing) — cream color over top half
    ctx.fillStyle = C.f;
    ctx.fillRect(7, 7, 2, 1);  // left eyelid
    ctx.fillRect(11, 7, 1, 1); // right eyelid

    // -- Pink cheeks --
    ctx.fillStyle = C.pk;
    ctx.fillRect(5, 10, 1, 1);  // left cheek
    ctx.fillRect(13, 10, 1, 1); // right cheek

    // -- Nose (small vertical) --
    ctx.fillStyle = C.ns;
    ctx.fillRect(8, 9, 1, 1);
    ctx.fillRect(8, 10, 1, 1);

    // -- Smile --
    ctx.fillStyle = C.mo;
    ctx.fillRect(7, 11, 4, 1);
    ctx.fillStyle = C.f; // mouth corners (rounded)
    ctx.fillRect(6, 11, 1, 1);
    ctx.fillRect(11, 11, 1, 1);

    // -- Legs (6 total: 3 left, 3 right) --
    ctx.fillStyle = C.lg;
    ctx.fillRect(5, 13, 1, 2);  // left 1
    ctx.fillRect(7, 13, 1, 2);  // left 2
    ctx.fillRect(9, 13, 1, 2);  // left 3
    ctx.fillRect(10, 13, 1, 2); // right 1
    ctx.fillRect(12, 13, 1, 2); // right 2
    ctx.fillRect(14, 13, 1, 2); // right 3
  }

  drawMascot();
})();

/* ===================================================================
   NEURAL PIXELS — scattered pulsing dots during generation
   =================================================================== */
(function initNeuralPixels(){
  var container = document.getElementById('neuralPixels');
  if(!container) return;
  var pixels = [];
  var COUNT = 40;

  for(var i = 0; i < COUNT; i++) {
    var el = document.createElement('div');
    el.className = 'neural-pixel';
    el.style.left = (Math.random() * 100) + '%';
    el.style.top = (Math.random() * 100) + '%';
    el.style.setProperty('--dur', (1.5 + Math.random() * 2.5) + 's');
    el.style.setProperty('--delay', (Math.random() * 3) + 's');
    el.style.width = (3 + Math.random() * 3) + 'px';
    el.style.height = el.style.width;
    container.appendChild(el);
    pixels.push(el);
  }

  // Expose activation
  window.__activateNeuralPixels = function(on) {
    if(on) container.classList.add('active');
    else container.classList.remove('active');
  };
})();

/* ===================================================================
   SYNAPSE CANVAS — animated neural network lines during thinking
   =================================================================== */
(function initSynapseCanvas(){
  var canvas = document.getElementById('synapseCanvas');
  if(!canvas) return;
  var ctx = canvas.getContext('2d');
  var nodes = [];
  var NODE_COUNT = 18;
  var W, H, raf = 0;
  var thinkLevel = 0;
  var targetThink = 0;

  function resize() {
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = W;
    canvas.height = H;
    // Regenerate nodes
    nodes = [];
    for(var i = 0; i < NODE_COUNT; i++) {
      nodes.push({
        x: Math.random() * W,
        y: Math.random() * H,
        vx: (Math.random() - 0.5) * 0.3,
        vy: (Math.random() - 0.5) * 0.3,
        r: 1.5 + Math.random() * 2,
        phase: Math.random() * Math.PI * 2,
      });
    }
  }
  resize();
  window.addEventListener('resize', resize);

  function draw(t) {
    raf = requestAnimationFrame(draw);
    var time = t / 1000;

    // Smooth think level
    thinkLevel += (targetThink - thinkLevel) * 0.05;

    ctx.clearRect(0, 0, W, H);

    // Update node positions (slow drift)
    for(var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      n.x += n.vx;
      n.y += n.vy;
      if(n.x < 0 || n.x > W) n.vx *= -1;
      if(n.y < 0 || n.y > H) n.vy *= -1;
      // Gentle attraction to center when thinking
      if(thinkLevel > 0.1) {
        n.vx += (W/2 - n.x) * 0.00003 * thinkLevel;
        n.vy += (H/2 - n.y) * 0.00003 * thinkLevel;
      }
    }

    // Draw connections
    var alpha = thinkLevel * 0.12;
    if(alpha < 0.005) return;

    ctx.lineWidth = 0.5;
    for(var i = 0; i < nodes.length; i++) {
      for(var j = i + 1; j < nodes.length; j++) {
        var a = nodes[i], b = nodes[j];
        var dx = a.x - b.x, dy = a.y - b.y;
        var dist = Math.sqrt(dx*dx + dy*dy);
        if(dist < 250) {
          var lineAlpha = (1 - dist / 250) * alpha;
          // Pulse along the line
          var pulse = 0.5 + 0.5 * Math.sin(time * 2 + a.phase + b.phase);
          lineAlpha *= (0.5 + pulse * 0.5) * thinkLevel;

          ctx.strokeStyle = 'rgba(244,244,245,' + lineAlpha.toFixed(3) + ')';
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();

          // Traveling pulse dot
          if(thinkLevel > 0.3 && pulse > 0.7) {
            var pt = (time * 0.5 + a.phase) % 1;
            var px = a.x + (b.x - a.x) * pt;
            var py = a.y + (b.y - a.y) * pt;
            ctx.fillStyle = 'rgba(147,197,253,' + (lineAlpha * 3).toFixed(3) + ')';
            ctx.beginPath();
            ctx.arc(px, py, 1.5, 0, Math.PI * 2);
            ctx.fill();
          }
        }
      }
    }

    // Draw nodes
    for(var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      var glow = 0.3 + 0.7 * (0.5 + 0.5 * Math.sin(time * 1.5 + n.phase));
      var na = glow * thinkLevel * 0.4;
      if(na < 0.01) continue;
      ctx.fillStyle = 'rgba(244,244,245,' + na.toFixed(3) + ')';
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r * (1 + thinkLevel * 0.3), 0, Math.PI * 2);
      ctx.fill();
    }
  }

  raf = requestAnimationFrame(draw);

  window.__activateSynapse = function(on) {
    targetThink = on ? 1 : 0;
    if(on) canvas.classList.add('active');
    else canvas.classList.remove('active');
  };
})();

/* ===================================================================
   HOOK: activate/deactivate thinking animations on send/done
   =================================================================== */
var _origHandleSend = window.handleSend;
// We patch the state transitions instead:
var _origShowActivity = window.showActivity;
window.showActivity = function() {
  // Activate all thinking visuals
  if(window.__activateNeuralPixels) window.__activateNeuralPixels(true);
  if(window.__activateSynapse) window.__activateSynapse(true);
  if(_origShowActivity) _origShowActivity();
};

var _origFinalizeMessage = window.finalizeMessage;
window.finalizeMessage = function(result) {
  // Deactivate thinking visuals
  if(window.__activateNeuralPixels) window.__activateNeuralPixels(false);
  if(window.__activateSynapse) window.__activateSynapse(false);
  if(_origFinalizeMessage) _origFinalizeMessage(result);
};

/* ===================================================================
   FRAMELESS WINDOW — controls, drag, and edge resize
   ===================================================================
   The native OS titlebar is gone (see main_window.py — FramelessWindowHint).
   We render custom window controls in HTML and route clicks back through
   the bridge. Window dragging and edge resizing use Qt's native
   startSystemMove / startSystemResize so OS-level snapping still works.
   =================================================================== */
(function initFramelessWindow(){
  // ── Platform detection ──────────────────────────────────────────
  // We try the bridge first (most reliable — comes from Python's sys.platform),
  // then fall back to navigator.platform, then default to "linux".
  function applyPlatform(p){
    if(!p) return;
    if(p !== 'darwin' && p !== 'win32' && p !== 'linux') p = 'linux';
    document.documentElement.setAttribute('data-platform', p);
  }
  // Apply a guess immediately so the UI renders correctly before the bridge connects
  applyPlatform(
    (navigator.platform === 'MacIntel') ? 'darwin'
    : (navigator.platform === 'Win32' || navigator.userAgent.indexOf('Windows') !== -1) ? 'win32'
    : 'linux'
  );

  // ── Bridge helper ───────────────────────────────────────────────
  // These methods are tolerant of the bridge not being connected yet
  // (e.g. during the first ~500ms after page load).
  function callBridgeSafe(method, ...args){
    if(!isBackendAvailable() || !window.bridge || !window.bridge[method]) return undefined;
    try{ return window.bridge[method](...args); }
    catch(e){ console.warn('[frameless] bridge call failed:', method, e); return undefined; }
  }

  // ── Window control buttons ──────────────────────────────────────
  // macOS traffic lights (top-left of sidebar)
  var tlClose = document.getElementById('tlClose');
  var tlMin   = document.getElementById('tlMin');
  var tlMax   = document.getElementById('tlMax');
  // Windows/Linux controls (top-right of topbar)
  var wcMin   = document.getElementById('wcMin');
  var wcMax   = document.getElementById('wcMax');
  var wcClose = document.getElementById('wcClose');

  function bindWindowButton(el, action){
    if(!el) return;
    el.addEventListener('click', function(e){
      e.preventDefault();
      e.stopPropagation();
      action();
    });
    // Mousedown must stopPropagation so it doesn't trigger the drag-region handler
    el.addEventListener('mousedown', function(e){
      e.stopPropagation();
    });
  }
  bindWindowButton(tlClose, function(){ callBridgeSafe('close_window'); });
  bindWindowButton(tlMin,   function(){ callBridgeSafe('minimize_window'); });
  bindWindowButton(tlMax,   function(){ callBridgeSafe('toggle_maximize_window'); });
  bindWindowButton(wcClose, function(){ callBridgeSafe('close_window'); });
  bindWindowButton(wcMin,   function(){ callBridgeSafe('minimize_window'); });
  bindWindowButton(wcMax,   function(){ callBridgeSafe('toggle_maximize_window'); });

  // Double-click on topbar → toggle maximize (Windows convention)
  var topbar = document.querySelector('.topbar');
  if(topbar){
    topbar.addEventListener('dblclick', function(e){
      // Ignore dblclick on interactive elements
      if(e.target.closest('button, input, a, .breadcrumb, .token-bar, .win-controls')) return;
      callBridgeSafe('toggle_maximize_window');
    });
  }

  // ── Drag region: topbar + mac-traffic-lights ────────────────────
  // When the user mousedowns on a [data-drag-region] element (and NOT on an
  // interactive child), we ask Qt to start a native system move.
  function isInteractiveTarget(el){
    return !!el.closest('button, input, a, textarea, select, [contenteditable="true"], [data-no-drag], .icon-btn, .sidebar-toggle, .token-bar, .breadcrumb, .win-controls, .mac-traffic-lights .tl, .chat-item, .nav-item, .catalog-trigger, .new-chat-btn, .section-btn, .composer, .composer-input, .cv-toggle, .cv-action, .cv-tab, .ftp-action, .ftp-file');
  }

  function startDrag(){
    // The bridge returns true if the OS accepted the drag request.
    // We don't need to track it further — Qt handles the rest.
    callBridgeSafe('start_window_drag');
  }

  document.querySelectorAll('[data-drag-region]').forEach(function(el){
    el.addEventListener('mousedown', function(e){
      if(e.button !== 0) return; // left button only
      if(isInteractiveTarget(e.target)) return;
      // Don't start drag if the user is trying to interact with a text selection
      if(window.getSelection && window.getSelection().toString().length > 0) return;
      startDrag();
    });
  });

  // The sidebar's empty space is also a drag region (macOS-style)
  var sidebar = document.querySelector('.sidebar');
  if(sidebar){
    sidebar.addEventListener('mousedown', function(e){
      if(e.button !== 0) return;
      if(isInteractiveTarget(e.target)) return;
      // Only drag if clicked on the sidebar background itself (not on a child element)
      // The brand area and the mac-traffic-lights wrapper are draggable.
      if(e.target === sidebar || e.target.classList.contains('brand') ||
         e.target.classList.contains('brand-status') || e.target.classList.contains('brand-status-dot') ||
         e.target.classList.contains('brand-name') || e.target.classList.contains('brand-version') ||
         e.target.classList.contains('sidebar-label') || e.target.classList.contains('sidebar-spacer')){
        startDrag();
      }
    });
  }

  // ── Edge resize handles ─────────────────────────────────────────
  document.querySelectorAll('[data-resize]').forEach(function(edge){
    edge.addEventListener('mousedown', function(e){
      if(e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      callBridgeSafe('start_window_resize', edge.dataset.resize);
    });
  });

  // ── Replace platform guess with the real value from the bridge ──
  // The bridge's get_platform() returns 'darwin' / 'win32' / 'linux'
  // and is the source of truth (Python's sys.platform is more reliable
  // than navigator.platform).
  function refreshPlatformFromBridge(){
    try{
      if(isBackendAvailable() && window.bridge && window.bridge.get_platform){
        var p = window.bridge.get_platform();
        applyPlatform(p);
      }
    }catch(e){}
  }
  // After bridge connects, refresh
  window.addEventListener('tera_pilot:bridge_ready', refreshPlatformFromBridge);
  // Also refresh on a delay in case bridge_ready already fired
  setTimeout(refreshPlatformFromBridge, 1500);
  setTimeout(refreshPlatformFromBridge, 3000);

  // ── Responsive sidebar auto-collapse ──────────────────────────
  // When the window is resized below 700px, auto-collapse the sidebar.
  // When resized back above 920px, restore it (unless user explicitly collapsed it).
  (function initResponsiveSidebar(){
    var app = document.querySelector('.app');
    if(!app) return;
    var userCollapsed = app.classList.contains('sidebar-collapsed');
    var autoCollapsed = false;

    function handleResize(){
      var w = window.innerWidth;
      var isCollapsed = app.classList.contains('sidebar-collapsed');

      if(w < 700 && !isCollapsed){
        app.classList.add('sidebar-collapsed');
        autoCollapsed = true;
      } else if(w >= 920 && autoCollapsed && !userCollapsed){
        app.classList.remove('sidebar-collapsed');
        autoCollapsed = false;
      }
    }

    // Track manual sidebar toggle to avoid overriding user choice
    var toggleBtn = document.querySelector('.sidebar-toggle');
    if(toggleBtn){
      toggleBtn.addEventListener('click', function(){
        userCollapsed = app.classList.contains('sidebar-collapsed');
        if(!userCollapsed) autoCollapsed = false; // user manually expanded
      });
    }

    window.addEventListener('resize', handleResize);
    handleResize(); // run once on load
  })();

  // ── Override __teraPilotReady to grab platform from initial state ────
  // The backend pushes a status dict that includes `platform`.
  var _origTeraPilotReady = window.__teraPilotReady;
  window.__teraPilotReady = function(status){
    try{
      if(status && status.platform){
        applyPlatform(status.platform);
      }
    }catch(e){}
    if(_origTeraPilotReady) _origTeraPilotReady(status);
    // v1.2: reload slash commands when backend reconnects
    if(window.__reloadSlashCommands) window.__reloadSlashCommands();
  };
})();
/* ===================================================================
   v1.2 — MEMORY FILE EDITOR (Collective Memory)
   =================================================================== */
// ===== Memory File Editor =====
(function(){
  var openBtn = document.getElementById('openClaudeMdBtn');
  var backdrop = document.getElementById('claudeMdBackdrop');
  var modal = document.getElementById('claudeMdModal');
  var closeBtn = document.getElementById('claudeMdClose');
  var cancelBtn = document.getElementById('claudeMdCancel');
  var saveBtn = document.getElementById('claudeMdSave');
  var editor = document.getElementById('claudeMdEditor');
  var pathLabel = document.getElementById('claudeMdPath');
  var statsEl = document.getElementById('claudeMdStats');
  var saveStatus = document.getElementById('claudeMdSaveStatus');
  var addLessonBtn = document.getElementById('claudeMdAddLessonBtn');
  var lessonForm = document.getElementById('claudeMdLessonForm');
  var lessonTitle = document.getElementById('claudeMdLessonTitle');
  var lessonBody = document.getElementById('claudeMdLessonBody');
  var lessonCancel = document.getElementById('claudeMdLessonCancel');
  var lessonSave = document.getElementById('claudeMdLessonSave');
  
  var currentPath = '';

  function openModal(){
    modal.style.display = 'flex';
    backdrop.style.display = 'block';
    if(isBackendAvailable()){
      callBridge('read_claude_md').then(function(res){
        if(res && res.ok){
          editor.value = res.content || '';
          currentPath = res.path || '';
          pathLabel.textContent = res.source === 'none' ? 'New file: ' + currentPath : currentPath;
          updateStats(res.content || '');
        }
      });
    }
  }

  function closeModal(){
    modal.style.display = 'none';
    backdrop.style.display = 'none';
    lessonForm.style.display = 'none';
    saveStatus.textContent = '';
  }

  function updateStats(content){
    if(isBackendAvailable()){
      // Quick local estimate
      var lines = content.split('\n').length;
      var sections = (content.match(/^## /gm) || []).length;
      var tokens = Math.ceil(content.length / 4);
      var budgetClass = tokens > 2500 ? ' over-budget' : '';
      statsEl.className = budgetClass ? 'over-budget' : '';
      statsEl.innerHTML = lines + ' lines · ' + sections + ' sections · ~' + tokens + ' tokens' + (tokens > 2500 ? ' <span style="color:#EF4444">(over 2500 budget!)</span>' : '');
    }
  }

  function saveContent(){
    if(!isBackendAvailable()) return;
    var content = editor.value;
    callBridge('write_claude_md', content, currentPath).then(function(res){
      if(res && res.ok){
        saveStatus.textContent = 'Saved ✓';
        currentPath = res.path;
        pathLabel.textContent = currentPath;
        updateStats(content);
        setTimeout(function(){ saveStatus.textContent = ''; }, 2000);
      } else {
        saveStatus.textContent = 'Error: ' + (res && res.error ? res.error : 'unknown');
        saveStatus.style.color = '#EF4444';
      }
    });
  }

  if(openBtn) openBtn.addEventListener('click', openModal);
  window.__openClaudeMdModal = openModal;
  if(closeBtn) closeBtn.addEventListener('click', closeModal);
  if(cancelBtn) cancelBtn.addEventListener('click', closeModal);
  if(backdrop) backdrop.addEventListener('click', closeModal);
  if(saveBtn) saveBtn.addEventListener('click', saveContent);
  
  // Live stats update as user types
  if(editor) editor.addEventListener('input', function(){
    updateStats(editor.value);
  });

  // Add Lesson workflow
  if(addLessonBtn) addLessonBtn.addEventListener('click', function(){
    lessonForm.style.display = lessonForm.style.display === 'none' ? 'block' : 'none';
    if(lessonForm.style.display === 'block') lessonTitle.focus();
  });
  if(lessonCancel) lessonCancel.addEventListener('click', function(){
    lessonForm.style.display = 'none';
  });
  if(lessonSave) lessonSave.addEventListener('click', function(){
    var title = lessonTitle.value.trim();
    var body = lessonBody.value.trim();
    if(!title || !body){ toast('Both title and body are required'); return; }
    if(isBackendAvailable()){
      callBridge('append_claude_lesson', title, body).then(function(res){
        if(res && res.ok){
          toast('Lesson added to ' + (res.path || 'memory file'));
          lessonTitle.value = '';
          lessonBody.value = '';
          lessonForm.style.display = 'none';
          // Refresh editor
          callBridge('read_claude_md').then(function(r){
            if(r && r.ok){
              editor.value = r.content || '';
              currentPath = r.path || '';
              pathLabel.textContent = currentPath;
              updateStats(r.content || '');
            }
          });
        }
      });
    }
  });

  // Ctrl+S to save when modal is open
  document.addEventListener('keydown', function(e){
    if((e.metaKey || e.ctrlKey) && e.key === 's' && modal.style.display === 'flex'){
      e.preventDefault();
      saveContent();
    }
  });
})();

/* ===================================================================
   v1.2 — SLASH COMMANDS (Boris methodology: "Everything you do more
   than twice becomes a tool.") — autocomplete popup for the composer.
   =================================================================== */
// ===== Slash Commands =====
(function(){
  var commands = [];
  var autocompleteEl = null;
  var selectedIndex = -1;
  var currentFilter = '';

  function createAutocomplete(){
    if(autocompleteEl) return;
    autocompleteEl = document.createElement('div');
    autocompleteEl.className = 'slash-autocomplete';
    autocompleteEl.style.cssText = 'position:absolute;bottom:100%;left:0;right:0;background:var(--bg-secondary);border:1px solid var(--border-strong);border-radius:var(--r-panel);margin-bottom:4px;max-height:200px;overflow-y:auto;z-index:100;display:none;box-shadow:0 -4px 20px rgba(0,0,0,0.3)';
    composerInput.parentElement.style.position = 'relative';
    composerInput.parentElement.appendChild(autocompleteEl);
  }

  function loadCommands(){
    if(!isBackendAvailable()) return;
    callBridge('list_slash_commands').then(function(res){
      if(res && res.ok) commands = res.commands || [];
    });
  }

  function showAutocomplete(filter){
    currentFilter = filter.toLowerCase().slice(1); // remove /
    var filtered = commands.filter(function(c){
      return c.id.toLowerCase().indexOf(currentFilter) !== -1;
    });
    if(filtered.length === 0 || !autocompleteEl){
      if(autocompleteEl) autocompleteEl.style.display = 'none';
      return;
    }
    selectedIndex = 0;
    autocompleteEl.innerHTML = filtered.map(function(c, i){
      var sourceTag = c.source === 'project' ? '<span style="color:#818CF8;font-size:9px">project</span>' : '';
      var argsTag = c.has_arguments ? '<span style="color:var(--text-muted);font-size:9px">$ARGUMENTS</span>' : '';
      return '<div class="slash-ac-item'+(i===0?' selected':'')+'" data-cmd="'+c.id+'" data-index="'+i+'" style="padding:6px 12px;cursor:pointer;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border);font-size:12px;' + (i===0?'background:var(--bg-floating);':'') + '">' +
        '<div><span style="color:var(--accent);font-family:JetBrains Mono,monospace;font-weight:500">/'+c.id+'</span> <span style="color:var(--text-secondary);margin-left:8px;font-size:11px">'+esc(c.description)+'</span></div>' +
        '<div style="display:flex;gap:6px;align-items:center">'+argsTag+sourceTag+'</div>' +
      '</div>';
    }).join('');
    autocompleteEl.style.display = 'block';

    autocompleteEl.querySelectorAll('.slash-ac-item').forEach(function(item){
      item.addEventListener('click', function(){
        selectCommand(this.getAttribute('data-cmd'));
      });
      item.addEventListener('mouseenter', function(){
        selectedIndex = parseInt(this.getAttribute('data-index'));
        highlightItem();
      });
    });
  }

  function highlightItem(){
    if(!autocompleteEl) return;
    var items = autocompleteEl.querySelectorAll('.slash-ac-item');
    items.forEach(function(item, i){
      item.style.background = i === selectedIndex ? 'var(--bg-floating)' : '';
      item.classList.toggle('selected', i === selectedIndex);
    });
    if(items[selectedIndex]) items[selectedIndex].scrollIntoView({block:'nearest'});
  }

  function selectCommand(cmdId){
    var cmd = commands.find(function(c){ return c.id === cmdId; });
    if(!cmd) return;
    if(autocompleteEl) autocompleteEl.style.display = 'none';
    if(cmd.has_arguments){
      composerInput.value = '/' + cmd.id + ' ';
      composerInput.focus();
      composerInput.setSelectionRange(composerInput.value.length, composerInput.value.length);
    } else {
      // Insert the command and let user send it
      composerInput.value = '/' + cmd.id;
      composerInput.dispatchEvent(new Event('input'));
    }
  }

  function hideAutocomplete(){
    if(autocompleteEl) autocompleteEl.style.display = 'none';
  }

  if(composerInput){
    createAutocomplete();
    
    composerInput.addEventListener('input', function(){
      var val = composerInput.value;
      var cursorPos = composerInput.selectionStart;
      // Check if we're at a slash command at the beginning or after a newline
      var beforeCursor = val.slice(0, cursorPos);
      var match = beforeCursor.match(/\/(\w*)$/);
      if(match){
        loadCommands();
        showAutocomplete('/' + match[1]);
      } else {
        hideAutocomplete();
      }
    });

    composerInput.addEventListener('keydown', function(e){
      if(!autocompleteEl || autocompleteEl.style.display === 'none') return;
      var items = autocompleteEl.querySelectorAll('.slash-ac-item');
      if(!items.length) return;
      
      if(e.key === 'ArrowDown'){
        e.preventDefault();
        selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
        highlightItem();
      } else if(e.key === 'ArrowUp'){
        e.preventDefault();
        selectedIndex = Math.max(selectedIndex - 1, 0);
        highlightItem();
      } else if(e.key === 'Tab' || e.key === 'Enter'){
        if(selectedIndex >= 0 && selectedIndex < items.length){
          e.preventDefault();
          selectCommand(items[selectedIndex].getAttribute('data-cmd'));
        }
      } else if(e.key === 'Escape'){
        hideAutocomplete();
      }
    });

    composerInput.addEventListener('blur', function(){
      setTimeout(hideAutocomplete, 150);
    });
  }

  function esc(s){
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  // Reload commands when project changes
  window.__reloadSlashCommands = loadCommands;
  // Load on init
  setTimeout(loadCommands, 1000);
})();

/* ===================================================================
   v1.1.0 — HEAVY CODE SECTION + MCP + ADVANCED AGENT SETTINGS
   =================================================================== */
(function(){
  // State
  const hcPane = document.getElementById('heavycodePane');
  const hcChatView = document.getElementById('hcChatView');
  const hcEmptyState = document.getElementById('hcEmptyState');
  const hcComposerInput = document.getElementById('hcComposerInput');
  const hcSendBtn = document.getElementById('hcSendBtn');
  const hcBackBtn = document.getElementById('hcBackBtn');
  const hcSettingsBtn = document.getElementById('hcSettingsBtn');
  const hcQuotaCount = document.getElementById('hcQuotaCount');
  const hcQuotaPill = document.getElementById('hcQuotaPill');
  const hcQuotaBadge = document.getElementById('hcQuotaBadge');
  const hcQuotaText = document.getElementById('hcQuotaText');
  const hcComposerHint = document.getElementById('hcComposerHint');
  const hcRoleHint = document.getElementById('hcRoleHint');
  const hcResetQuotaBtn = document.getElementById('hcResetQuotaBtn');
  const hcTeraPilotSidebarToggle = document.getElementById('hcTeraPilotSidebarToggle');
  const csBackdrop = document.getElementById('csBackdrop');
  const switcher = document.getElementById('sectionSwitcher');
  if(!hcPane || !switcher) return;

  // Section state
  let hcActive = false;
  let hcMode = 'single';        // 'single' | 'subagent' | 'parallel'
  let hcIsGenerating = false;
  let hcChatId = null;

  // ── Section switcher override ─────────────────────────────────
  // The original section-switcher IIFE only opens/closes the Coming
  // Soon overlay. We override its behavior: heavycode shows our pane.
  //
  // v1.1.1: .hc-pane now lives INSIDE <main class="stage">, so it only
  // covers the stage area — the Tera Pilot sidebar stays visible. We no longer
  // dim the screen with csBackdrop (that would also dim the sidebar and
  // make it feel like a separate-window modal again).
  function showHCPane() {
    // Hide general UI elements that don't apply to Heavy Code
    document.querySelectorAll('.coming-soon-overlay').forEach(o => o.classList.remove('visible'));
    hcPane.classList.add('visible');
    // Intentionally NOT showing csBackdrop — sidebar must stay fully visible
    // so the user keeps spatial context that they're still inside Tera Pilot.
    if (csBackdrop) csBackdrop.classList.remove('visible');
    switcher.querySelectorAll('.section-btn').forEach(b => b.classList.remove('active'));
    switcher.querySelector('[data-section="heavycode"]').classList.add('active');
    hcActive = true;
    refreshHCQuota();
    hcComposerInput && hcComposerInput.focus();
  }

  function hideHCPane() {
    hcPane.classList.remove('visible');
    if (csBackdrop) csBackdrop.classList.remove('visible');
    switcher.querySelectorAll('.section-btn').forEach(b => b.classList.remove('active'));
    switcher.querySelector('[data-section="general"]').classList.add('active');
    hcActive = false;
  }

  // v1.1.1: Wire up the Tera Pilot-sidebar-toggle button in hc-topbar.
  // The main topbar's sidebar toggle is covered by .hc-pane, so we need
  // our own button to toggle .app.sidebar-collapsed. Also mirror the
  // collapsed state to <body> for the CSS icon-rotation hook.
  if (hcTeraPilotSidebarToggle) {
    const appEl = document.querySelector('.app');
    const syncBodyClass = () => {
      if (!appEl) return;
      if (appEl.classList.contains('sidebar-collapsed')) {
        document.body.classList.add('hc-tera-pilot-collapsed');
      } else {
        document.body.classList.remove('hc-tera-pilot-collapsed');
      }
    };
    hcTeraPilotSidebarToggle.addEventListener('click', () => {
      if (!appEl) return;
      appEl.classList.toggle('sidebar-collapsed');
      syncBodyClass();
    });
    // Keep the body class in sync if the sidebar is toggled by any
    // other means (keyboard shortcut, original sidebar-toggle button
    // when HC pane is hidden, etc.).
    if (appEl) {
      const observer = new MutationObserver(syncBodyClass);
      observer.observe(appEl, { attributes: true, attributeFilter: ['class'] });
      syncBodyClass();
    }
  }

  // Quick-settings drawer (subagent mode / roles / capabilities) — floats
  // in from the right via the edge handle, closes on backdrop click or Esc.
  const hcDrawerHandle = document.getElementById('hcDrawerHandle');
  const hcSidebarDrawer = document.getElementById('hcSidebar');
  const hcDrawerBackdrop = document.getElementById('hcDrawerBackdrop');
  function setHCDrawerOpen(open) {
    if (hcSidebarDrawer) hcSidebarDrawer.classList.toggle('open', open);
    if (hcDrawerHandle) hcDrawerHandle.classList.toggle('open', open);
    if (hcDrawerBackdrop) hcDrawerBackdrop.classList.toggle('open', open);
  }
  if (hcDrawerHandle) {
    hcDrawerHandle.addEventListener('click', () => {
      setHCDrawerOpen(!hcSidebarDrawer.classList.contains('open'));
    });
  }
  if (hcDrawerBackdrop) {
    hcDrawerBackdrop.addEventListener('click', () => setHCDrawerOpen(false));
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && hcSidebarDrawer && hcSidebarDrawer.classList.contains('open')) {
      setHCDrawerOpen(false);
    }
  });

  // Override the original click handlers by attaching new ones
  // (the original IIFE runs first; ours run after and take precedence
  // for the heavycode case by stopping propagation).
  switcher.querySelectorAll('.section-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const section = btn.dataset.section;
      if (section === 'heavycode') {
        e.stopPropagation();
        showHCPane();
      } else if (section === 'general') {
        // Let the original handler close overlays, then ensure HC pane is hidden
        setTimeout(() => hideHCPane(), 0);
      } else if (section === 'office' && hcActive) {
        // v1.3.1 fix: switching straight to Office from Heavy Code must
        // close this pane too, or it's left stacked underneath/behind
        // the office pane and re-opening HC later looks "stuck".
        hideHCPane();
      }
    }, true);  // capture phase so we run first
  });

  if (hcBackBtn) {
    hcBackBtn.addEventListener('click', () => {
      hideHCPane();
      // Click the General button to restore original state
      const generalBtn = switcher.querySelector('[data-section="general"]');
      if (generalBtn) generalBtn.click();
    });
  }

  // ESC to close HC pane (in addition to existing ESC handler)
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && hcActive) {
      hideHCPane();
    }
  });

  // ── Mode selection ────────────────────────────────────────────
  document.querySelectorAll('.hc-mode-option').forEach(opt => {
    opt.addEventListener('change', () => {
      hcMode = opt.dataset.mode;
      document.querySelectorAll('.hc-mode-option').forEach(o => o.classList.remove('selected'));
      opt.classList.add('selected');
      const labels = {single: 'single', subagent: 'orchestrator + subagents', parallel: 'parallel multi-agents'};
      if (hcComposerHint) hcComposerHint.textContent = 'Multi-agent mode: ' + (labels[hcMode] || hcMode);
    });
  });

  // ── Quota refresh ─────────────────────────────────────────────
  async function refreshHCQuota() {
    if (!isBackendAvailable()) return;
    try {
      const stats = await callBridge('get_quota_stats');
      if (!stats || !stats.ok) return;
      const hc = (stats.sections || {}).heavy_code;
      if (!hc) return;
      const used = hc.used || 0;
      const limit = hc.limit || 10;
      const remaining = hc.remaining === -1 ? '∞' : hc.remaining;
      // Update topbar pill
      if (hcQuotaCount) hcQuotaCount.textContent = used + ' / ' + (limit === 0 ? '∞' : limit);
      if (hcQuotaPill) {
        hcQuotaPill.classList.remove('warn', 'exhausted');
        if (hc.exhausted) hcQuotaPill.classList.add('exhausted');
        else if (limit > 0 && remaining !== '∞' && remaining <= 2) hcQuotaPill.classList.add('warn');
      }
      // Update sidebar text
      if (hcQuotaText) hcQuotaText.textContent = (limit === 0 ? 'Unlimited' : (used + ' / ' + limit + ' used today'));
      // Update section-switcher badge
      if (hcQuotaBadge) {
        hcQuotaBadge.textContent = limit === 0 ? '∞' : (remaining === '∞' ? '∞' : remaining + ' left');
      }
    } catch (e) {
      console.warn('refreshHCQuota failed', e);
    }
  }

  if (hcResetQuotaBtn) {
    hcResetQuotaBtn.addEventListener('click', async () => {
      if (!isBackendAvailable()) { toast('Backend not connected', 'error'); return; }
      if (!confirm('Reset today\'s quota counter? This is a debug feature — normally the quota resets at 00:00 UTC.')) return;
      try {
        await callBridge('clear_quota_history');
        toast('Quota counter reset', 'success');
        refreshHCQuota();
      } catch (e) { toast('Failed: ' + e.message, 'error'); }
    });
  }

  // ── Suggestion chips ──────────────────────────────────────────
  document.querySelectorAll('.hc-suggestion').forEach(s => {
    s.addEventListener('click', () => {
      if (hcComposerInput) {
        hcComposerInput.value = s.dataset.prompt || '';
        hcComposerInput.focus();
        autosizeHC();
      }
    });
  });

  // ── Composer autosize + ⌘+Enter ───────────────────────────────
  function autosizeHC() {
    if (!hcComposerInput) return;
    hcComposerInput.style.height = 'auto';
    hcComposerInput.style.height = Math.max(60, Math.min(200, hcComposerInput.scrollHeight)) + 'px';
  }
  if (hcComposerInput) {
    hcComposerInput.addEventListener('input', autosizeHC);
    hcComposerInput.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        sendHCMessage();
      }
    });
  }

  // ── Send a Heavy Code message ─────────────────────────────────
  async function sendHCMessage() {
    if (!hcComposerInput) return;
    const text = hcComposerInput.value.trim();
    if (!text) return;

    if (hcIsGenerating) {
      // Cancel
      hcIsGenerating = false;
      updateHCSendBtn();
      if (isBackendAvailable()) {
        callBridge('stop_agent').catch(() => {});
        // v1.1.1: also cancel via HTTP (the HC agent runs via HTTP path)
        if (window.__apiBase) {
          fetch(window.__apiBase + '/api/agent/stop', {
            method: 'POST',
            headers: _apiHeaders()
          }).catch(() => {});
        }
      }
      return;
    }

    if (!isBackendAvailable()) {
      toast('Backend not connected', 'error');
      return;
    }

    if (!state.projectRoot) {
      toast('Open a project first (⌘O) — Heavy Code needs a workspace.', 'warning');
      return;
    }

    // Build the prompt — prepend mode/role hints for orchestrator/parallel modes
    let sendText = text;
    const roleHint = hcRoleHint ? hcRoleHint.value : 'auto';
    if (hcMode === 'subagent') {
      sendText = '[MULTI-AGENT MODE: orchestrator + subagents]\n' +
        (roleHint !== 'auto' ? `[PREFERRED SUBAGENT ROLE: ${roleHint}]\n` : '') +
        'Use the spawn_subagent tool to delegate sub-tasks to specialist subagents.\n\n' +
        'Task: ' + text;
    } else if (hcMode === 'parallel') {
      sendText = '[MULTI-AGENT MODE: parallel]\n' +
        'For independent sub-tasks, use the spawn_multi_agents tool to run them in parallel.\n\n' +
        'Task: ' + text;
    }

    // Append user message to chat
    appendHCMessage('user', text);
    hcComposerInput.value = '';
    autosizeHC();
    if (hcEmptyState) hcEmptyState.style.display = 'none';

    // Add an assistant placeholder
    const assistantEl = appendHCMessage('assistant', '');
    const bodyEl = assistantEl.querySelector('.hc-msg-body');
    bodyEl.classList.add('stream-cursor');

    hcIsGenerating = true;
    updateHCSendBtn();

    // Try HTTP path first, fall back to bridge
    if (window.__apiBase) {
      try {
        const resp = await fetch(window.__apiBase + '/api/agent/stream', {
          method: 'POST',
          headers: _apiHeaders(),
          body: JSON.stringify({
            text: sendText,
            chat_id: hcChatId,
            project_root: state.projectRoot,
            section: 'heavy_code',
          }),
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.error || 'HTTP ' + resp.status);
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';
          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            try {
              const data = JSON.parse(line.slice(6));
              handleHCSSE(data, bodyEl);
            } catch (e) {}
          }
        }
      } catch (e) {
        // Fall back to bridge
        await sendHCViaBridge(sendText, bodyEl);
      }
    } else {
      await sendHCViaBridge(sendText, bodyEl);
    }

    hcIsGenerating = false;
    updateHCSendBtn();
    bodyEl.classList.remove('stream-cursor');
    refreshHCQuota();
  }

  async function sendHCViaBridge(text, bodyEl) {
    try {
      const result = await callBridge('send_agent_message', {
        text: text,
        chat_id: hcChatId,
        section: 'heavy_code',
      });
      if (result && result.ok) {
        hcChatId = result.chat_id;
      } else if (result && result.error) {
        bodyEl.textContent = result.error;
        if (result.quota_exhausted) {
          refreshHCQuota();
        }
        return;
      }
    } catch (e) {
      bodyEl.textContent = 'Error: ' + e.message;
      return;
    }
    // Subscribe to agent_step_signal + agent_final signals
    if (window.bridge) {
      const onStep = (data) => {
        if (!data) return;
        handleHCSSE(data, bodyEl);
      };
      const onFinal = (data) => {
        if (!data) return;
        if (data.text) bodyEl.textContent = data.text;
        if (window.bridge.agent_step_signal) window.bridge.agent_step_signal.disconnect(onStep);
        if (window.bridge.agent_final) window.bridge.agent_final.disconnect(onFinal);
      };
      if (window.bridge.agent_step_signal) window.bridge.agent_step_signal.connect(onStep);
      if (window.bridge.agent_final) window.bridge.agent_final.connect(onFinal);
    }
  }

  function handleHCSSE(data, bodyEl) {
    if (data.type === 'chat_info') {
      hcChatId = data.chat_id;
      return;
    }
    if (data.type === 'step') {
      const isSubagent = data.subagent === true || data.parent_label;
      const stepEl = document.createElement('div');
      stepEl.className = 'hc-msg-step' + (isSubagent ? ' subagent' : '');
      const label = data.label || data.detail || '';
      let text = label;
      if (data.thought) text = data.thought;
      if (data.tool) {
        const args = data.args ? JSON.stringify(data.args).slice(0, 80) : '';
        text = `[${data.tool}] ${args}` + (data.thought ? '\n' + data.thought : '');
      }
      stepEl.textContent = (isSubagent ? (data.parent_label || 'subagent') + ': ' : '') + text.slice(0, 200);
      bodyEl.appendChild(stepEl);
      hcChatView.scrollTop = hcChatView.scrollHeight;
      return;
    }
    if (data.type === 'token') {
      bodyEl.textContent += data.content || '';
      hcChatView.scrollTop = hcChatView.scrollHeight;
      return;
    }
    if (data.type === 'done') {
      if (data.text) bodyEl.textContent = data.text;
      return;
    }
    if (data.type === 'error') {
      const errEl = document.createElement('div');
      errEl.style.color = '#ef4444';
      errEl.style.fontSize = '12px';
      errEl.textContent = '⚠ ' + (data.message || 'Unknown error');
      bodyEl.appendChild(errEl);
      if (data.quota_exhausted) refreshHCQuota();
      return;
    }
  }

  function appendHCMessage(role, content) {
    const div = document.createElement('div');
    div.className = 'hc-msg ' + role;
    const roleLabel = role === 'user' ? 'You' : 'Heavy Code';
    div.innerHTML = '<div class="hc-msg-role">' + roleLabel + '</div>' +
                    '<div class="hc-msg-body">' + escapeHtml(content) + '</div>';
    hcChatView.appendChild(div);
    hcChatView.scrollTop = hcChatView.scrollHeight;
    return div;
  }

  function escapeHtml(s) {
    if (!s) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function updateHCSendBtn() {
    if (!hcSendBtn) return;
    if (hcIsGenerating) {
      hcSendBtn.textContent = '⏹ Stop';
      hcSendBtn.style.background = '#ef4444';
    } else {
      hcSendBtn.textContent = 'Send';
      hcSendBtn.style.background = '';
    }
  }

  if (hcSendBtn) hcSendBtn.addEventListener('click', sendHCMessage);
  if (hcSettingsBtn) {
    hcSettingsBtn.addEventListener('click', () => {
      // Open settings modal and switch to agent tab
      const settingsBtn = document.getElementById('openSettingsBtn');
      if (settingsBtn) settingsBtn.click();
      setTimeout(() => {
        const agentTab = document.querySelector('.modal-tab[data-tab="agent"]');
        if (agentTab) agentTab.click();
      }, 100);
    });
  }

  // v2.0.0 — Collaboration mode button (Reviewer / Codegen / Pair / Observer)
  const hcCollabBtn = document.getElementById('hcRunCollabBtn');
  if (hcCollabBtn) {
    hcCollabBtn.addEventListener('click', async () => {
      const sel = document.getElementById('hcCollabMode');
      const mode = sel ? sel.value : 'single';
      const input = document.getElementById('hcComposerInput');
      const task = input ? input.value.trim() : '';
      if (mode === 'single') {
        toast('Pick a collaboration mode other than Single first.', 'warning');
        return;
      }
      if (!task) {
        toast('Type a task in the Heavy Code composer first.', 'warning');
        return;
      }
      if (!isBackendAvailable()) { toast('Backend not connected', 'error'); return; }
      hcCollabBtn.disabled = true;
      hcCollabBtn.textContent = 'Running...';
      try {
        const r = await callBridge('run_collaboration', mode, task);
        if (r && r.ok) {
          const meta = r.metadata || {};
          const verdict = meta.verdict ? `  |  verdict: ${meta.verdict}` : '';
          const iters = r.iterations ? `  |  iterations: ${r.iterations}` : '';
          toast(`Collaboration ${mode} complete${iters}${verdict}`, 'success');
          if (input) input.value = '';
          // Surface the output in the HC chat view
          const view = document.getElementById('hcChatView');
          if (view) {
            const msg = document.createElement('div');
            msg.className = 'msg assistant';
            const header = `<div class="msg-meta">${mode} · ${meta.verdict || 'done'}</div>`;
            const body = `<div class="msg-content">${(r.output || '').replace(/</g,'&lt;')}</div>`;
            msg.innerHTML = header + body;
            view.appendChild(msg);
            view.scrollTop = view.scrollHeight;
          }
        } else {
          toast('Collaboration failed: ' + (r && r.error ? r.error : 'unknown'), 'error');
        }
      } catch (e) {
        toast('Collaboration error: ' + e.message, 'error');
      } finally {
        hcCollabBtn.disabled = false;
        hcCollabBtn.textContent = 'Run collaboration on next prompt';
      }
    });
  }

  // Initial quota refresh
  setTimeout(refreshHCQuota, 1500);
  // Refresh every 60s while pane is visible
  setInterval(() => { if (hcActive) refreshHCQuota(); }, 60000);

  // Expose for debugging
  window.__hcRefreshQuota = refreshHCQuota;
})();

/* ===================================================================
   v1.1.0 — SETTINGS: MCP TAB + EXPANDED AGENT TAB
   =================================================================== */
(function(){
  // Extend renderSettingsTab to handle the new 'mcp' tab
  const _origRenderSettingsTab = window.renderSettingsTab;
  window.renderSettingsTab = async function(tab) {
    if (tab === 'mcp') {
      const body = document.getElementById('settingsBody');
      const footer = document.getElementById('modalFooter');
      footer.style.display = 'none';
      body.innerHTML = '';
      await renderMCPTab(body);
      return;
    }
    return _origRenderSettingsTab.apply(this, arguments);
  };

  async function renderMCPTab(body) {
    body.innerHTML = `
      <div class="settings-section">
        <div class="settings-section-title">MCP Servers</div>
        <p style="font-size:12px;color:var(--text-secondary);margin:0 0 var(--s-12) 0;line-height:1.5">
          MCP (Model Context Protocol) servers extend the agent with external tools —
          filesystem access, GitHub, browser automation, databases, and more.
          Available in <strong>all sections</strong> (General, Heavy Code, Office).
          Config is persisted to <code>~/.tera_pilot/mcp.json</code>.
        </p>
        <div id="mcpServersList" class="mcp-servers-list">Loading…</div>
        <button class="btn-secondary" id="mcpReloadBtn" style="margin-top:var(--s-12)">↻ Reload from disk</button>
        <button class="btn-primary" id="mcpAddBtn" style="margin-top:var(--s-12);margin-left:var(--s-8)">+ Add server</button>
      </div>
      <div class="settings-section" id="mcpAddFormSection" style="display:none">
        <div class="settings-section-title">Add new MCP server</div>
        <div class="mcp-add-form">
          <div>
            <label>Name</label>
            <input type="text" id="mcpAddName" placeholder="filesystem">
          </div>
          <div>
            <label>Command (space-separated)</label>
            <input type="text" id="mcpAddCommand" placeholder="npx -y @modelcontextprotocol/server-filesystem /tmp">
          </div>
          <div class="full-row">
            <label>Environment variables (one KEY=value per line, optional)</label>
            <textarea id="mcpAddEnv" rows="3" placeholder="GITHUB_TOKEN=ghp_xxx&#10;API_KEY=..."></textarea>
          </div>
          <div class="full-row" style="display:flex;gap:var(--s-8);justify-content:flex-end">
            <button class="btn-secondary" id="mcpAddCancel">Cancel</button>
            <button class="btn-primary" id="mcpAddSave">Save & start</button>
          </div>
        </div>
      </div>
      <div class="settings-section">
        <div class="settings-section-title">Popular MCP servers</div>
        <div style="font-size:11px;color:var(--text-secondary);line-height:1.6">
          <p style="margin:0 0 var(--s-8) 0"><strong>Filesystem</strong> — read/write files outside the project root:</p>
          <pre style="background:var(--bg-primary);padding:var(--s-8);border-radius:var(--r-sm);margin:0 0 var(--s-12) 0;font-size:10px;overflow-x:auto">npx -y @modelcontextprotocol/server-filesystem /path/to/allow</pre>
          <p style="margin:0 0 var(--s-8) 0"><strong>GitHub</strong> — read repos, issues, PRs:</p>
          <pre style="background:var(--bg-primary);padding:var(--s-8);border-radius:var(--r-sm);margin:0 0 var(--s-12) 0;font-size:10px;overflow-x:auto">npx -y @modelcontextprotocol/server-github
env: GITHUB_TOKEN=ghp_xxx</pre>
          <p style="margin:0 0 var(--s-8) 0"><strong>Browser (Playwright)</strong> — navigate, click, screenshot:</p>
          <pre style="background:var(--bg-primary);padding:var(--s-8);border-radius:var(--r-sm);margin:0;font-size:10px;overflow-x:auto">npx -y @modelcontextprotocol/server-playwright</pre>
        </div>
      </div>
    `;

    await loadMCPServers();

    document.getElementById('mcpReloadBtn').addEventListener('click', async () => {
      if (!isBackendAvailable()) { toast('Backend not connected', 'error'); return; }
      try {
        await callBridge('mcp_reload_config');
        toast('MCP config reloaded', 'success');
        await loadMCPServers();
      } catch (e) { toast('Failed: ' + e.message, 'error'); }
    });
    document.getElementById('mcpAddBtn').addEventListener('click', () => {
      document.getElementById('mcpAddFormSection').style.display = '';
      document.getElementById('mcpAddName').focus();
    });
    document.getElementById('mcpAddCancel').addEventListener('click', () => {
      document.getElementById('mcpAddFormSection').style.display = 'none';
    });
    document.getElementById('mcpAddSave').addEventListener('click', async () => {
      const name = document.getElementById('mcpAddName').value.trim();
      const command = document.getElementById('mcpAddCommand').value.trim();
      const envText = document.getElementById('mcpAddEnv').value.trim();
      if (!name || !command) { toast('Name and command are required', 'error'); return; }
      const env = {};
      envText.split('\n').forEach(line => {
        const idx = line.indexOf('=');
        if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
      });
      if (!isBackendAvailable()) { toast('Backend not connected', 'error'); return; }
      try {
        const result = await callBridge('mcp_add_server', { name, command: command.split(/\s+/), env, enabled: true, autostart: true });
        if (result.ok) {
          toast('MCP server added & started', 'success');
          document.getElementById('mcpAddName').value = '';
          document.getElementById('mcpAddCommand').value = '';
          document.getElementById('mcpAddEnv').value = '';
          document.getElementById('mcpAddFormSection').style.display = 'none';
          await loadMCPServers();
        } else {
          toast('Failed: ' + (result.error || 'unknown'), 'error');
        }
      } catch (e) { toast('Failed: ' + e.message, 'error'); }
    });
  }

  async function loadMCPServers() {
    const list = document.getElementById('mcpServersList');
    if (!list) return;
    if (!isBackendAvailable()) {
      list.innerHTML = '<div style="font-size:12px;color:var(--text-muted)">Backend not connected.</div>';
      return;
    }
    try {
      const result = await callBridge('mcp_list_servers');
      if (!result.ok || !result.servers || result.servers.length === 0) {
        list.innerHTML = '<div style="font-size:12px;color:var(--text-muted);padding:var(--s-12);text-align:center">No MCP servers configured. Click "Add server" to add one.</div>';
        return;
      }
      list.innerHTML = result.servers.map(s => `
        <div class="mcp-server-card ${s.running ? 'running' : 'stopped'}">
          <div class="mcp-server-info">
            <div class="mcp-server-name">
              ${escapeHtmlSimple(s.name)}
              <span class="mcp-server-status ${s.running ? 'running' : 'stopped'}">${s.running ? 'Running' : 'Stopped'}</span>
              ${!s.enabled ? '<span class="mcp-server-status stopped">Disabled</span>' : ''}
            </div>
            <div class="mcp-server-command">${escapeHtmlSimple((s.command || []).join(' '))}</div>
            <div class="mcp-server-meta">
              <span>Tools: <strong>${s.tool_count || 0}</strong></span>
              ${s.server_info && s.server_info.name ? '<span>Server: ' + escapeHtmlSimple(s.server_info.name + ' ' + (s.server_info.version || '')) + '</span>' : ''}
              ${s.env_keys && s.env_keys.length ? '<span>Env: ' + escapeHtmlSimple(s.env_keys.join(', ')) + '</span>' : ''}
            </div>
          </div>
          <div class="mcp-server-actions">
            ${s.running
              ? `<button class="btn-secondary" data-mcp-action="stop" data-name="${escapeHtmlSimple(s.name)}">Stop</button>`
              : `<button class="btn-secondary" data-mcp-action="start" data-name="${escapeHtmlSimple(s.name)}" ${!s.enabled ? 'disabled' : ''}>Start</button>`
            }
            <button class="btn-secondary" data-mcp-action="toggle" data-name="${escapeHtmlSimple(s.name)}" data-enabled="${!s.enabled}">${s.enabled ? 'Disable' : 'Enable'}</button>
            <button class="btn-secondary" data-mcp-action="remove" data-name="${escapeHtmlSimple(s.name)}" style="color:#ef4444">Remove</button>
          </div>
        </div>
      `).join('');
      // Wire action buttons
      list.querySelectorAll('button[data-mcp-action]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const action = btn.dataset.mcpAction;
          const name = btn.dataset.name;
          if (action === 'remove' && !confirm('Remove MCP server "' + name + '"?')) return;
          try {
            let result;
            if (action === 'start') result = await callBridge('mcp_start_server', name);
            else if (action === 'stop') result = await callBridge('mcp_stop_server', name);
            else if (action === 'toggle') result = await callBridge('mcp_toggle_server', name, btn.dataset.enabled === 'true');
            else if (action === 'remove') result = await callBridge('mcp_remove_server', name);
            if (result && result.ok) {
              toast('MCP: ' + action + ' ' + name + ' OK', 'success');
              await loadMCPServers();
            } else {
              toast('Failed: ' + (result && result.error || 'unknown'), 'error');
            }
          } catch (e) { toast('Failed: ' + e.message, 'error'); }
        });
      });
    } catch (e) {
      list.innerHTML = '<div style="font-size:12px;color:#ef4444">Failed to load: ' + escapeHtmlSimple(e.message) + '</div>';
    }
  }

  function escapeHtmlSimple(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  }

  // ── Expand the Agent tab with advanced settings ──────────────
  // We monkey-patch renderAgentTab to append the advanced controls
  // AFTER the existing autonomy + diff-review UI.
  const _origRenderAgentTab = window.renderAgentTab;
  window.renderAgentTab = async function(body) {
    // First call the original to render autonomy + diff-review
    await _origRenderAgentTab.apply(this, arguments);
    // Then append advanced settings
    await appendAdvancedAgentSettings(body);
  };

  async function appendAdvancedAgentSettings(body) {
    if (!isBackendAvailable()) return;
    let adv;
    try { adv = await callBridge('get_advanced_agent_settings'); } catch (e) { return; }
    if (!adv || !adv.ok) return;

    const a = adv.agent || {};
    const inf = adv.inference || {};

    const section = document.createElement('div');
    section.innerHTML = `
      <div class="advanced-section" style="margin-top:var(--s-16)">
        <div class="advanced-section-title">Agent runtime</div>
        <div class="advanced-row">
          <label for="advMaxIter">Max iterations</label>
          <div style="display:flex;align-items:center;gap:var(--s-8)">
            <input type="range" id="advMaxIter" min="1" max="50" value="${a.max_iterations || 8}">
            <span class="range-val" id="advMaxIterVal">${a.max_iterations || 8}</span>
          </div>
        </div>
        <div class="advanced-hint">How many tool-call iterations the agent can do before giving up. Heavy Code uses max(20, this value).</div>

        <div class="advanced-row">
          <label for="advRunTimeout">Run timeout (s)</label>
          <input type="number" id="advRunTimeout" min="1" max="600" value="${a.run_timeout || 15}">
        </div>
        <div class="advanced-hint">Per-tool execution timeout in seconds.</div>

        <div class="advanced-row">
          <label for="advEnablePlanning">Planning phase</label>
          <div style="display:flex;align-items:center;gap:var(--s-8)">
            <input type="checkbox" id="advEnablePlanning" ${a.enable_planning ? 'checked' : ''}>
            <span style="font-size:11px;color:var(--text-secondary)">Enabled</span>
          </div>
        </div>
        <div class="advanced-hint">When on, the agent writes a 3-5 step plan before any tool call. Off = direct execution.</div>

        <!-- v2.0.0-tui: close_on_switch option -->
        <div class="advanced-row">
          <label for="advCloseOnSwitch">Close on switch</label>
          <div style="display:flex;align-items:center;gap:var(--s-8)">
            <input type="checkbox" id="advCloseOnSwitch" ${settings.close_on_switch ? 'checked' : ''}>
            <span style="font-size:11px;color:var(--text-secondary)">Enabled</span>
          </div>
        </div>
        <div class="advanced-hint">When enabled, the current window (GUI or TUI) closes after launching the other interface.</div>
      </div>

      <div class="advanced-section">
        <div class="advanced-section-title">Context memory</div>
        <div class="advanced-row">
          <label for="advMaxMessages">Max messages</label>
          <input type="number" id="advMaxMessages" min="2" max="100" value="${a.memory_max_messages || 20}">
        </div>
        <div class="advanced-hint">Sliding-window size for conversation history. Older messages are dropped.</div>

        <div class="advanced-row">
          <label for="advMaxTokens">Max context tokens</label>
          <input type="number" id="advMaxTokens" min="1000" max="128000" step="500" value="${a.memory_max_tokens || 8000}">
        </div>
        <div class="advanced-hint">Auto-compaction kicks in when context approaches this limit.</div>
      </div>

      <div class="advanced-section">
        <div class="advanced-section-title">Inference — active provider: ${escapeHtmlSimple(inf.active_provider || '')}</div>
        <div class="advanced-row">
          <label for="advTemperature">Temperature</label>
          <div style="display:flex;align-items:center;gap:var(--s-8)">
            <input type="range" id="advTemperature" min="0" max="2" step="0.05" value="${inf.temperature != null ? inf.temperature : 0.2}">
            <span class="range-val" id="advTemperatureVal">${inf.temperature != null ? inf.temperature : 0.2}</span>
          </div>
        </div>
        <div class="advanced-hint">Lower = more deterministic. Higher = more creative. 0.2 is good for code; 0.7+ for prose.</div>

        <div class="advanced-row">
          <label for="advMaxTokens">Max tokens (output)</label>
          <input type="number" id="advMaxTokensInf" min="256" max="32768" step="256" value="${inf.max_tokens || 4096}">
        </div>
        <div class="advanced-hint">Maximum tokens the model can generate per response.</div>

        <div class="advanced-row">
          <label for="advTopP">Top-p</label>
          <div style="display:flex;align-items:center;gap:var(--s-8)">
            <input type="range" id="advTopP" min="0" max="1" step="0.05" value="${inf.top_p != null ? inf.top_p : 0.95}">
            <span class="range-val" id="advTopPVal">${inf.top_p != null ? inf.top_p : 0.95}</span>
          </div>
        </div>
        <div class="advanced-hint">Nucleus sampling. 1.0 = no filtering, 0.1 = only top 10% probability mass.</div>
      </div>

      <div class="advanced-section">
        <button class="btn-primary" id="advSaveBtn" style="width:100%;padding:8px">Save advanced settings</button>
      </div>
    `;
    body.appendChild(section);

    // Wire range sliders to live-update their values
    const wireRange = (sliderId, valId) => {
      const s = document.getElementById(sliderId);
      const v = document.getElementById(valId);
      if (s && v) s.addEventListener('input', () => { v.textContent = s.value; });
    };
    wireRange('advMaxIter', 'advMaxIterVal');
    wireRange('advTemperature', 'advTemperatureVal');
    wireRange('advTopP', 'advTopPVal');

    document.getElementById('advSaveBtn').addEventListener('click', async () => {
      const partial = {
        agent: {
          max_iterations: parseInt(document.getElementById('advMaxIter').value, 10),
          enable_planning: document.getElementById('advEnablePlanning').checked,
          run_timeout: parseInt(document.getElementById('advRunTimeout').value, 10),
          memory_max_messages: parseInt(document.getElementById('advMaxMessages').value, 10),
          memory_max_tokens: parseInt(document.getElementById('advMaxTokens').value, 10),
        },
        inference: {
          temperature: parseFloat(document.getElementById('advTemperature').value),
          max_tokens: parseInt(document.getElementById('advMaxTokensInf').value, 10),
          top_p: parseFloat(document.getElementById('advTopP').value),
        },
        close_on_switch: document.getElementById('advCloseOnSwitch').checked,
      };
      try {
        const result = await callBridge('save_advanced_agent_settings', partial);
        if (result && result.ok) {
          toast('Advanced agent settings saved', 'success');
        } else {
          toast('Failed: ' + (result && result.error || 'unknown'), 'error');
        }
      } catch (e) { toast('Failed: ' + e.message, 'error'); }
    });
  }

  // ── MCP indicator in statusbar (refreshed periodically) ──────
  function addMCPIndicator() {
    const statusbar = document.querySelector('.statusbar');
    if (!statusbar) return;
    if (document.getElementById('mcpIndicator')) return;
    const ind = document.createElement('div');
    ind.id = 'mcpIndicator';
    ind.className = 'mcp-indicator';
    ind.innerHTML = '<span class="mcp-indicator-dot"></span><span class="mcp-indicator-text">MCP: 0</span>';
    ind.title = 'Model Context Protocol servers — click to open MCP settings';
    ind.addEventListener('click', () => {
      const sb = document.getElementById('openSettingsBtn');
      if (sb) sb.click();
      setTimeout(() => {
        const mcpTab = document.querySelector('.modal-tab[data-tab="mcp"]');
        if (mcpTab) mcpTab.click();
      }, 100);
    });
    // Try to insert before the gitBar (or any reasonable spot)
    const firstChild = statusbar.firstElementChild;
    if (firstChild) statusbar.insertBefore(ind, firstChild);
    else statusbar.appendChild(ind);
  }

  async function refreshMCPIndicator() {
    if (!isBackendAvailable()) return;
    try {
      const r = await callBridge('mcp_list_servers');
      const ind = document.getElementById('mcpIndicator');
      if (!ind) return;
      if (!r || !r.ok) {
        ind.classList.remove('active');
        ind.querySelector('.mcp-indicator-text').textContent = 'MCP: off';
        return;
      }
      const running = (r.servers || []).filter(s => s.running);
      const totalTools = r.total_tools || 0;
      if (running.length > 0) {
        ind.classList.add('active');
        ind.querySelector('.mcp-indicator-text').textContent = `MCP: ${running.length} server${running.length === 1 ? '' : 's'} · ${totalTools} tool${totalTools === 1 ? '' : 's'}`;
      } else {
        ind.classList.remove('active');
        ind.querySelector('.mcp-indicator-text').textContent = 'MCP: off';
      }
    } catch (e) {}
  }

  // Defer indicator setup until DOM is fully ready
  setTimeout(() => {
    addMCPIndicator();
    refreshMCPIndicator();
    setInterval(refreshMCPIndicator, 30000);
  }, 2000);
})();
// ============================================================
/* ===================================================================
   v1.2.0 — SWARM ROSTER (Boris pattern: parallel agent sessions)
   =================================================================== */
(function(){
  const spawnBtn = document.getElementById('swarmSpawnBtn');
  const cleanupBtn = document.getElementById('swarmCleanupBtn');
  const list = document.getElementById('swarmList');
  const empty = document.getElementById('swarmEmpty');
  const form = document.getElementById('swarmSpawnForm');
  const nameInput = document.getElementById('swarmNameInput');
  const goalInput = document.getElementById('swarmGoalInput');
  const roleSelect = document.getElementById('swarmRoleSelect');
  const cancelBtn = document.getElementById('swarmCancelBtn');
  const confirmBtn = document.getElementById('swarmConfirmSpawnBtn');
  const indicator = document.getElementById('swarmIndicator');
  const indicatorCount = document.getElementById('swarmIndicatorCount');

  let swarmAgents = [];

  function renderSwarmList(){
    if(!list) return;
    if(swarmAgents.length === 0){
      list.innerHTML = '';
      list.appendChild(empty);
      empty.style.display = 'block';
      if(indicator) indicator.style.display = 'none';
      return;
    }
    empty.style.display = 'none';
    if(indicator){
      indicator.style.display = 'inline-flex';
      indicatorCount.textContent = swarmAgents.length;
    }
    list.innerHTML = swarmAgents.map(function(a){
      var statusLabel = a.status.charAt(0).toUpperCase() + a.status.slice(1);
      return '<div class="swarm-card" data-id="'+a.id+'">' +
        '<div class="swarm-card-header">' +
          '<span class="swarm-card-name" title="'+esc(a.name)+'">'+esc(a.name)+'</span>' +
          '<span class="swarm-card-role">'+esc(a.role)+'</span>' +
        '</div>' +
        '<div class="swarm-card-goal" title="'+esc(a.goal)+'">'+esc(a.goal)+'</div>' +
        '<div class="swarm-card-footer">' +
          '<span class="swarm-card-status" data-status="'+a.status+'"><span class="dot"></span>'+statusLabel+'</span>' +
          '<button class="swarm-card-remove" data-remove-id="'+a.id+'" title="Remove agent">' +
            '<svg width="10" height="10" viewBox="0 0 14 14" fill="none"><path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>' +
          '</button>' +
        '</div>' +
      '</div>';
    }).join('');

    list.querySelectorAll('.swarm-card-remove').forEach(function(btn){
      btn.addEventListener('click', function(){
        var id = this.getAttribute('data-remove-id');
        if(isBackendAvailable()){
          callBridge('swarm_remove', id).then(function(){
            refreshSwarm();
          });
        }
      });
    });
  }

  function esc(s){
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function refreshSwarm(){
    if(!isBackendAvailable()) return;
    callBridge('swarm_list').then(function(res){
      if(res && res.ok){
        swarmAgents = res.agents || [];
        renderSwarmList();
      }
    });
  }

  if(spawnBtn){
    spawnBtn.addEventListener('click', function(){
      form.style.display = form.style.display === 'none' ? 'block' : 'none';
      if(form.style.display === 'block') nameInput.focus();
    });
  }
  if(cleanupBtn){
    cleanupBtn.addEventListener('click', function(){
      if(isBackendAvailable()){
        callBridge('swarm_cleanup').then(function(){
          refreshSwarm();
          toast('Swarm cleaned up');
        });
      }
    });
  }
  if(cancelBtn){
    cancelBtn.addEventListener('click', function(){
      form.style.display = 'none';
    });
  }
  if(confirmBtn){
    confirmBtn.addEventListener('click', function(){
      var name = nameInput.value.trim() || ('Agent #' + (swarmAgents.length + 1));
      var goal = goalInput.value.trim();
      if(!goal){ toast('Please enter a goal for the agent'); return; }
      var role = roleSelect.value;
      if(isBackendAvailable()){
        callBridge('swarm_spawn', name, goal, role).then(function(){
          nameInput.value = '';
          goalInput.value = '';
          form.style.display = 'none';
          refreshSwarm();
          toast('Agent "' + name + '" spawned');
        });
      }
    });
  }

  // Expose refresh for other modules
  window.__refreshSwarm = refreshSwarm;

  // Auto-refresh every 3s when swarm panel is visible
  setInterval(function(){
    var swarmSection = document.getElementById('swarmSection');
    if(swarmSection && swarmSection.offsetParent !== null){
      refreshSwarm();
    }
  }, 3000);
})();

/* ===================================================================
   v1.2.0 — OFFICE WORKER PANE
   =================================================================== */
(function(){
  const officePane = document.getElementById('officePane');
  const officeChatView = document.getElementById('officeChatView');
  const officeComposerInput = document.getElementById('officeComposerInput');
  const officeSendBtn = document.getElementById('officeSendBtn');
  const officeBackBtn = document.getElementById('officeBackBtn');
  const officeTeraPilotSidebarToggle = document.getElementById('officeTeraPilotSidebarToggle');
  const officeComposerHint = document.getElementById('officeComposerHint');
  const switcher = document.querySelector('.section-switcher');
  const csBackdrop = document.getElementById('csBackdrop');

  if (!officePane || !switcher) return;

  let officeActive = false;
  let officeDocType = 'docx';

  function showOfficePane() {
    officePane.classList.add('office-active');
    if (csBackdrop) csBackdrop.classList.remove('visible');
    switcher.querySelectorAll('.section-btn').forEach(b => b.classList.remove('active'));
    switcher.querySelector('[data-section="office"]').classList.add('active');
    officeActive = true;
    officeComposerInput && officeComposerInput.focus();
  }

  function hideOfficePane() {
    officePane.classList.remove('office-active');
    if (csBackdrop) csBackdrop.classList.remove('visible');
    switcher.querySelectorAll('.section-btn').forEach(b => b.classList.remove('active'));
    switcher.querySelector('[data-section="general"]').classList.add('active');
    officeActive = false;
  }

  // Override section switcher for office
  switcher.querySelectorAll('.section-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const section = btn.dataset.section;
      if (section === 'office') {
        e.stopPropagation();
        showOfficePane();
      } else if (section === 'general' && officeActive) {
        hideOfficePane();
      } else if (section === 'heavycode' && officeActive) {
        // v1.3.1 fix: this branch was missing entirely, so clicking
        // "Heavy Code" while Office Worker was open left the Office
        // pane's full-stage overlay on top — Heavy Code silently
        // activated underneath it and looked like nothing happened.
        hideOfficePane();
      }
    }, true);
  });

  // Back button
  if (officeBackBtn) {
    officeBackBtn.addEventListener('click', () => {
      hideOfficePane();
      const generalBtn = switcher.querySelector('[data-section="general"]');
      if (generalBtn) generalBtn.click();
    });
  }

  // ESC to close office pane
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && officeActive) {
      hideOfficePane();
    }
  });

  // Document type selection
  document.querySelectorAll('.office-doc-type').forEach(opt => {
    opt.addEventListener('change', () => {
      officeDocType = opt.dataset.type;
      document.querySelectorAll('.office-doc-type').forEach(o => o.classList.remove('selected'));
      opt.classList.add('selected');
      const labels = {docx: 'Word (.docx)', xlsx: 'Excel (.xlsx)', pptx: 'PowerPoint (.pptx)'};
      if (officeComposerHint) officeComposerHint.textContent = 'Document type: ' + (labels[officeDocType] || officeDocType);
    });
  });

  // Sidebar toggle
  if (officeTeraPilotSidebarToggle) {
    const appEl = document.querySelector('.app');
    const syncBodyClass = () => {
      if (!appEl) return;
      if (appEl.classList.contains('sidebar-collapsed')) {
        document.body.classList.add('office-tera-pilot-collapsed');
      } else {
        document.body.classList.remove('office-tera-pilot-collapsed');
      }
    };
    officeTeraPilotSidebarToggle.addEventListener('click', () => {
      if (!appEl) return;
      appEl.classList.toggle('sidebar-collapsed');
      syncBodyClass();
    });
    if (appEl) {
      const observer = new MutationObserver(syncBodyClass);
      observer.observe(appEl, { attributes: true, attributeFilter: ['class'] });
      syncBodyClass();
    }
  }

  // Quick-settings drawer (document type / capabilities) — floats in from
  // the right via the edge handle, closes on backdrop click or Esc.
  const officeDrawerHandle = document.getElementById('officeDrawerHandle');
  const officeSidebarDrawer = document.getElementById('officeSidebar');
  const officeDrawerBackdrop = document.getElementById('officeDrawerBackdrop');
  function setOfficeDrawerOpen(open) {
    if (officeSidebarDrawer) officeSidebarDrawer.classList.toggle('open', open);
    if (officeDrawerHandle) officeDrawerHandle.classList.toggle('open', open);
    if (officeDrawerBackdrop) officeDrawerBackdrop.classList.toggle('open', open);
  }
  if (officeDrawerHandle) {
    officeDrawerHandle.addEventListener('click', () => {
      setOfficeDrawerOpen(!officeSidebarDrawer.classList.contains('open'));
    });
  }
  if (officeDrawerBackdrop) {
    officeDrawerBackdrop.addEventListener('click', () => setOfficeDrawerOpen(false));
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && officeSidebarDrawer && officeSidebarDrawer.classList.contains('open')) {
      setOfficeDrawerOpen(false);
    }
  });

  // Send button
  if (officeSendBtn) {
    officeSendBtn.addEventListener('click', async () => {
      const prompt = officeComposerInput.value.trim();
      if (!prompt) return;

      if (!isBackendAvailable()) {
        toast('Backend not available', 'error');
        return;
      }

      officeComposerInput.value = '';
      officeComposerInput.style.height = 'auto';
      officeComposerInput.rows = 3;

      try {
        // Send message with office section context
        const opts = {
          text: prompt,
          section: 'office',
          doctype: officeDocType
        };
        const result = await callBridge('send_message', opts);
        if (result && result.ok) {
          // Message was sent successfully
          if (officeComposerInput) officeComposerInput.focus();
        }
      } catch (e) {
        toast('Error: ' + (e.message || e), 'error');
      }
    });
  }

  // Ctrl/Cmd+Enter to send
  if (officeComposerInput) {
    officeComposerInput.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        officeSendBtn && officeSendBtn.click();
      }
    });
    // Auto-resize textarea
    officeComposerInput.addEventListener('input', () => {
      officeComposerInput.style.height = 'auto';
      officeComposerInput.style.height = Math.min(officeComposerInput.scrollHeight, 200) + 'px';
    });
  }

  // Suggestion chips
  document.querySelectorAll('.office-suggestion').forEach(s => {
    s.addEventListener('click', () => {
      if (officeComposerInput) {
        officeComposerInput.value = s.dataset.prompt;
        officeComposerInput.focus();
        officeComposerInput.style.height = 'auto';
        officeComposerInput.style.height = Math.min(officeComposerInput.scrollHeight, 200) + 'px';
      }
    });
  });

  console.log('[office] v1.2.0 Office Worker pane loaded');
})();
// ── Custom tooltips ──────────────────────────────────────────────────
// Replaces the native browser `title` tooltip (a plain, unstyled OS box)
// with a small tooltip bubble that matches the rest of the UI. Works for
// every element with a `title` attribute — static ones in index.html and
// ones app.js sets dynamically (e.g. sendBtn.title = '...') — without
// needing to touch each call site individually.
(function () {
  let tipEl = null;
  let currentTarget = null;
  let showTimer = null;

  function ensureTipEl() {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.className = 'custom-tooltip';
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }

  function position(target) {
    const tip = ensureTipEl();
    const r = target.getBoundingClientRect();
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let top = r.top - th - 8;
    let left = r.left + r.width / 2 - tw / 2;
    if (top < 4) top = r.bottom + 8;
    left = Math.max(6, Math.min(left, window.innerWidth - tw - 6));
    tip.style.top = top + 'px';
    tip.style.left = left + 'px';
  }

  function show(target) {
    const text = target.getAttribute('title');
    if (!text) return;
    target.setAttribute('data-original-title', text);
    target.removeAttribute('title');
    currentTarget = target;
    const tip = ensureTipEl();
    tip.textContent = text;
    tip.classList.remove('visible');
    position(target);
    requestAnimationFrame(function () {
      if (currentTarget !== target) return;
      position(target);
      tip.classList.add('visible');
    });
  }

  function hide() {
    if (currentTarget) {
      const orig = currentTarget.getAttribute('data-original-title');
      if (orig && !currentTarget.getAttribute('title')) {
        currentTarget.setAttribute('title', orig);
      }
      currentTarget.removeAttribute('data-original-title');
    }
    currentTarget = null;
    clearTimeout(showTimer);
    if (tipEl) tipEl.classList.remove('visible');
  }

  document.addEventListener('mouseover', function (e) {
    const el = e.target.closest && e.target.closest('[title]');
    if (!el || el === currentTarget) return;
    if (currentTarget) hide();
    clearTimeout(showTimer);
    showTimer = setTimeout(function () { show(el); }, 350);
  }, true);

  document.addEventListener('mouseout', function (e) {
    const el = e.target.closest && e.target.closest('[title],[data-original-title]');
    if (!el) return;
    if (e.relatedTarget && el.contains(e.relatedTarget)) return;
    hide();
  }, true);

  document.addEventListener('mousedown', hide, true);
  document.addEventListener('scroll', function () { if (currentTarget) hide(); }, true);
  window.addEventListener('blur', hide);
})();
