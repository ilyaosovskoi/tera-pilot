/* ===================================================================
   TERA_PILOT v2.2.1 — tools_panels.js
   ===================================================================

   Implements the unified Tools panel that exposes every backend
   capability previously reachable only through the TUI slash commands.

   Each sidebar item with class ``.tools-nav`` and a ``data-tool``
   attribute opens the panel with the corresponding renderer.

   The panel talks to the new /api/* endpoints installed by
   ``tera_pilot.api_extended`` — it never imports TUI code itself.

   Renderers
   ---------
   • capabilities   — browse & run capability templates
   • hooks          — register / list / test / toggle / remove hooks
   • checkpoints    — create / list / rewind / diff / auto
   • handoffs       — create / list / inspect / accept-reject blocks
   • github         — token / repo / PRs / issues / actions
   • audit          — agent identity + signed audit trail
   • spend          — team spend dashboard
   • consensus      — multi-provider consensus config & run
   • second_opinion — cross-model second-opinion config & run
   • verify         — verify last response with another model
   • learnings      — list / dismiss / restore / scan
   • github_actions — generate GitHub Action workflow YAML
   • notify         — configure Telegram/Discord/Slack
   • daemon         — submit background tasks + status
   • mcp_server     — Tera Pilot-as-MCP-server status + tool list
   • persona        — system-prompt persona editor
   • providers      — custom provider wizard (Nvidia NIM, OpenAI-compat, …)
*/

