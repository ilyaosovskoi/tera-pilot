/* ===================================================================
   bridge_shim.js — v2.2.2
   ===================================================================
   Provides `window.bridge` for app.js when the legacy QWebChannel
   bridge is not present (i.e. the page is loaded in a plain browser
   instead of a Qt/PyWebView window).

   app.js was written against the old Qt bridge API where every
   signal was an object with `.connect(cb)` / `.disconnect(cb)` and
   every method either returned synchronously or called a callback
   with the result. Without this shim, `window.bridge` is undefined
   and the page crashes at line 3536:

       window.bridge.guardian_review_requested.connect(...)

   The shim does two things:

   1. Exposes every signal app.js references as a stub signal object.
      `.connect()` records the callback; `.disconnect()` clears it.
      The backend can later fire a signal by calling
      `bridge.__fire('signal_name', ...args)`.

   2. Tries to connect to the local API server. If /api/status
      responds, marks the bridge as connected (`__teraPilotBridgeConnected
      = true`), calls `window.__teraPilotReady(status)`, and dispatches
      the `tera_pilot:bridge_ready` event so app.js wires its signals.

   3. Provides a generic method dispatcher `bridge.__call(method,
      args)` that maps snake_case method names to HTTP endpoints.
      app.js's `callBridge()` calls `window.bridge[method](...args,
      callback)` — the shim's Proxy turns that into a fetch().

   The actual HTTP endpoint mapping lives in `bridge_routes.js`
   (optional). If that file is absent, methods resolve a structured
   `{ok: false, error: ...}` result — the UI runs in "demo mode"
   without a backend and callers never receive bare `undefined`.
   =================================================================== */

