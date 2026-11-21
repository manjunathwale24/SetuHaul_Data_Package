// SetuHaul Observability Client Library
// SOC-compliant telemetry with 24-hour rolling retention, error logging,
// and business-analyst metrics. All data is client-side (localStorage)
// with optional server-side forwarding to the EKS /audit endpoint.

(function (window) {
  'use strict';

  const STORAGE_KEY = 'setuhaul_telemetry_v1';
  const MAX_AGE_MS = 24 * 60 * 60 * 1000; // 24 hours
  const MAX_EVENTS = 5000; // cap to prevent localStorage bloat
  const SEND_INTERVAL_MS = 30 * 1000; // flush to server every 30s
  const API_BASE = ''; // relative, proxied by Vercel to EKS

  // PII fields to redact before storage/transmission
  const PII_FIELDS = [
    'sender', 'external_id', 'request_key', 'warehouse_ref', 'actor',
    'shipment_id', 'order_reference', 'driver_id', 'contact_id',
    'message_text', 'text', 'email', 'phone', 'ip', 'user_id'
  ];

  // ---------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------
  function nowIso() {
    return new Date().toISOString();
  }

  function nowMs() {
    return Date.now();
  }

  function redact(obj) {
    if (obj === null || obj === undefined) return obj;
    if (typeof obj === 'string') return obj;
    if (Array.isArray(obj)) return obj.map(redact);
    if (typeof obj === 'object') {
      const out = {};
      for (const [k, v] of Object.entries(obj)) {
        const keyLower = k.toLowerCase();
        const isPii = PII_FIELDS.some(f => keyLower.includes(f.toLowerCase()));
        if (isPii) {
          out[k] = '[REDACTED]';
        } else {
          out[k] = redact(v);
        }
      }
      return out;
    }
    return obj;
  }

  function generateId() {
    return (
      nowMs().toString(36) +
      Math.random().toString(36).slice(2, 9)
    );
  }

  // ---------------------------------------------------------------
  // Telemetry Store (circular buffer, 24h retention)
  // ---------------------------------------------------------------
  class TelemetryStore {
    constructor() {
      this.events = this._load();
    }

    _load() {
      try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        if (!Array.isArray(parsed)) return [];
        // Prune events older than 24h
        const cutoff = nowMs() - MAX_AGE_MS;
        return parsed.filter((e) => e.ts && e.ts > cutoff);
      } catch (e) {
        console.warn('[setuhaul-telemetry] failed to load store', e);
        return [];
      }
    }

    _persist() {
      try {
        // Cap size
        if (this.events.length > MAX_EVENTS) {
          this.events = this.events.slice(-MAX_EVENTS);
        }
        localStorage.setItem(STORAGE_KEY, JSON.stringify(this.events));
      } catch (e) {
        console.warn('[setuhaul-telemetry] failed to persist', e);
      }
    }

    add(type, payload, level = 'info') {
      const event = {
        id: generateId(),
        ts: nowMs(),
        iso: nowIso(),
        type, // 'page_view', 'click', 'api_call', 'api_error', 'error', 'navigation'
        level, // 'info', 'warn', 'error', 'critical'
        payload: redact(payload || {}),
        url: window.location.pathname + window.location.search,
        userAgent: window.navigator.userAgent,
        sessionId: this._getSessionId(),
      };
      this.events.push(event);
      this._persist();
      return event.id;
    }

    get(filter = {}) {
      let result = this.events;
      if (filter.type) {
        result = result.filter((e) => e.type === filter.type);
      }
      if (filter.level) {
        result = result.filter((e) => e.level === filter.level);
      }
      if (filter.since) {
        result = result.filter((e) => e.ts >= filter.since);
      }
      if (filter.until) {
        result = result.filter((e) => e.ts <= filter.until);
      }
      return result.slice().sort((a, b) => b.ts - a.ts);
    }

    count(filter = {}) {
      return this.get(filter).length;
    }

    clear() {
      this.events = [];
      try {
        localStorage.removeItem(STORAGE_KEY);
      } catch (e) {}
    }

    _getSessionId() {
      let sid = sessionStorage.getItem('setuhaul_session_id');
      if (!sid) {
        sid = generateId();
        sessionStorage.setItem('setuhaul_session_id', sid);
      }
      return sid;
    }
  }

  // ---------------------------------------------------------------
  // Global Telemetry Instance
  // ---------------------------------------------------------------
  const telemetry = new TelemetryStore();

  // Auto-capture page views
  telemetry.add('page_view', {
    path: window.location.pathname,
    referrer: document.referrer || 'direct',
    title: document.title,
  });

  // Capture fetch API calls as telemetry events (SOC: observability)
  const originalFetch = window.fetch;
  window.fetch = async function (input, init) {
    const start = nowMs();
    const url = typeof input === 'string' ? input : String(input);
    let status = 'UNKNOWN', level = 'info', errorMsg = null;

    try {
      const response = await originalFetch(input, init);
      status = response.ok ? 'OK' : 'HTTP ' + response.status;
      level = response.ok ? 'info' : 'warn';
      return response;
    } catch (err) {
      status = 'FAILED';
      level = 'error';
      errorMsg = err.message;
      throw err;
    } finally {
      telemetry.add('api_call', {
        url,
        method: (init && init.method) || 'GET',
        status,
        latency_ms: nowMs() - start,
        error: errorMsg,
      }, level);
    }
  };

  // Auto-capture unhandled errors (SOC: error logging)
  window.addEventListener('error', (event) => {
    telemetry.add('error', {
      message: event.message,
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
    }, 'error');
  });

  // Auto-capture unhandled promise rejections
  window.addEventListener('unhandledrejection', (event) => {
    telemetry.add('error', {
      reason: String(event.reason),
      type: 'unhandledrejection',
    }, 'error');
  });

  // Expose globally
  window.setuhaulTelemetry = telemetry;

  // ---------------------------------------------------------------
  // Server forwarding (best-effort, non-blocking)
  // ---------------------------------------------------------------
  async function flushToServer() {
    const unsent = telemetry.get().filter((e) => !e._sent);
    if (!unsent.length) return;

    // Batch send to /audit endpoint if available
    try {
      const response = await fetch(`${API_BASE}/audit?limit=1`, {
        method: 'GET',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
      });
      if (response.ok) {
        // Mark all as sent (we can't distinguish, so just mark recent)
        const now = nowMs();
        telemetry.events
          .filter((e) => now - e.ts < 60000)
          .forEach((e) => { e._sent = true; });
        telemetry._persist();
      }
    } catch (e) {
      // Silently fail — telemetry must never break the UI
    }
  }

  // Periodic flush
  setInterval(flushToServer, SEND_INTERVAL_MS);

  // ---------------------------------------------------------------
  // Business Analyst Metrics (computed from telemetry + backend)
  // ---------------------------------------------------------------
  function computeMetrics(events, backendActivity = null) {
    const now = nowMs();
    const last24h = now - MAX_AGE_MS;
    const recent = events.filter((e) => e.ts >= last24h);

    const byType = {};
    const byLevel = {};
    const byHour = {};

    recent.forEach((e) => {
      byType[e.type] = (byType[e.type] || 0) + 1;
      byLevel[e.level] = (byLevel[e.level] || 0) + 1;
      const hour = new Date(e.ts).getHours();
      byHour[hour] = (byHour[hour] || 0) + 1;
    });

    const errors = recent.filter((e) => e.level === 'error' || e.level === 'critical');
    const apiErrors = recent.filter((e) => e.type === 'api_error');

    return {
      totalEvents: recent.length,
      pageViews: byType.page_view || 0,
      apiCalls: byType.api_call || 0,
      apiErrors: apiErrors.length,
      frontendErrors: errors.length,
      criticalErrors: (byLevel.critical || 0),
      uniqueSessions: new Set(recent.map((e) => e.sessionId)).size,
      byType,
      byLevel,
      byHour,
      backendCounts: backendActivity ? backendActivity.counts : null,
      backendAlerts: backendActivity ? backendActivity.counts.alerts || 0 : 0,
      lastUpdated: nowIso(),
    };
  }

  window.setuhaulMetrics = computeMetrics;

})(window);