(function(){
  'use strict';
  if (window.__teraPilotToolsPanelsInstalled) return;
  window.__teraPilotToolsPanelsInstalled = true;

  // ── Helpers ────────────────────────────────────────────────────────
  function _apiBase(){ return window.__apiBase || ''; }
  function _apiHeaders(){
    var h = {'Content-Type': 'application/json'};
    if (window.__apiToken) h['Authorization'] = 'Bearer ' + window.__apiToken;
    return h;
  }
  async function _get(path){
    var resp = await fetch(_apiBase() + path, {headers: _apiHeaders()});
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
  }
  async function _post(path, body){
    var resp = await fetch(_apiBase() + path, {
      method: 'POST', headers: _apiHeaders(), body: JSON.stringify(body || {})
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return resp.json();
  }
  function _toast(msg, kind){
    if (typeof window.toast === 'function') return window.toast(msg, kind);
    console.log('[tools]', kind || '', msg);
  }
  function _esc(s){
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function _fmtTime(ts){
    if (!ts) return '—';
    var d = new Date(ts * 1000);
    return d.toLocaleString();
  }
  function _fmtBytes(n){
    n = n || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024*1024) return (n/1024).toFixed(1) + ' KB';
    return (n/1024/1024).toFixed(1) + ' MB';
  }

  // ── Panel state ────────────────────────────────────────────────────
  var panel = document.getElementById('toolsPanel');
  var backdrop = document.getElementById('toolsBackdrop');
  var body = document.getElementById('toolsPanelBody');
  var titleEl = document.getElementById('toolsPanelTitle');
  var subtitleEl = document.getElementById('toolsPanelSubtitle');
  var footerEl = document.getElementById('toolsPanelFooter');
  var currentTool = null;

  function openPanel(tool){
    currentTool = tool;
    panel.classList.add('open');
    backdrop.classList.add('open');
    document.body.classList.add('tools-panel-open');
    render();
  }
  function closePanel(){
    panel.classList.remove('open');
    backdrop.classList.remove('open');
    document.body.classList.remove('tools-panel-open');
    currentTool = null;
  }
  async function render(){
    if (!currentTool) return;
    var meta = TOOL_META[currentTool] || {title: currentTool, subtitle: ''};
    titleEl.textContent = meta.title;
    subtitleEl.textContent = meta.subtitle;
    footerEl.style.display = 'none';
    footerEl.innerHTML = '';
    body.innerHTML = '<div class="tools-loading">Loading…</div>';
    try {
      var fn = RENDERERS[currentTool];
      if (!fn) throw new Error('No renderer for ' + currentTool);
      await fn(body);
    } catch(e){
      console.error('[tools] render error', e);
      body.innerHTML = '<div class="tools-error">' + _esc(e.message) + '</div>';
    }
  }

  // ── Tool metadata ──────────────────────────────────────────────────
  var TOOL_META = {
    // v2.3.4: Heavy Code / Office open their full panes (see app.js
    // __teraPilotOpenHeavyCode / __teraPilotOpenOffice) — the metadata
    // only feeds the Settings Tools grid cards.
    heavy_code:     {title: 'Heavy Code', subtitle: 'Multi-agent coding — subagents + parallel execution'},
    office:         {title: 'Office Worker', subtitle: 'Create and edit .docx / .xlsx / .pptx files'},
    capabilities:   {title: 'Capability Catalog', subtitle: 'Pre-built prompt templates — run with one click'},
    hooks:          {title: 'Hooks', subtitle: 'Intercept, modify, or block tool calls'},
    checkpoints:    {title: 'Checkpoints', subtitle: 'Snapshot state and rewind agent mistakes'},
    handoffs:       {title: 'Handoffs', subtitle: 'Post-task editable handoff documents'},
    github:         {title: 'GitHub', subtitle: 'PR/issue automation + implementation context'},
    audit:          {title: 'Audit Trail', subtitle: 'Agent identity + signed offline audit log'},
    spend:          {title: 'Spend Dashboard', subtitle: 'Team token usage + cost'},
    consensus:      {title: 'Consensus', subtitle: 'Run the same prompt across 2–3 providers'},
    second_opinion: {title: 'Second Opinion', subtitle: 'Cross-model review of an agent response'},
    verify:         {title: 'Verify', subtitle: 'Cross-model verification of the last response'},
    learnings:      {title: 'Learnings', subtitle: 'Auto-detected learnings (rollbacks, CI failures)'},
    github_actions: {title: 'GitHub Actions', subtitle: 'Generate workflow YAML templates'},
    notify:         {title: 'Notifications', subtitle: 'Telegram / Discord / Slack webhooks'},
    daemon:         {title: 'Daemon', subtitle: 'Background task queue + remote execution'},
    mcp_server:     {title: 'MCP Server', subtitle: 'Tera Pilot-as-MCP-server status + tool list'},
    persona:        {title: 'Persona', subtitle: 'System-prompt persona editor'},
    providers:      {title: 'Custom Providers', subtitle: 'Add Nvidia NIM, OpenAI-compatible endpoints, and more'},
  };

  // ═══════════════════════════════════════════════════════════════════
  // RENDERERS
  // ═══════════════════════════════════════════════════════════════════

  var RENDERERS = {};

  // ── Capabilities ───────────────────────────────────────────────────
  RENDERERS.capabilities = async function(root){
    var data = await _get('/api/capabilities/list');
    var caps = (data && data.capabilities) || [];
    if (!caps.length){
      root.innerHTML = '<div class="tools-empty">No capability templates found.</div>';
      return;
    }
    var html = '<div class="tools-grid">';
    caps.forEach(function(c){
      html += '<div class="cap-card" data-cap-id="' + _esc(c.id) + '">' +
        '<div class="cap-card-header">' +
          '<div class="cap-card-icon">' + _esc((c.category || '◆').slice(0,1)) + '</div>' +
          '<div><div class="cap-card-title">' + _esc(c.title || c.id) + '</div>' +
          '<div class="cap-card-cat">' + _esc(c.category || '') + '</div></div>' +
        '</div>' +
        '<div class="cap-card-desc">' + _esc(c.description || '') + '</div>' +
        '<div class="cap-card-actions">' +
          '<button class="btn-secondary" data-action="view" data-id="' + _esc(c.id) + '">View</button>' +
          '<button class="btn-primary" data-action="run" data-id="' + _esc(c.id) + '">Run</button>' +
        '</div>' +
      '</div>';
    });
    html += '</div>';
    root.innerHTML = html;
    root.querySelectorAll('[data-action="run"]').forEach(function(btn){
      btn.addEventListener('click', function(){ runCapability(btn.dataset.id); });
    });
    root.querySelectorAll('[data-action="view"]').forEach(function(btn){
      btn.addEventListener('click', function(){ viewCapability(btn.dataset.id); });
    });
  };

  async function viewCapability(id){
    try {
      var data = await _get('/api/capabilities/get?id=' + encodeURIComponent(id));
      var cap = data.capability;
      if (!cap){ _toast('Capability not found', 'error'); return; }
      var html = '<div class="cap-detail">' +
        '<h3>' + _esc(cap.title || cap.id) + '</h3>' +
        '<div class="cap-detail-cat">Category: ' + _esc(cap.category || '') + '</div>' +
        '<p>' + _esc(cap.description || '') + '</p>';
      if (cap.variables && cap.variables.length){
        html += '<div class="cap-vars"><div class="cap-vars-title">Variables:</div><ul>';
        cap.variables.forEach(function(v){
          html += '<li><code>' + _esc(v.name) + '</code>' +
            (v.required ? ' <span class="req">required</span>' : '') +
            (v.description ? ' — ' + _esc(v.description) : '') + '</li>';
        });
        html += '</ul></div>';
      }
      if (cap.template){
        html += '<div class="cap-template"><div class="cap-vars-title">Template:</div><pre>' + _esc(cap.template) + '</pre></div>';
      }
      html += '</div>';
      body.innerHTML = html;
    } catch(e){ _toast(e.message, 'error'); }
  }

  async function runCapability(id){
    try {
      var data = await _get('/api/capabilities/get?id=' + encodeURIComponent(id));
      var cap = data.capability;
      if (!cap){ _toast('Capability not found', 'error'); return; }
      var html = '<div class="cap-run"><h3>Run: ' + _esc(cap.title || id) + '</h3>';
      if (cap.variables && cap.variables.length){
        html += '<div class="cap-run-vars">';
        cap.variables.forEach(function(v){
          html += '<label class="cap-var-input">' +
            '<span>' + _esc(v.name) + (v.required ? ' *' : '') + '</span>' +
            '<input type="text" data-var="' + _esc(v.name) + '" placeholder="' + _esc(v.description || '') + '">' +
          '</label>';
        });
        html += '</div>';
      }
      html += '<div class="cap-run-actions">' +
        '<button class="btn-secondary" id="capRunCancel">Cancel</button>' +
        '<button class="btn-primary" id="capRunSubmit">Generate prompt</button>' +
      '</div>' +
      '<div class="cap-run-output" id="capRunOutput" style="display:none"></div>' +
      '</div>';
      body.innerHTML = html;
      document.getElementById('capRunCancel').addEventListener('click', render);
      document.getElementById('capRunSubmit').addEventListener('click', async function(){
        var vars = {};
        body.querySelectorAll('[data-var]').forEach(function(inp){
          vars[inp.dataset.var] = inp.value;
        });
        try {
          var result = await _post('/api/capabilities/run', {id: id, variables: vars});
          var out = document.getElementById('capRunOutput');
          out.style.display = '';
          var prompt = (result && (result.prompt || result.text)) || JSON.stringify(result, null, 2);
          out.innerHTML = '<div class="cap-vars-title">Generated prompt:</div><pre>' + _esc(prompt) + '</pre>' +
            '<button class="btn-primary" id="capRunSend">Send to chat</button>';
          document.getElementById('capRunSend').addEventListener('click', function(){
            var inp = document.getElementById('composerInput');
            if (inp){ inp.value = prompt; inp.dispatchEvent(new Event('input')); }
            closePanel();
          });
        } catch(e){ _toast(e.message, 'error'); }
      });
    } catch(e){ _toast(e.message, 'error'); }
  }


  // ── Hooks ──────────────────────────────────────────────────────────
  RENDERERS.hooks = async function(root){
    var data = await _get('/api/hooks/list');
    var hooks = (data && data.hooks) || [];
    var stats = await _get('/api/hooks/stats').catch(function(){ return {}; });
    var html = '<div class="tools-stats-row">';
    html += '<div class="stat-pill"><span class="stat-pill-label">Total</span><span class="stat-pill-value">' + (stats.total || hooks.length) + '</span></div>';
    html += '<div class="stat-pill"><span class="stat-pill-label">Enabled</span><span class="stat-pill-value">' + (stats.enabled || 0) + '</span></div>';
    html += '<div class="stat-pill"><span class="stat-pill-label">Blocked</span><span class="stat-pill-value">' + (stats.blocked || 0) + '</span></div>';
    html += '<div class="stat-pill"><span class="stat-pill-label">Modified</span><span class="stat-pill-value">' + (stats.modified || 0) + '</span></div>';
    html += '</div>';
    html += '<div class="tools-actions"><button class="btn-primary" id="hookNewBtn">+ New hook</button></div>';
    if (!hooks.length){
      html += '<div class="tools-empty">No hooks registered. Create one to intercept tool calls.</div>';
    } else {
      html += '<table class="tools-table"><thead><tr>' +
        '<th>Name</th><th>Type</th><th>Priority</th><th>Status</th><th>Actions</th>' +
      '</tr></thead><tbody>';
      hooks.forEach(function(h){
        html += '<tr>' +
          '<td><strong>' + _esc(h.name || h.hook_id) + '</strong><div class="tools-row-sub">' + _esc(h.hook_id) + '</div></td>' +
          '<td>' + _esc(h.hook_type) + '</td>' +
          '<td>' + _esc(h.priority || 100) + '</td>' +
          '<td>' + (h.enabled ? '<span class="badge badge-success">enabled</span>' : '<span class="badge badge-muted">disabled</span>') + '</td>' +
          '<td class="tools-row-actions">' +
            '<button class="btn-mini" data-action="toggle" data-id="' + _esc(h.hook_id) + '" data-enabled="' + (!h.enabled) + '">' + (h.enabled ? 'Disable' : 'Enable') + '</button>' +
            '<button class="btn-mini" data-action="test" data-id="' + _esc(h.hook_id) + '">Test</button>' +
            '<button class="btn-mini btn-danger" data-action="remove" data-id="' + _esc(h.hook_id) + '">Remove</button>' +
          '</td>' +
        '</tr>';
      });
      html += '</tbody></table>';
    }
    root.innerHTML = html;
    root.querySelectorAll('[data-action="toggle"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          await _post('/api/hooks/toggle', {hook_id: btn.dataset.id, enabled: btn.dataset.enabled === 'true'});
          _toast('Hook updated', 'success');
          render();
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="remove"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        if (!confirm('Remove hook?')) return;
        try {
          await _post('/api/hooks/remove', {hook_id: btn.dataset.id});
          _toast('Hook removed', 'success');
          render();
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="test"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          var r = await _post('/api/hooks/test', {hook_id: btn.dataset.id, event_type: 'pre_tool_use'});
          _toast('Hook test: ' + (r.verdict || r.action || 'OK'), 'success');
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    var newBtn = root.querySelector('#hookNewBtn');
    if (newBtn) newBtn.addEventListener('click', function(){ showHookEditor(); });
  };

  function showHookEditor(){
    body.innerHTML = '<div class="cap-run"><h3>New hook</h3>' +
      '<label class="cap-var-input"><span>Name</span><input type="text" id="hookName" placeholder="e.g. block-rm-rf"></label>' +
      '<label class="cap-var-input"><span>Type</span><select id="hookType">' +
        '<option value="pre_tool_use">pre_tool_use (can BLOCK/MODIFY)</option>' +
        '<option value="post_tool_use">post_tool_use (informational)</option>' +
        '<option value="user_prompt_submit">user_prompt_submit (can BLOCK/MODIFY)</option>' +
      '</select></label>' +
      '<label class="cap-var-input"><span>Priority (lower runs first)</span><input type="number" id="hookPriority" value="100"></label>' +
      '<label class="cap-var-input"><span>Python code</span><textarea id="hookCode" rows="10" placeholder="def register_hooks(manager):&#10;    from tera_pilot.hook_system import HookEvent, HookResult&#10;    def my_hook(event):&#10;        if &#39;rm -rf&#39; in str(event.data.get(&#39;args&#39;, &#39;&#39;))&#10;            return HookResult(action=&#39;BLOCK&#39;, reason=&#39;rm -rf not allowed&#39;)&#10;        return HookResult(action=&#39;ALLOW&#39;)&#10;    manager.register(&#39;pre_tool_use&#39;, my_hook, name=&#39;block-rm-rf&#39;)"></textarea></label>' +
      '<div class="cap-run-actions">' +
        '<button class="btn-secondary" id="hookCancel">Cancel</button>' +
        '<button class="btn-primary" id="hookSave">Save hook</button>' +
      '</div></div>';
    document.getElementById('hookCancel').addEventListener('click', render);
    document.getElementById('hookSave').addEventListener('click', async function(){
      try {
        await _post('/api/hooks/register', {
          name: document.getElementById('hookName').value,
          hook_type: document.getElementById('hookType').value,
          priority: parseInt(document.getElementById('hookPriority').value || '100'),
          code: document.getElementById('hookCode').value,
          enabled: true,
        });
        _toast('Hook registered', 'success');
        render();
      } catch(e){ _toast(e.message, 'error'); }
    });
  }


  // ── Checkpoints ────────────────────────────────────────────────────
  RENDERERS.checkpoints = async function(root){
    var list = await _get('/api/checkpoint/list');
    var stats = await _get('/api/checkpoint/stats').catch(function(){ return {}; });
    var cps = (list && list.checkpoints) || [];
    var html = '<div class="tools-stats-row">' +
      '<div class="stat-pill"><span class="stat-pill-label">Total</span><span class="stat-pill-value">' + (stats.total || cps.length) + '</span></div>' +
      '<div class="stat-pill"><span class="stat-pill-label">Auto-checkpoint</span><span class="stat-pill-value">' + (stats.auto_enabled ? 'ON' : 'OFF') + '</span></div>' +
      '<div class="stat-pill"><span class="stat-pill-label">Disk</span><span class="stat-pill-value">' + _fmtBytes(stats.disk_bytes) + '</span></div>' +
    '</div>';
    html += '<div class="tools-actions">' +
      '<button class="btn-primary" id="cpCreateBtn">+ Create checkpoint</button>' +
      '<button class="btn-secondary" id="cpAutoBtn">' + (stats.auto_enabled ? 'Disable auto' : 'Enable auto') + '</button>' +
    '</div>';
    if (!cps.length){
      html += '<div class="tools-empty">No checkpoints yet. Create one to snapshot state.</div>';
    } else {
      html += '<table class="tools-table"><thead><tr>' +
        '<th>When</th><th>Label</th><th>ID</th><th>Actions</th>' +
      '</tr></thead><tbody>';
      cps.forEach(function(c, i){
        html += '<tr>' +
          '<td>' + _fmtTime(c.created_at) + '</td>' +
          '<td>' + _esc(c.label || '(unlabelled)') + '</td>' +
          '<td><code>' + _esc(c.checkpoint_id) + '</code></td>' +
          '<td class="tools-row-actions">' +
            '<button class="btn-mini" data-action="rewind" data-id="' + _esc(c.checkpoint_id) + '">Rewind to here</button>' +
            (i === 0 ? '' : '<button class="btn-mini" data-action="diff" data-id="' + _esc(c.checkpoint_id) + '">Diff vs latest</button>') +
          '</td>' +
        '</tr>';
      });
      html += '</tbody></table>';
    }
    root.innerHTML = html;
    root.querySelector('#cpCreateBtn').addEventListener('click', async function(){
      var label = prompt('Checkpoint label (optional):', '');
      if (label === null) return;
      try {
        await _post('/api/checkpoint/create', {label: label || ''});
        _toast('Checkpoint created', 'success');
        render();
      } catch(e){ _toast(e.message, 'error'); }
    });
    root.querySelector('#cpAutoBtn').addEventListener('click', async function(){
      try {
        await _post('/api/checkpoint/auto', {enabled: !stats.auto_enabled});
        _toast('Auto-checkpoint ' + (!stats.auto_enabled ? 'enabled' : 'disabled'), 'success');
        render();
      } catch(e){ _toast(e.message, 'error'); }
    });
    root.querySelectorAll('[data-action="rewind"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        if (!confirm('Rewind to this checkpoint? File changes after this point will be reverted.')) return;
        try {
          var r = await _post('/api/checkpoint/rewind_to', {checkpoint_id: btn.dataset.id});
          _toast('Rewound to checkpoint (' + (r.message_count || 0) + ' messages retained)', 'success');
          render();
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="diff"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          var r = await _post('/api/checkpoint/diff', {from_id: btn.dataset.id, to_id: cps[0].checkpoint_id});
          var out = body.querySelector('#cpDiffOut') || document.createElement('div');
          out.id = 'cpDiffOut';
          out.className = 'tools-output';
          out.innerHTML = '<div class="cap-vars-title">Diff:</div><pre>' + _esc(r.diff || r.summary || JSON.stringify(r, null, 2)) + '</pre>';
          body.appendChild(out);
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
  };


  // ── Handoffs ───────────────────────────────────────────────────────
  RENDERERS.handoffs = async function(root){
    var data = await _get('/api/handoff/list');
    var docs = (data && data.handoffs) || [];
    var html = '<div class="tools-actions"><button class="btn-primary" id="handoffNewBtn">+ New handoff</button></div>';
    if (!docs.length){
      html += '<div class="tools-empty">No handoff documents yet. Create one from an agent response.</div>';
    } else {
      html += '<table class="tools-table"><thead><tr>' +
        '<th>Title</th><th>Created</th><th>Blocks</th><th>Actions</th>' +
      '</tr></thead><tbody>';
      docs.forEach(function(d){
        html += '<tr>' +
          '<td><strong>' + _esc(d.title || d.doc_id) + '</strong><div class="tools-row-sub">' + _esc(d.doc_id) + '</div></td>' +
          '<td>' + _fmtTime(d.created_at) + '</td>' +
          '<td>' + (d.block_count || 0) + '</td>' +
          '<td class="tools-row-actions">' +
            '<button class="btn-mini" data-action="view" data-id="' + _esc(d.doc_id) + '">View</button>' +
            '<button class="btn-mini" data-action="md" data-id="' + _esc(d.doc_id) + '">Markdown</button>' +
            '<button class="btn-mini" data-action="revise" data-id="' + _esc(d.doc_id) + '">Build revision</button>' +
            '<button class="btn-mini btn-danger" data-action="delete" data-id="' + _esc(d.doc_id) + '">Delete</button>' +
          '</td>' +
        '</tr>';
      });
      html += '</tbody></table>';
    }
    root.innerHTML = html;
    root.querySelector('#handoffNewBtn').addEventListener('click', function(){ showHandoffEditor(); });
    root.querySelectorAll('[data-action="view"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          var r = await _get('/api/handoff/get?id=' + encodeURIComponent(btn.dataset.id));
          var h = r.handoff;
          if (!h){ _toast('Handoff not found', 'error'); return; }
          var html2 = '<div class="handoff-detail"><div class="tools-actions">' +
            '<button class="btn-secondary" id="handoffBack">Back</button></div>' +
            '<h3>' + _esc(h.title) + '</h3>';
          (h.blocks || []).forEach(function(b){
            var status = b.status || 'pending';
            var statusClass = status === 'accepted' ? 'badge-success' : (status === 'rejected' ? 'badge-danger' : 'badge-muted');
            html2 += '<div class="handoff-block block-type-' + _esc(b.type) + '">' +
              '<div class="handoff-block-header">' +
                '<span class="badge ' + statusClass + '">' + _esc(status) + '</span>' +
                '<span class="handoff-block-type">' + _esc(b.type) + '</span>' +
              '</div>';
            if (b.type === 'code' || b.type === 'file_diff'){
              html2 += '<pre>' + _esc(b.content) + '</pre>';
            } else {
              html2 += '<div class="handoff-block-content">' + _esc(b.content) + '</div>';
            }
            html2 += '<div class="handoff-block-actions">' +
              '<button class="btn-mini" data-action="accept" data-doc="' + _esc(h.doc_id) + '" data-block="' + _esc(b.id) + '">Accept</button>' +
              '<button class="btn-mini" data-action="reject" data-doc="' + _esc(h.doc_id) + '" data-block="' + _esc(b.id) + '">Reject</button>' +
              (b.type === 'todo' ? '<button class="btn-mini" data-action="todo" data-doc="' + _esc(h.doc_id) + '" data-block="' + _esc(b.id) + '">Toggle done</button>' : '') +
            '</div></div>';
          });
          html2 += '</div>';
          body.innerHTML = html2;
          body.querySelector('#handoffBack').addEventListener('click', render);
          body.querySelectorAll('[data-action="accept"]').forEach(function(b2){
            b2.addEventListener('click', async function(){
              await _post('/api/handoff/block_status', {doc_id: b2.dataset.doc, block_id: b2.dataset.block, status: 'accepted'});
              _toast('Block accepted', 'success');
              btn.click();
            });
          });
          body.querySelectorAll('[data-action="reject"]').forEach(function(b2){
            b2.addEventListener('click', async function(){
              await _post('/api/handoff/block_status', {doc_id: b2.dataset.doc, block_id: b2.dataset.block, status: 'rejected'});
              _toast('Block rejected', 'success');
              btn.click();
            });
          });
          body.querySelectorAll('[data-action="todo"]').forEach(function(b2){
            b2.addEventListener('click', async function(){
              await _post('/api/handoff/todo_toggle', {doc_id: b2.dataset.doc, block_id: b2.dataset.block});
              btn.click();
            });
          });
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="md"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          var r = await _post('/api/handoff/export_md', {doc_id: btn.dataset.id});
          var out = body.querySelector('#handoffMdOut') || document.createElement('div');
          out.id = 'handoffMdOut';
          out.className = 'tools-output';
          out.innerHTML = '<div class="cap-vars-title">Markdown:</div><pre>' + _esc(r.markdown || r.content || JSON.stringify(r)) + '</pre>';
          body.appendChild(out);
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="revise"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          var r = await _post('/api/handoff/revision_prompt', {doc_id: btn.dataset.id});
          var inp = document.getElementById('composerInput');
          if (inp && r.prompt){
            inp.value = r.prompt;
            inp.dispatchEvent(new Event('input'));
            _toast('Revision prompt sent to composer', 'success');
            closePanel();
          } else {
            _toast('No prompt generated', 'error');
          }
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="delete"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        if (!confirm('Delete handoff?')) return;
        await _post('/api/handoff/delete', {doc_id: btn.dataset.id});
        _toast('Handoff deleted', 'success');
        render();
      });
    });
  };

  function showHandoffEditor(){
    body.innerHTML = '<div class="cap-run"><h3>New handoff</h3>' +
      '<label class="cap-var-input"><span>Title</span><input type="text" id="handoffTitle" placeholder="e.g. Implement feature X"></label>' +
      '<label class="cap-var-input"><span>Agent output (paste full text)</span><textarea id="handoffOutput" rows="10" placeholder="Paste the agent response here — Tera Pilot will parse it into typed blocks."></textarea></label>' +
      '<div class="cap-run-actions">' +
        '<button class="btn-secondary" id="handoffCancel">Cancel</button>' +
        '<button class="btn-primary" id="handoffCreate">Create</button>' +
      '</div></div>';
    document.getElementById('handoffCancel').addEventListener('click', render);
    document.getElementById('handoffCreate').addEventListener('click', async function(){
      try {
        await _post('/api/handoff/create', {
          title: document.getElementById('handoffTitle').value,
          agent_output: document.getElementById('handoffOutput').value,
          source_section: 'general',
        });
        _toast('Handoff created', 'success');
        render();
      } catch(e){ _toast(e.message, 'error'); }
    });
  }


  // ── GitHub ─────────────────────────────────────────────────────────
  RENDERERS.github = async function(root){
    var status = await _get('/api/github/status').catch(function(){ return {}; });
    var prs = {items: []};
    var issues = {items: []};
    if (status.authenticated && status.repo){
      try { prs = await _get('/api/github/list_prs?state=open&limit=10'); } catch(e){}
      try { issues = await _get('/api/github/list_issues?state=open&limit=10'); } catch(e){}
    }
    var html = '<div class="tools-section">' +
      '<div class="cap-vars-title">Repository</div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">Authed</span><span class="stat-pill-value">' + (status.authenticated ? 'Yes' : 'No') + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Repo</span><span class="stat-pill-value">' + _esc(status.repo || '—') + '</span></div>' +
      '</div>' +
      '<div class="tools-actions">' +
        '<button class="btn-secondary" id="ghTokenBtn">Set token</button>' +
        '<button class="btn-secondary" id="ghRepoBtn">Set repo</button>' +
        '<button class="btn-secondary" id="ghDetectBtn">Auto-detect repo</button>' +
        '<button class="btn-primary" id="ghNewPrBtn">Create PR</button>' +
        '<button class="btn-primary" id="ghNewIssueBtn">Create issue</button>' +
      '</div>' +
    '</div>';

    html += '<div class="tools-section"><div class="cap-vars-title">Open PRs</div>';
    if (!prs.items || !prs.items.length){
      html += '<div class="tools-empty">No open PRs.</div>';
    } else {
      html += '<table class="tools-table"><thead><tr><th>#</th><th>Title</th><th>Author</th><th>Updated</th><th>Actions</th></tr></thead><tbody>';
      prs.items.forEach(function(p){
        html += '<tr><td>#' + p.number + '</td><td>' + _esc(p.title) + '</td><td>' + _esc(p.user || p.author || '') + '</td><td>' + _fmtTime(p.updated_at) + '</td>' +
          '<td class="tools-row-actions">' +
            '<button class="btn-mini" data-action="pr-context" data-num="' + p.number + '">Implement context</button>' +
            '<button class="btn-mini" data-action="pr-comment" data-num="' + p.number + '">Comment</button>' +
          '</td></tr>';
      });
      html += '</tbody></table>';
    }
    html += '</div>';

    html += '<div class="tools-section"><div class="cap-vars-title">Open Issues</div>';
    if (!issues.items || !issues.items.length){
      html += '<div class="tools-empty">No open issues.</div>';
    } else {
      html += '<table class="tools-table"><thead><tr><th>#</th><th>Title</th><th>Labels</th><th>Actions</th></tr></thead><tbody>';
      issues.items.forEach(function(i){
        html += '<tr><td>#' + i.number + '</td><td>' + _esc(i.title) + '</td><td>' + _esc((i.labels || []).join(', ')) + '</td>' +
          '<td class="tools-row-actions">' +
            '<button class="btn-mini" data-action="issue-view" data-num="' + i.number + '">View</button>' +
          '</td></tr>';
      });
      html += '</tbody></table>';
    }
    html += '</div>';

    root.innerHTML = html;

    root.querySelector('#ghTokenBtn').addEventListener('click', function(){
      var t = prompt('GitHub token (will be stored at ~/.tera_pilot/github_token):', '');
      if (t === null) return;
      _post('/api/github/set_token', {token: t}).then(function(){ _toast('Token saved', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#ghRepoBtn').addEventListener('click', function(){
      var r = prompt('Repo (owner/repo):', '');
      if (r === null) return;
      var parts = r.split('/');
      if (parts.length !== 2){ _toast('Use owner/repo format', 'error'); return; }
      _post('/api/github/set_repo', {owner: parts[0], repo: parts[1]}).then(function(){ _toast('Repo set', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#ghDetectBtn').addEventListener('click', function(){
      _post('/api/github/detect_repo', {}).then(function(r){ _toast('Detected: ' + (r.repo || r.full_name || 'none'), 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#ghNewPrBtn').addEventListener('click', function(){
      var title = prompt('PR title:', '');
      if (!title) return;
      var head = prompt('Head branch (feature branch):', '');
      var base = prompt('Base branch:', 'main');
      var body = prompt('PR body (optional):', '');
      _post('/api/github/create_pr', {title: title, head: head, base: base, body: body}).then(function(){ _toast('PR created', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#ghNewIssueBtn').addEventListener('click', function(){
      var title = prompt('Issue title:', '');
      if (!title) return;
      var body = prompt('Issue body (optional):', '');
      _post('/api/github/create_issue', {title: title, body: body}).then(function(){ _toast('Issue created', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelectorAll('[data-action="pr-context"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          var r = await _get('/api/github/pr_context?number=' + btn.dataset.num);
          var inp = document.getElementById('composerInput');
          if (inp && r.prompt){
            inp.value = r.prompt;
            inp.dispatchEvent(new Event('input'));
            _toast('Implementation context loaded into composer', 'success');
            closePanel();
          }
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="pr-comment"]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var c = prompt('Comment body:', '');
        if (c === null) return;
        _post('/api/github/comment_pr', {number: parseInt(btn.dataset.num), body: c}).then(function(){ _toast('Comment posted', 'success'); }).catch(function(e){ _toast(e.message, 'error'); });
      });
    });
    root.querySelectorAll('[data-action="issue-view"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          var r = await _get('/api/github/get_issue?number=' + btn.dataset.num);
          body.innerHTML = '<div class="tools-actions"><button class="btn-secondary" id="ghBack">Back</button></div>' +
            '<h3>#' + r.number + ' — ' + _esc(r.title) + '</h3>' +
            '<div class="tools-output"><pre>' + _esc(r.body || '') + '</pre></div>';
          body.querySelector('#ghBack').addEventListener('click', render);
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
  };


  // ── Audit ──────────────────────────────────────────────────────────
  RENDERERS.audit = async function(root){
    var identity = await _get('/api/agents/identity').catch(function(){ return {}; });
    var summary = await _get('/api/audit/summary').catch(function(){ return {}; });
    var agents = await _get('/api/agents/list').catch(function(){ return {agents: []}; });
    var html = '<div class="tools-section"><div class="cap-vars-title">Agent identity</div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">ID</span><span class="stat-pill-value">' + _esc(identity.id || '—') + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Role</span><span class="stat-pill-value">' + _esc(identity.role || '—') + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Name</span><span class="stat-pill-value">' + _esc(identity.name || '—') + '</span></div>' +
      '</div></div>';
    html += '<div class="tools-section"><div class="cap-vars-title">Audit summary</div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">Total entries</span><span class="stat-pill-value">' + (summary.total_entries || 0) + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Distinct agents</span><span class="stat-pill-value">' + (summary.distinct_agents || 0) + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Tools called</span><span class="stat-pill-value">' + (summary.tools_called || 0) + '</span></div>' +
      '</div></div>';
    html += '<div class="tools-actions">' +
      '<button class="btn-secondary" id="auditSpawnBtn">Spawn subidentity</button>' +
      '<button class="btn-secondary" id="auditExportJsonBtn">Export JSON</button>' +
      '<button class="btn-secondary" id="auditExportCsvBtn">Export CSV</button>' +
      '<button class="btn-primary" id="auditSignedBtn">Export signed (Ed25519)</button>' +
    '</div>';
    if (agents.agents && agents.agents.length){
      html += '<table class="tools-table"><thead><tr><th>Agent ID</th><th>Role</th><th>Name</th><th>Parent</th></tr></thead><tbody>';
      agents.agents.forEach(function(a){
        html += '<tr><td><code>' + _esc(a.id) + '</code></td><td>' + _esc(a.role) + '</td><td>' + _esc(a.name) + '</td><td>' + _esc(a.parent_id || '—') + '</td></tr>';
      });
      html += '</tbody></table>';
    }
    root.innerHTML = html;
    root.querySelector('#auditSpawnBtn').addEventListener('click', function(){
      var role = prompt('Role (researcher / implementer / planner):', 'researcher');
      if (!role) return;
      var name = prompt('Name (optional):', '');
      _post('/api/agents/spawn', {role: role, name: name || ''}).then(function(r){ _toast('Spawned: ' + (r.id || 'OK'), 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#auditExportJsonBtn').addEventListener('click', async function(){
      var r = await _get('/api/audit/export_json');
      downloadJson('audit.json', r);
    });
    root.querySelector('#auditExportCsvBtn').addEventListener('click', async function(){
      var r = await _get('/api/audit/export_csv');
      downloadText('audit.csv', r.csv || r.data || '');
    });
    root.querySelector('#auditSignedBtn').addEventListener('click', async function(){
      var r = await _get('/api/audit/signed_export');
      downloadJson('audit-signed.json', r);
      _toast('Signed audit exported — Ed25519 key at ~/.tera_pilot/audit_key', 'success');
    });
  };


  // ── Spend Dashboard ────────────────────────────────────────────────
  RENDERERS.spend = async function(root){
    var identity = await _get('/api/spend/identity').catch(function(){ return {}; });
    var budget = await _get('/api/spend/budget').catch(function(){ return {}; });
    var report = await _get('/api/spend/report?days=30').catch(function(){ return {}; });
    var sources = await _get('/api/spend/sources').catch(function(){ return {sources: []}; });
    var html = '<div class="tools-section"><div class="cap-vars-title">Identity</div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">User</span><span class="stat-pill-value">' + _esc(identity.name || identity.user_id || '—') + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Team</span><span class="stat-pill-value">' + _esc(identity.team || '—') + '</span></div>' +
      '</div>' +
      '<div class="tools-actions">' +
        '<button class="btn-secondary" id="spendTeamBtn">Set team</button>' +
      '</div></div>';
    var spent = report.totals ? (report.totals.cost_usd || 0) : 0;
    var cap = budget.monthly_usd || 0;
    var pct = cap > 0 ? Math.min(100, (spent / cap) * 100) : 0;
    html += '<div class="tools-section"><div class="cap-vars-title">Budget (last 30 days)</div>' +
      '<div class="progress-bar"><div class="progress-fill" style="width:' + pct + '%;background:' + (pct > 80 ? 'var(--danger)' : 'var(--success)') + '"></div></div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">Spent</span><span class="stat-pill-value">$' + spent.toFixed(4) + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Cap</span><span class="stat-pill-value">$' + (cap || 0).toFixed(2) + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Alert at</span><span class="stat-pill-value">' + (budget.alert_pct || 80) + '%</span></div>' +
      '</div>' +
      '<div class="tools-actions">' +
        '<button class="btn-secondary" id="spendBudgetBtn">Set budget</button>' +
        '<button class="btn-secondary" id="spendAddSourceBtn">Add token_history source</button>' +
        '<button class="btn-secondary" id="spendExportJsonBtn">Export JSON</button>' +
        '<button class="btn-secondary" id="spendExportCsvBtn">Export CSV</button>' +
      '</div></div>';
    if (sources.sources && sources.sources.length){
      html += '<div class="tools-section"><div class="cap-vars-title">Token-history sources</div><ul class="tools-list">';
      sources.sources.forEach(function(s){
        html += '<li><code>' + _esc(s.path || s) + '</code></li>';
      });
      html += '</ul></div>';
    }
    if (report.by_provider){
      html += '<div class="tools-section"><div class="cap-vars-title">By provider</div><table class="tools-table"><thead><tr><th>Provider</th><th>Tokens in</th><th>Tokens out</th><th>Cost</th></tr></thead><tbody>';
      Object.keys(report.by_provider).forEach(function(p){
        var v = report.by_provider[p] || {};
        html += '<tr><td>' + _esc(p) + '</td><td>' + (v.tokens_in || 0) + '</td><td>' + (v.tokens_out || 0) + '</td><td>$' + ((v.cost_usd || 0)).toFixed(4) + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }
    root.innerHTML = html;
    root.querySelector('#spendTeamBtn').addEventListener('click', function(){
      var t = prompt('Team name:', identity.team || '');
      if (t === null) return;
      _post('/api/spend/team', {team: t}).then(function(){ _toast('Team set', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#spendBudgetBtn').addEventListener('click', function(){
      var m = prompt('Monthly USD cap:', String(budget.monthly_usd || 50));
      if (m === null) return;
      var a = prompt('Alert at (%):', String(budget.alert_pct || 80));
      if (a === null) return;
      _post('/api/spend/budget', {monthly_usd: parseFloat(m), alert_pct: parseFloat(a)}).then(function(){ _toast('Budget set', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#spendAddSourceBtn').addEventListener('click', function(){
      var p = prompt('Path to token_history.jsonl (or directory of *.jsonl):', '');
      if (p === null) return;
      _post('/api/spend/sources_add', {path: p}).then(function(){ _toast('Source added', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#spendExportJsonBtn').addEventListener('click', async function(){
      var r = await _get('/api/spend/export_json?days=30');
      downloadJson('spend-report.json', r);
    });
    root.querySelector('#spendExportCsvBtn').addEventListener('click', async function(){
      var r = await _get('/api/spend/export_csv?days=30');
      downloadText('spend-report.csv', r.csv || r.data || '');
    });
  };


  // ── Consensus ──────────────────────────────────────────────────────
  RENDERERS.consensus = async function(root){
    var cfg = await _get('/api/consensus/config').catch(function(){ return {}; });
    var html = '<div class="tools-section"><div class="cap-vars-title">Configuration</div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">Min agreement</span><span class="stat-pill-value">' + (cfg.min_agreement || 0.7) + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Timeout</span><span class="stat-pill-value">' + (cfg.timeout || 30) + 's</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Max chars</span><span class="stat-pill-value">' + (cfg.max_chars_per_response || 4000) + '</span></div>' +
      '</div>' +
      '<div class="tools-actions">' +
        '<button class="btn-secondary" id="consCfgBtn">Configure</button>' +
      '</div></div>';
    html += '<div class="tools-section"><div class="cap-vars-title">Run consensus</div>' +
      '<textarea id="consensusPrompt" rows="4" placeholder="Prompt to send to all providers in parallel…"></textarea>' +
      '<div class="tools-actions"><button class="btn-primary" id="consensusRunBtn">Run</button></div>' +
      '<div id="consensusOutput" class="tools-output" style="display:none"></div></div>';
    root.innerHTML = html;
    root.querySelector('#consCfgBtn').addEventListener('click', function(){
      var ma = prompt('Min agreement (0.0–1.0):', String(cfg.min_agreement || 0.7));
      if (ma === null) return;
      var to = prompt('Per-provider timeout (s):', String(cfg.timeout || 30));
      if (to === null) return;
      _post('/api/consensus/config', {min_agreement: parseFloat(ma), timeout: parseInt(to)}).then(function(){ _toast('Saved', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#consensusRunBtn').addEventListener('click', async function(){
      var prompt = document.getElementById('consensusPrompt').value;
      if (!prompt){ _toast('Enter a prompt', 'error'); return; }
      var out = document.getElementById('consensusOutput');
      out.style.display = '';
      out.innerHTML = '<div class="tools-loading">Running consensus across providers…</div>';
      try {
        var r = await _post('/api/consensus/run', {prompt: prompt});
        var html2 = '<div class="cap-vars-title">Agreement: ' + ((r.agreement_score || 0) * 100).toFixed(0) + '%</div>';
        if (r.divergences && r.divergences.length){
          html2 += '<ul class="tools-list">';
          r.divergences.forEach(function(d){
            html2 += '<li><strong>' + _esc(d.kind) + '</strong>: ' + _esc(d.summary || d.reason || '') + '</li>';
          });
          html2 += '</ul>';
        }
        if (r.responses){
          html2 += '<div class="cap-vars-title">Per-provider responses:</div>';
          Object.keys(r.responses).forEach(function(p){
            var resp = r.responses[p];
            html2 += '<div class="tools-output"><div class="cap-vars-title">' + _esc(p) + (resp.error ? ' <span class="badge badge-danger">error</span>' : '') + '</div><pre>' + _esc(resp.content || resp.error || '') + '</pre></div>';
          });
        }
        out.innerHTML = html2;
      } catch(e){ out.innerHTML = '<div class="tools-error">' + _esc(e.message) + '</div>'; }
    });
  };


  // ── Second Opinion ─────────────────────────────────────────────────
  RENDERERS.second_opinion = async function(root){
    var cfg = await _get('/api/second_opinion/config').catch(function(){ return {}; });
    var providers = await _get('/api/second_opinion/providers').catch(function(){ return {providers: []}; });
    var html = '<div class="tools-section"><div class="cap-vars-title">Configuration</div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">Pro</span><span class="stat-pill-value">' + (cfg.pro_enabled ? 'ON' : 'OFF') + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Default provider</span><span class="stat-pill-value">' + _esc(cfg.default_provider || '—') + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Default model</span><span class="stat-pill-value">' + _esc(cfg.default_model || '—') + '</span></div>' +
      '</div>' +
      '<div class="tools-actions"><button class="btn-secondary" id="soCfgBtn">Configure</button></div></div>';
    html += '<div class="tools-section"><div class="cap-vars-title">Run second opinion</div>' +
      '<label class="cap-var-input"><span>Original prompt</span><textarea id="soPrompt" rows="3" placeholder="The user\'s original prompt"></textarea></label>' +
      '<label class="cap-var-input"><span>Agent response</span><textarea id="soResponse" rows="5" placeholder="The agent\'s response to verify"></textarea></label>' +
      '<label class="cap-var-input"><span>Reviewer provider</span><select id="soProvider">' +
        (providers.providers || []).map(function(p){ return '<option value="' + _esc(p.id) + '">' + _esc(p.label || p.id) + '</option>'; }).join('') +
      '</select></label>' +
      '<div class="tools-actions"><button class="btn-primary" id="soRunBtn">Run second opinion</button></div>' +
      '<div id="soOutput" class="tools-output" style="display:none"></div></div>';
    root.innerHTML = html;
    root.querySelector('#soCfgBtn').addEventListener('click', function(){
      // v2.3.4: the backend bridge expects provider_id/model (not the old
      // default_provider/default_model keys, which were silently ignored).
      var p = prompt('Default reviewer provider:', cfg.provider_id || '');
      if (p === null) return;
      var m = prompt('Default reviewer model:', cfg.model || '');
      if (m === null) return;
      _post('/api/second_opinion/config', {provider_id: p, model: m}).then(function(){ _toast('Saved', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#soRunBtn').addEventListener('click', async function(){
      var prompt = document.getElementById('soPrompt').value;
      var response = document.getElementById('soResponse').value;
      var provider = document.getElementById('soProvider').value;
      if (!prompt || !response){ _toast('Prompt and response are required', 'error'); return; }
      var out = document.getElementById('soOutput');
      out.style.display = '';
      out.innerHTML = '<div class="tools-loading">Running second opinion…</div>';
      try {
        var r = await _post('/api/second_opinion/run', {prompt: prompt, response: response, provider_id: provider});
        out.innerHTML = '<div class="cap-vars-title">Verdict: <span class="badge ' + (r.verdict === 'APPROVE' ? 'badge-success' : (r.verdict === 'REJECT' ? 'badge-danger' : 'badge-muted')) + '">' + _esc(r.verdict || 'UNKNOWN') + '</span></div>' +
          '<pre>' + _esc(r.review || r.content || JSON.stringify(r, null, 2)) + '</pre>';
      } catch(e){ out.innerHTML = '<div class="tools-error">' + _esc(e.message) + '</div>'; }
    });
  };


  // ── Verify ─────────────────────────────────────────────────────────
  RENDERERS.verify = async function(root){
    var cfg = await _get('/api/second_opinion/config').catch(function(){ return {}; });
    var providers = await _get('/api/second_opinion/providers').catch(function(){ return {providers: []}; });
    var lastMsg = '';
    try {
      var msgs = document.querySelectorAll('#chatView .msg');
      if (msgs.length > 1){
        var last = msgs[msgs.length - 1];
        var body2 = last.querySelector('.msg-body, .msg-content, .markdown-body');
        if (body2) lastMsg = body2.textContent || '';
      }
    } catch(e){}
    var html = '<div class="tools-section"><div class="cap-vars-title">Verify last response</div>' +
      '<p class="tools-note">Cross-model verification runs an independent reviewer on the agent\'s last answer.</p>' +
      '<label class="cap-var-input"><span>User request</span><textarea id="verifyRequest" rows="2" placeholder="The original user prompt"></textarea></label>' +
      '<label class="cap-var-input"><span>Agent response</span><textarea id="verifyResponse" rows="6">' + _esc(lastMsg) + '</textarea></label>' +
      '<label class="cap-var-input"><span>Verifier provider</span><select id="verifyProvider">' +
        (providers.providers || []).map(function(p){ return '<option value="' + _esc(p.id) + '">' + _esc(p.label || p.id) + '</option>'; }).join('') +
      '</select></label>' +
      '<div class="tools-actions"><button class="btn-primary" id="verifyRunBtn">Run verification</button></div>' +
      '<div id="verifyOutput" class="tools-output" style="display:none"></div></div>';
    root.innerHTML = html;
    root.querySelector('#verifyRunBtn').addEventListener('click', async function(){
      var req = document.getElementById('verifyRequest').value;
      var resp = document.getElementById('verifyResponse').value;
      var prov = document.getElementById('verifyProvider').value;
      if (!resp){ _toast('Agent response is required', 'error'); return; }
      var out = document.getElementById('verifyOutput');
      out.style.display = '';
      out.innerHTML = '<div class="tools-loading">Running verification…</div>';
      try {
        var r = await _post('/api/verify/run', {user_request: req, agent_response: resp, verifier_provider: prov});
        var v = r.verdict || r.result || {};
        out.innerHTML = '<div class="cap-vars-title">Verdict: <span class="badge ' + (v.overall === 'correct' ? 'badge-success' : 'badge-danger') + '">' + _esc(v.overall || 'UNKNOWN') + '</span></div>' +
          (v.summary ? '<p>' + _esc(v.summary) + '</p>' : '') +
          (v.issues && v.issues.length ? '<ul class="tools-list"><li>' + v.issues.map(_esc).join('</li><li>') + '</li></ul>' : '') +
          (v.suggestions && v.suggestions.length ? '<div class="cap-vars-title">Suggestions:</div><ul class="tools-list"><li>' + v.suggestions.map(_esc).join('</li><li>') + '</li></ul>' : '') +
          '<details><summary>Full response</summary><pre>' + _esc(JSON.stringify(r, null, 2)) + '</pre></details>';
      } catch(e){ out.innerHTML = '<div class="tools-error">' + _esc(e.message) + '</div>'; }
    });
  };


  // ── Learnings ──────────────────────────────────────────────────────
  RENDERERS.learnings = async function(root){
    var data = await _get('/api/learnings/list');
    var learnings = (data && data.learnings) || (data && data.data && data.data.learnings) || [];
    var dismissed = await _get('/api/learnings/dismissed').catch(function(){ return {dismissed: []}; });
    var html = '<div class="tools-actions">' +
      '<button class="btn-primary" id="learnScanBtn">Scan for new learnings</button>' +
    '</div>';
    if (!learnings.length){
      html += '<div class="tools-empty">No active learnings. Run a scan to detect rollbacks / CI failures.</div>';
    } else {
      html += '<table class="tools-table"><thead><tr><th>Name</th><th>Trigger</th><th>Source</th><th>Actions</th></tr></thead><tbody>';
      learnings.forEach(function(l){
        html += '<tr><td><strong>' + _esc(l.name || l.id) + '</strong></td>' +
          '<td>' + _esc(l.trigger_type || '—') + '</td>' +
          '<td><code>' + _esc(l.source || '') + '</code></td>' +
          '<td class="tools-row-actions">' +
            '<button class="btn-mini" data-action="show" data-name="' + _esc(l.name || l.id) + '">Show</button>' +
            '<button class="btn-mini btn-danger" data-action="dismiss" data-name="' + _esc(l.name || l.id) + '">Dismiss</button>' +
          '</td></tr>';
      });
      html += '</tbody></table>';
    }
    if (dismissed.dismissed && dismissed.dismissed.length){
      html += '<div class="tools-section"><div class="cap-vars-title">Dismissed (' + dismissed.dismissed.length + ')</div><ul class="tools-list">';
      dismissed.dismissed.forEach(function(d){
        html += '<li><code>' + _esc(d.name || d) + '</code> <button class="btn-mini" data-action="restore" data-name="' + _esc(d.name || d) + '">Restore</button></li>';
      });
      html += '</ul></div>';
    }
    root.innerHTML = html;
    root.querySelector('#learnScanBtn').addEventListener('click', async function(){
      try {
        await _post('/api/learnings/scan', {});
        _toast('Scan complete', 'success');
        render();
      } catch(e){ _toast(e.message, 'error'); }
    });
    root.querySelectorAll('[data-action="show"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try {
          var r = await _get('/api/learnings/show?name=' + encodeURIComponent(btn.dataset.name));
          body.innerHTML = '<div class="tools-actions"><button class="btn-secondary" id="learnBack">Back</button></div>' +
            '<h3>' + _esc(btn.dataset.name) + '</h3>' +
            '<div class="tools-output"><pre>' + _esc(r.content || r.markdown || JSON.stringify(r, null, 2)) + '</pre></div>';
          body.querySelector('#learnBack').addEventListener('click', render);
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="dismiss"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        await _post('/api/learnings/dismiss', {name: btn.dataset.name});
        _toast('Learning dismissed', 'success');
        render();
      });
    });
    root.querySelectorAll('[data-action="restore"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        await _post('/api/learnings/restore', {name: btn.dataset.name});
        _toast('Learning restored', 'success');
        render();
      });
    });
  };


  // ── GitHub Actions ─────────────────────────────────────────────────
  RENDERERS.github_actions = async function(root){
    root.innerHTML = '<div class="tools-section"><div class="cap-vars-title">Generate GitHub Action workflow</div>' +
      '<p class="tools-note">Generate a workflow YAML that runs Tera Pilot on PR / push / dispatch events.</p>' +
      '<label class="cap-var-input"><span>Trigger</span><select id="ghaTrigger">' +
        '<option value="pull_request">pull_request</option>' +
        '<option value="push">push</option>' +
        '<option value="workflow_dispatch">workflow_dispatch</option>' +
      '</select></label>' +
      '<div class="tools-actions"><button class="btn-primary" id="ghaGenBtn">Generate</button></div>' +
      '<div id="ghaOutput" class="tools-output" style="display:none"></div></div>';
    root.querySelector('#ghaGenBtn').addEventListener('click', async function(){
      try {
        var r = await _post('/api/github/generate_action', {trigger: document.getElementById('ghaTrigger').value});
        var out = root.querySelector('#ghaOutput');
        out.style.display = '';
        out.innerHTML = '<div class="cap-vars-title">Workflow YAML:</div><pre>' + _esc(r.yaml || r.content || JSON.stringify(r, null, 2)) + '</pre>' +
          '<button class="btn-secondary" id="ghaDownload">Download</button>';
        out.querySelector('#ghaDownload').addEventListener('click', function(){
          downloadText('tera-pilot-action.yml', r.yaml || r.content || '');
        });
      } catch(e){ _toast(e.message, 'error'); }
    });
  };


  // ── Notifications ──────────────────────────────────────────────────
  RENDERERS.notify = async function(root){
    var backends = await _get('/api/notify/backends').catch(function(){ return {backends: []}; });
    var status = await _get('/api/notify/status').catch(function(){ return {}; });
    var html = '<div class="tools-section"><div class="cap-vars-title">Notification backends</div>';
    if (!backends.backends || !backends.backends.length){
      html += '<div class="tools-empty">No backends configured.</div>';
    } else {
      html += '<table class="tools-table"><thead><tr><th>Name</th><th>Type</th><th>Status</th><th>Actions</th></tr></thead><tbody>';
      backends.backends.forEach(function(b){
        var st = (status.backends || {})[b.name] || {};
        html += '<tr><td><strong>' + _esc(b.name) + '</strong></td><td>' + _esc(b.type || b.kind || '') + '</td>' +
          '<td>' + (st.enabled ? '<span class="badge badge-success">enabled</span>' : '<span class="badge badge-muted">disabled</span>') + '</td>' +
          '<td class="tools-row-actions">' +
            '<button class="btn-mini" data-action="toggle" data-name="' + _esc(b.name) + '" data-enabled="' + (!st.enabled) + '">' + (st.enabled ? 'Disable' : 'Enable') + '</button>' +
            '<button class="btn-mini" data-action="test" data-name="' + _esc(b.name) + '">Test</button>' +
            '<button class="btn-mini btn-danger" data-action="remove" data-name="' + _esc(b.name) + '">Remove</button>' +
          '</td></tr>';
      });
      html += '</tbody></table>';
    }
    html += '<div class="tools-actions">' +
      '<button class="btn-primary" id="notifAddBtn">+ Add backend</button>' +
      '<button class="btn-secondary" id="notifTestAllBtn">Test all</button>' +
    '</div></div>';
    root.innerHTML = html;
    root.querySelector('#notifAddBtn').addEventListener('click', function(){
      var name = prompt('Backend name:', 'telegram');
      if (!name) return;
      var type = prompt('Type (telegram/discord/slack):', 'telegram');
      if (!type) return;
      var config = {};
      if (type === 'telegram'){
        config.token = prompt('Bot token:', '');
        config.chat_id = prompt('Chat ID:', '');
      } else if (type === 'discord'){
        config.webhook_url = prompt('Webhook URL:', '');
      } else if (type === 'slack'){
        config.webhook_url = prompt('Webhook URL:', '');
      }
      _post('/api/notify/configure', {name: name, config: {type: type, ...config}}).then(function(){ _toast('Backend added', 'success'); render(); }).catch(function(e){ _toast(e.message, 'error'); });
    });
    root.querySelector('#notifTestAllBtn').addEventListener('click', async function(){
      try { var r = await _post('/api/notify/test_all', {}); _toast('Test sent to all backends', 'success'); } catch(e){ _toast(e.message, 'error'); }
    });
    root.querySelectorAll('[data-action="toggle"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        await _post('/api/notify/toggle', {name: btn.dataset.name, enabled: btn.dataset.enabled === 'true'});
        _toast('Backend updated', 'success');
        render();
      });
    });
    root.querySelectorAll('[data-action="test"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        try { await _post('/api/notify/test', {name: btn.dataset.name}); _toast('Test notification sent', 'success'); } catch(e){ _toast(e.message, 'error'); }
      });
    });
    root.querySelectorAll('[data-action="remove"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        if (!confirm('Remove backend?')) return;
        await _post('/api/notify/remove', {name: btn.dataset.name});
        _toast('Backend removed', 'success');
        render();
      });
    });
  };


  // ── Daemon ─────────────────────────────────────────────────────────
  RENDERERS.daemon = async function(root){
    var status = await _get('/api/daemon/status').catch(function(){ return {}; });
    var html = '<div class="tools-section"><div class="cap-vars-title">Daemon status</div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">Running</span><span class="stat-pill-value">' + (status.running ? 'Yes' : 'No') + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Queued</span><span class="stat-pill-value">' + (status.queued || 0) + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Active</span><span class="stat-pill-value">' + (status.active || 0) + '</span></div>' +
      '</div></div>';
    html += '<div class="tools-section"><div class="cap-vars-title">Submit background task</div>' +
      '<textarea id="daemonPrompt" rows="4" placeholder="Prompt for the background agent…"></textarea>' +
      '<div class="tools-actions"><button class="btn-primary" id="daemonSubmitBtn">Submit task</button></div>' +
      '<div id="daemonOutput" class="tools-output" style="display:none"></div></div>';
    if (status.tasks && status.tasks.length){
      html += '<div class="tools-section"><div class="cap-vars-title">Recent tasks</div><table class="tools-table"><thead><tr><th>ID</th><th>Status</th><th>Created</th></tr></thead><tbody>';
      status.tasks.forEach(function(t){
        html += '<tr><td><code>' + _esc(t.id) + '</code></td><td>' + _esc(t.status) + '</td><td>' + _fmtTime(t.created_at) + '</td></tr>';
      });
      html += '</tbody></table></div>';
    }
    root.innerHTML = html;
    root.querySelector('#daemonSubmitBtn').addEventListener('click', async function(){
      var p = document.getElementById('daemonPrompt').value;
      if (!p){ _toast('Enter a prompt', 'error'); return; }
      try {
        var r = await _post('/api/daemon/submit', {prompt: p});
        var out = document.getElementById('daemonOutput');
        out.style.display = '';
        out.innerHTML = '<div class="cap-vars-title">Task submitted</div><pre>' + _esc(JSON.stringify(r, null, 2)) + '</pre>';
        _toast('Task submitted', 'success');
      } catch(e){ _toast(e.message, 'error'); }
    });
  };


  // ── MCP Server ─────────────────────────────────────────────────────
  RENDERERS.mcp_server = async function(root){
    var status = await _get('/api/mcp_server/status').catch(function(){ return {}; });
    var tools = await _get('/api/mcp_server/list_tools').catch(function(){ return {tools: []}; });
    var html = '<div class="tools-section"><div class="cap-vars-title">MCP server status</div>' +
      '<div class="tools-grid-row">' +
        '<div class="stat-pill"><span class="stat-pill-label">Mode</span><span class="stat-pill-value">' + _esc(status.mode || '—') + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Tools</span><span class="stat-pill-value">' + (tools.tools ? tools.tools.length : 0) + '</span></div>' +
        '<div class="stat-pill"><span class="stat-pill-label">Protocol</span><span class="stat-pill-value">' + _esc(status.protocol_version || '2024-11-05') + '</span></div>' +
      '</div>' +
      '<p class="tools-note">Run <code>tera-pilot-acp --mcp-server --workspace /path</code> to expose Tera Pilot\'s tools to other MCP-compatible agents.</p></div>';
    if (tools.tools && tools.tools.length){
      html += '<table class="tools-table"><thead><tr><th>Tool</th><th>Description</th><th>Write?</th></tr></thead><tbody>';
      tools.tools.forEach(function(t){
        html += '<tr><td><code>' + _esc(t.name) + '</code></td><td>' + _esc(t.description || '') + '</td>' +
          '<td>' + (t.write ? '<span class="badge badge-danger">write</span>' : '<span class="badge badge-success">read</span>') + '</td></tr>';
      });
      html += '</tbody></table>';
    }
    root.innerHTML = html;
  };


  // ── Persona ────────────────────────────────────────────────────────
  RENDERERS.persona = async function(root){
    var p = await _get('/api/persona/get').catch(function(){ return {}; });
    var content = p.content || p.persona || '';
    root.innerHTML = '<div class="tools-section"><div class="cap-vars-title">Persona / system prompt</div>' +
      '<p class="tools-note">Custom persona text is prepended to the system prompt on every agent turn.</p>' +
      '<textarea id="personaContent" rows="12" style="font-family:var(--font-mono, monospace);font-size:12px">' + _esc(content) + '</textarea>' +
      '<div class="tools-actions">' +
        '<button class="btn-secondary" id="personaResetBtn">Reset to default</button>' +
        '<button class="btn-primary" id="personaSaveBtn">Save</button>' +
      '</div></div>';
    root.querySelector('#personaSaveBtn').addEventListener('click', async function(){
      try {
        await _post('/api/persona/set', {content: document.getElementById('personaContent').value});
        _toast('Persona saved', 'success');
      } catch(e){ _toast(e.message, 'error'); }
    });
    root.querySelector('#personaResetBtn').addEventListener('click', async function(){
      if (!confirm('Reset persona to default?')) return;
      try {
        var r = await _post('/api/persona/reset', {});
        document.getElementById('personaContent').value = r.content || '';
        _toast('Persona reset', 'success');
      } catch(e){ _toast(e.message, 'error'); }
    });
  };


  // ── Providers (custom provider wizard) ─────────────────────────────
  RENDERERS.providers = async function(root){
    var data = await _get('/api/providers/custom/list');
    var providers = (data && data.providers) || [];
    var templates = await _get('/api/providers/templates').catch(function(){ return {templates: []}; });
    var html = '<div class="tools-section"><div class="cap-vars-title">Built-in templates</div>' +
      '<div class="template-grid">';
    (templates.templates || []).forEach(function(t){
      html += '<div class="template-card" data-template="' + _esc(t.id) + '">' +
        '<div class="template-card-title">' + _esc(t.name) + '</div>' +
        '<div class="template-card-desc">' + _esc(t.description || '') + '</div>' +
        (t.docs_url ? '<a href="' + _esc(t.docs_url) + '" target="_blank" rel="noopener" class="template-card-link">Docs →</a>' : '') +
        '<button class="btn-primary" data-action="use-template" data-template="' + _esc(t.id) + '">Use template</button>' +
      '</div>';
    });
    html += '</div></div>';
    html += '<div class="tools-section"><div class="cap-vars-title">Your custom providers (' + providers.length + ')</div>' +
      '<div class="tools-actions"><button class="btn-primary" id="cpAddBtn">+ Add provider</button></div>';
    if (!providers.length){
      html += '<div class="tools-empty">No custom providers yet. Add Nvidia NIM or any OpenAI-compatible endpoint.</div>';
    } else {
      html += '<table class="tools-table"><thead><tr><th>ID</th><th>Name</th><th>Base URL</th><th>Model</th><th>Key</th><th>Actions</th></tr></thead><tbody>';
      providers.forEach(function(p){
        html += '<tr>' +
          '<td><code>' + _esc(p.provider_id) + '</code></td>' +
          '<td>' + _esc(p.label) + '</td>' +
          '<td><code>' + _esc(p.api_base) + '</code></td>' +
          '<td>' + _esc(p.default_model) + '</td>' +
          '<td>' + (p.api_key_masked ? '<code>' + _esc(p.api_key_masked) + '</code>' : '<span class="badge badge-muted">env</span>') + '</td>' +
          '<td class="tools-row-actions">' +
            '<button class="btn-mini" data-action="edit" data-id="' + _esc(p.provider_id) + '">Edit</button>' +
            '<button class="btn-mini btn-danger" data-action="remove" data-id="' + _esc(p.provider_id) + '">Remove</button>' +
          '</td></tr>';
      });
      html += '</tbody></table>';
    }
    html += '</div>';
    root.innerHTML = html;
    root.querySelector('#cpAddBtn').addEventListener('click', function(){ openProviderWizard(null); });
    root.querySelectorAll('[data-action="use-template"]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var tpl = (templates.templates || []).find(function(t){ return t.id === btn.dataset.template; });
        openProviderWizard(tpl || null);
      });
    });
    root.querySelectorAll('[data-action="edit"]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var p = providers.find(function(x){ return x.provider_id === btn.dataset.id; });
        openProviderWizard(null, p);
      });
    });
    root.querySelectorAll('[data-action="remove"]').forEach(function(btn){
      btn.addEventListener('click', async function(){
        if (!confirm('Remove provider?')) return;
        try {
          await _post('/api/providers/custom/remove', {provider_id: btn.dataset.id});
          _toast('Provider removed', 'success');
          render();
        } catch(e){ _toast(e.message, 'error'); }
      });
    });
  };

  function openProviderWizard(template, existing){
    var modal = document.getElementById('providerWizardModal');
    var bd = document.getElementById('providerWizardBackdrop');
    var bodyEl = document.getElementById('providerWizardBody');
    document.getElementById('providerWizardTitle').textContent = existing ? 'Edit provider' : (template ? 'Add: ' + template.name : 'Add custom provider');
    var presets = {
      // v2.3.4: model examples refreshed to current (2026) models — the
      // previous defaults (llama-3.1, gpt-3.5-turbo) were years old.
      nvidia_nim: {provider_type: 'nvidia_nim', base_url: 'https://integrate.api.nvidia.com/v1', model: 'meta/llama-4-scout-17b-16e-instruct', env_var: 'NVIDIA_API_KEY', context_window: 131072},
      openai_compat: {provider_type: 'openai_compat', base_url: 'https://api.example.com/v1', model: 'gpt-5.5', env_var: '', context_window: 16384},
      ollama_local: {provider_type: 'openai_compat', base_url: 'http://127.0.0.1:11434', model: 'llama4', env_var: '', context_window: 32768},
      lmstudio_local: {provider_type: 'openai_compat', base_url: 'http://127.0.0.1:1234/v1', model: 'local-model', env_var: '', context_window: 32768},
    };
    var preset = template ? (presets[template.id] || {}) : {};
    var vals = existing || {};
    bodyEl.innerHTML = '' +
      '<label class="cap-var-input"><span>Provider ID (unique slug)</span><input type="text" id="pwId" value="' + _esc(vals.provider_id || '') + '" placeholder="my-nim"></label>' +
      '<label class="cap-var-input"><span>Display name</span><input type="text" id="pwName" value="' + _esc(vals.label || '') + '" placeholder="My Nvidia NIM"></label>' +
      '<label class="cap-var-input"><span>Provider type</span><select id="pwType">' +
        '<option value="openai_compat"' + (((vals.type || preset.provider_type) === 'openai_compat') ? ' selected' : '') + '>OpenAI-compatible</option>' +
        '<option value="nvidia_nim"' + (((vals.type || preset.provider_type) === 'nvidia_nim') ? ' selected' : '') + '>Nvidia NIM</option>' +
      '</select></label>' +
      '<label class="cap-var-input"><span>Base URL</span><input type="text" id="pwBaseUrl" value="' + _esc(vals.api_base || preset.base_url || '') + '"></label>' +
      '<label class="cap-var-input"><span>Default model</span><input type="text" id="pwModel" value="' + _esc(vals.default_model || preset.model || '') + '"></label>' +
      '<label class="cap-var-input"><span>API key</span><input type="password" id="pwApiKey" placeholder="' + (vals.api_key_masked ? '(unchanged)' : 'paste key here') + '"></label>' +
      '<label class="cap-var-input"><span>Env var name (alternative to API key)</span><input type="text" id="pwEnvVar" value="' + _esc(vals.env_var || preset.env_var || '') + '" placeholder="MY_PROVIDER_API_KEY"></label>' +
      '<label class="cap-var-input"><span>Context window (tokens)</span><input type="number" id="pwCtx" value="' + _esc(vals.context_window || preset.context_window || 16384) + '"></label>' +
      '<div class="tools-note">If both API key and env var are set, the API key wins. Env var is read at runtime — useful for not committing secrets.</div>';
    modal.classList.add('open');
    bd.classList.add('open');
    function close(){ modal.classList.remove('open'); bd.classList.remove('open'); }
    document.getElementById('providerWizardClose').onclick = close;
    document.getElementById('providerWizardCancel').onclick = close;
    bd.onclick = close;
    document.getElementById('providerWizardTest').onclick = async function(){
      var payload = collectPayload();
      try {
        _toast('Testing…', '');
        var r = await _post('/api/providers/custom/test', payload);
        _toast('OK: ' + ((r.response || '').slice(0, 60)), 'success');
      } catch(e){ _toast('Test failed: ' + e.message, 'error'); }
    };
    document.getElementById('providerWizardSave').onclick = async function(){
      var payload = collectPayload();
      if (!payload.provider_id){ _toast('Provider ID is required', 'error'); return; }
      try {
        var endpoint = existing ? '/api/providers/custom/update' : '/api/providers/custom/add';
        await _post(endpoint, payload);
        _toast('Provider saved', 'success');
        close();
        render();
      } catch(e){ _toast(e.message, 'error'); }
    };
    function collectPayload(){
      return {
        provider_id: document.getElementById('pwId').value.trim(),
        name: document.getElementById('pwName').value.trim(),
        provider_type: document.getElementById('pwType').value,
        base_url: document.getElementById('pwBaseUrl').value.trim(),
        model: document.getElementById('pwModel').value.trim(),
        api_key: document.getElementById('pwApiKey').value,
        env_var: document.getElementById('pwEnvVar').value.trim(),
        context_window: parseInt(document.getElementById('pwCtx').value || '16384'),
      };
    }
  }


  // ── Download helpers ───────────────────────────────────────────────
  function downloadJson(filename, data){
    var blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  }
  function downloadText(filename, text){
    var blob = new Blob([text], {type: 'text/plain'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    setTimeout(function(){ URL.revokeObjectURL(url); }, 1000);
  }


  // ── Sidebar wiring ─────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function(){
    document.querySelectorAll('.tools-nav').forEach(function(btn){
      btn.addEventListener('click', function(){
        openPanel(btn.dataset.tool);
      });
    });
    document.getElementById('toolsPanelClose').addEventListener('click', closePanel);
    document.getElementById('toolsPanelRefresh').addEventListener('click', render);
    document.getElementById('toolsBackdrop').addEventListener('click', closePanel);
    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape' && panel.classList.contains('open')) closePanel();
    });
  });

  // Expose for app.js integration
  window.__teraPilotTools = {
    open: openPanel,
    close: closePanel,
    refresh: render,
  };
  // v2.2.3: Expose renderers + meta so the Settings → Tools tab can render
  // any tool's content directly inside the Settings modal body, without
  // having to open the separate Tools drawer.
  window.__teraPilotToolsRenderers = RENDERERS;
  window.__teraPilotToolMeta = TOOL_META;

})();