(function () {
  'use strict';

  if (window.bridge && window.__teraPilotBridgeShimInstalled) return;

  // ── Signal stub ───────────────────────────────────────────────────
  // A Qt-style signal: .connect(cb) registers, .disconnect(cb) removes.
  // __fire(...args) invokes every registered cb with those args.
  function Signal(name) {
    this._name = name;
    this._cbs = [];
  }
  Signal.prototype.connect = function (cb) {
    if (typeof cb === 'function' && this._cbs.indexOf(cb) === -1) {
      this._cbs.push(cb);
    }
    return this;
  };
  Signal.prototype.disconnect = function (cb) {
    var i = this._cbs.indexOf(cb);
    if (i !== -1) this._cbs.splice(i, 1);
    return this;
  };
  Signal.prototype.__fire = function () {
    var args = Array.prototype.slice.call(arguments);
    for (var i = 0; i < this._cbs.length; i++) {
      try { this._cbs[i].apply(null, args); }
      catch (e) { console.warn('[bridge_shim] signal ' + this._name + ' handler threw:', e); }
    }
  };

  // Every signal name app.js touches (see `grep window.bridge.*.connect`).
  var SIGNAL_NAMES = [
    'token_streamed', 'token_stats_updated',
    'agent_step', 'agent_done', 'agent_error',
    'agent_step_signal', 'agent_tool_result', 'agent_final',
    'file_changed', 'provider_changed',
    'chat_list_changed', 'chat_saved', 'settings_saved',
    'oneshot_done', 'oneshot_error',
    'git_status_changed', 'title_generated', 'apply_result',
    'router_decision', 'diff_review_requested',
    'action_confirm_requested', 'guardian_review_requested',
    'update_check_result'
  ];

  var bridge = {};
  for (var i = 0; i < SIGNAL_NAMES.length; i++) {
    bridge[SIGNAL_NAMES[i]] = new Signal(SIGNAL_NAMES[i]);
  }

  // ── Bridge method dispatcher ─────────────────────────────────────
  // app.js does one of two things:
  //   (a) VOID: window.bridge.method(...args)            // returns nothing
  //   (b) ASYNC: window.bridge.method(...args, callback) // callback(result)
  //
  // The shim wraps both into a Promise-returning `__call`. For (b) the
  // last argument is the callback; we invoke it when the Promise
  // settles.
  function detectRoute(method) {
    // Map of method → HTTP route. Extend as needed.
    var GET_ROUTES = {
      'get_status':            '/api/status',
      'list_providers':        '/api/providers',
      'list_chats':            '/api/chat/list',
      'list_templates':        '/api/templates',
      'list_skills':           '/api/skills',
      'list_snippets':         '/api/snippets',
      'list_slash_commands':   '/api/slash_commands/list',
      'get_token_stats':       '/api/quota/stats',
      'get_provider_breakdown':'/api/quota/breakdown',
      'get_pricing_table':     '/api/pricing/table',
      'get_quota_stats':       '/api/quota/stats',
      'get_context_status':    '/api/context/status',
      'get_agent_autonomy':    '/api/agent/advanced_settings',
      'get_guardian_level':    '/api/agent/advanced_settings',
      'get_settings':          '/api/settings',
      'get_advanced_agent_settings': '/api/agent/advanced_settings',
      'get_persistence_backend': '/api/persistence/backend',
      'get_queue_stats':       '/api/queue/stats',
      'check_for_updates':     '/api/updates/check',
      'read_claude_md':        '/api/claude_md/read',
      'mcp_list_servers':      '/api/mcp/servers',
      'load_chat':             '/api/chat/load',
      'pin_context_file':      '/api/context/pin',
      'unpin_context_file':    '/api/context/unpin'
    };
    var POST_ROUTES = {
      'set_provider':          '/api/providers/activate',
      'toggle_auto_router':    '/api/router/toggle',
      'send_agent_message':    '/api/agent/stream',
      'send_message':          '/api/chat/stream',
      'stop_agent':            '/api/agent/stop',
      'stop_generation':       '/api/chat/stop',
      'clear_context':         '/api/context/clear',
      'compact_context':       '/api/context/compact',
      'reload_project_context':'/api/context/reload',
      'undo_last_agent':       '/api/agent/undo',
      'delete_chat':           '/api/chat/delete',
      'rename_chat':           '/api/chat/rename',
      'create_chat':           '/api/chat/create',
      'rag_search':            '/api/rag/search',
      'save_settings':         '/api/settings/save',
      'set_budget':            '/api/budget/set',
      'fetch_live_pricing':    '/api/pricing/fetch',
      'set_persistence_backend':'/api/persistence/backend',
      'token_optimization_tips':'/api/token_optimization/tips',
      'set_agent_autonomy':    '/api/agent/autonomy',
      'set_guardian_level':    '/api/agent/guardian',
      'set_diff_review':       '/api/agent/diff_review',
      'health_check':          '/api/providers/health',
      'save_snippet':          '/api/snippets/save',
      'delete_snippet':        '/api/snippets/delete',
      'classify_prompt':       '/api/router/classify',
      'respond_diff_review':   '/api/diff/respond',
      'respond_action_confirm':'/api/action/respond',
      'respond_guardian_review':'/api/guardian/respond',
      'write_file':            '/api/files/write',
      'read_file':             '/api/files/read',
      'list_files':            '/api/files/list',
      'generate_title':        '/api/chat/generate_title',
      'save_memory':           '/api/memory/save',
      'write_claude_md':       '/api/claude_md/write',
      'append_claude_lesson':  '/api/claude_md/append_lesson',
      'mcp_reload_config':     '/api/mcp/reload',
      'mcp_add_server':        '/api/mcp/add',
      'mcp_remove_server':     '/api/mcp/remove',
      'mcp_toggle_server':     '/api/mcp/toggle',
      'mcp_start_server':      '/api/mcp/start',
      'mcp_stop_server':       '/api/mcp/stop',
      'save_advanced_agent_settings': '/api/agent/advanced_settings/save',
      'clear_quota_history':   '/api/quota/clear',
      'open_external_url':     '/api/external/open',
      'native_file_picker':      '/api/native_file_picker',
      'swarm_spawn':           '/api/swarm/spawn',
      'swarm_list':            '/api/swarm/list',
      'swarm_remove':          '/api/swarm/remove',
      'swarm_cleanup':         '/api/swarm/cleanup',
      'run_collaboration':     '/api/collaboration/run',
      'enhance_prompt':        '/api/oneshot/enhance',
      'launch_tui':            '/api/launch_tui',
      'open_project':          '/api/open_project'
    };
    if (GET_ROUTES[method]) return { verb: 'GET', url: GET_ROUTES[method] };
    if (POST_ROUTES[method]) return { verb: 'POST', url: POST_ROUTES[method] };
    return null;
  }

  function authHeaders() {
    var h = { 'Content-Type': 'application/json' };
    if (window.__apiToken) h['Authorization'] = 'Bearer ' + window.__apiToken;
    return h;
  }

  function apiBase() {
    if (window.__apiBase) return window.__apiBase.replace(/\/$/, '');
    return '';  // same-origin
  }

  function doFetch(route, args, method) {
    var url = apiBase() + route.url;
    var opts = { method: route.verb, headers: authHeaders() };
    // Normalize args: callBridge often passes positional args as an Array
    // (e.g. load_chat(chatId) → ['abc123']). Convert known single-arg
    // GET methods into the query-param object the backend expects.
    var params = args;
    if (Array.isArray(args)) {
      if (route.verb === 'GET') {
        var GET_ARG_MAP = {
          'load_chat': 'id',
          'pin_context_file': 'path',
          'unpin_context_file': 'path'
        };
        var key = GET_ARG_MAP[method];
        if (key && args.length >= 1) {
          params = {};
          params[key] = args[0];
          // pass through extra object arg if present
          if (args.length >= 2 && args[1] && typeof args[1] === 'object' && !Array.isArray(args[1])) {
            for (var ek in args[1]) params[ek] = args[1][ek];
          }
        } else if (args.length === 1 && args[0] && typeof args[0] === 'object' && !Array.isArray(args[0])) {
          params = args[0];
        } else {
          params = {};
        }
      } else {
        // POST: prefer a single object body; otherwise wrap positional args
        if (args.length === 1 && args[0] && typeof args[0] === 'object' && !Array.isArray(args[0])) {
          params = args[0];
        } else if (args.length === 0) {
          params = {};
        } else {
          // Common patterns: delete_chat(id), rename_chat(id, title), etc.
          var POST_ARG_MAP = {
            'delete_chat': ['id'],
            'rename_chat': ['id', 'title'],
            'create_chat': ['title'],
            'set_provider': ['provider_id'],
            'open_project': ['path']
          };
          var keys = POST_ARG_MAP[method];
          if (keys) {
            params = {};
            for (var i = 0; i < keys.length && i < args.length; i++) {
              params[keys[i]] = args[i];
            }
          } else {
            params = { args: args };
          }
        }
      }
    }
    if (route.verb === 'POST') {
      opts.body = JSON.stringify(params || {});
    } else if (params && typeof params === 'object' && Object.keys(params).length) {
      // For GET, append query string for simple key=value pairs.
      var qs = [];
      for (var k in params) {
        if (params[k] !== undefined && params[k] !== null) {
          qs.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        }
      }
      if (qs.length) url += (url.indexOf('?') === -1 ? '?' : '&') + qs.join('&');
    }
    return fetch(url, opts).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var ct = r.headers.get('content-type') || '';
      if (ct.indexOf('application/json') !== -1) return r.json();
      if (ct.indexOf('text/') !== -1) return r.text();
      return r.json().catch(function () { return { ok: true }; });
    });
  }

  bridge.__call = function (method, args) {
    var route = detectRoute(method);
    if (!route) {
      // Unknown method → no-op (demo mode). Resolves a safe failure object
      // so callers that do `result.ok` don't crash on undefined.
      console.debug('[bridge_shim] no route for method:', method);
      return Promise.resolve({ ok: false, error: 'No route for method: ' + method });
    }
    return doFetch(route, args, method).catch(function (e) {
      console.warn('[bridge_shim] ' + method + ' failed:', e.message);
      // Prefer a structured error over rejecting, so callBridge resolve
      // path still gets an object with .ok — avoids "undefined is not an object".
      return { ok: false, error: e && e.message ? e.message : String(e) };
    });
  };

  // ── Proxy: bridge.some_method(...args, callback?) ────────────────
  // A Proxy turns any property access into a function that:
  //   1. Collects args
  //   2. If last arg is a function, treats it as a Node-style callback
  //   3. Calls __call(method, args) and resolves/rejects the callback
  var methodProxy = new Proxy({}, {
    get: function (_target, prop) {
      if (prop in bridge) return bridge[prop];          // signal or __call
      if (typeof prop !== 'string') return undefined;
      return function () {
        var args = Array.prototype.slice.call(arguments);
        var cb = null;
        if (args.length && typeof args[args.length - 1] === 'function') {
          cb = args.pop();
        }
        var p = bridge.__call(prop, args);
        if (cb) {
          p.then(function (r) { try { cb(r != null ? r : { ok: false, error: 'empty result' }); } catch (e) { console.warn(e); } },
                 function (e) { try { cb({ ok: false, error: e && e.message ? e.message : String(e) }); } catch (_) {} });
        }
        return undefined;  // callers use the callback, not the return value
      };
    }
  });

  // Merge signal stubs into the proxy target via a wrapper.
  // We expose `window.bridge` as the proxy; signal access goes through
  // the get-trap above (which checks `prop in bridge` first).
  window.bridge = methodProxy;
  // Stash signal stubs so __fire works after wiring.
  window.bridge.__signals = bridge;
  window.bridge.__fire = function (name) {
    var sig = bridge[name];
    if (sig && sig.__fire) {
      var rest = Array.prototype.slice.call(arguments, 1);
      sig.__fire.apply(sig, rest);
    }
  };

  // VOID_METHODS in app.js — methods that should resolve immediately
  // without waiting for a callback. The shim already handles this via
  // the proxy (returns undefined, fires callback async).
  // No extra work needed.

  // ── Connect to backend ────────────────────────────────────────────
  // Try to fetch /api/status. If it succeeds, mark the bridge as
  // connected, call __teraPilotReady, and dispatch the tera_pilot:bridge_ready
  // event so app.js wires its signal handlers.
  function tryConnect() {
    // file:// URLs can't use fetch() — skip the auto-connect attempt
    // and run in demo mode. The user will see the UI but no backend
    // data. When served via the tera_pilot web server (http://127.0.0.1:...),
    // fetch works normally.
    if (location.protocol === 'file:') {
      console.info('[bridge_shim] file:// protocol — running in demo mode (no backend)');
      return;
    }
    fetch(apiBase() + '/api/status', { headers: authHeaders() })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (status) {
        if (!status) {
          console.info('[bridge_shim] /api/status unavailable — running in demo mode');
          return;
        }
        window.__teraPilotBridgeConnected = true;
        if (typeof window.__teraPilotReady === 'function') {
          try { window.__teraPilotReady(status); } catch (e) { console.warn(e); }
        }
        try {
          window.dispatchEvent(new CustomEvent('tera_pilot:bridge_ready'));
        } catch (e) {
          // CustomEvent may not be available in very old engines; fall back.
          var ev = document.createEvent('CustomEvent');
          ev.initCustomEvent('tera_pilot:bridge_ready', false, false, null);
          window.dispatchEvent(ev);
        }
        console.info('[bridge_shim] connected to backend at', apiBase() || '(same origin)');
      })
      .catch(function () {
        console.info('[bridge_shim] backend not reachable — running in demo mode');
      });
  }

  // Defer connection until DOM is ready so app.js's __teraPilotReady hook
  // is installed before we call it.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', tryConnect, { once: true });
  } else {
    setTimeout(tryConnect, 0);
  }

  // Expose a fire() helper so the backend (or test harness) can simulate
  // signals arriving from the server. e.g. for SSE-driven events.
  window.__teraPilotBridgeFire = window.bridge.__fire;

  window.__teraPilotBridgeShimInstalled = true;
  console.info('[bridge_shim] installed — signals stubbed, method dispatcher ready');
})();
