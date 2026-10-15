/* ============================================================================
 * form-autosave.js — browser-local draft persistence for data-entry forms.
 *
 * Goal: if the user is halfway through filling a form and the tab crashes,
 * the network drops, or the page is reloaded, reopening that same form offers
 * to restore everything they had typed.
 *
 * How it works:
 *   - Runs on every page (included globally in base.html).
 *   - Auto-discovers eligible full-page POST forms — no per-form wiring needed.
 *   - As the user types (debounced), snapshots the whole form into localStorage,
 *     namespaced per user + per page + per form.
 *   - On the next load, if a fresh draft exists, shows a "Restore / Close"
 *     banner fixed just under the top header (never silently overwrites).
 *   - Clears the draft when the form is submitted.
 *   - Drafts older than DRAFT_TTL_MS are treated as stale and pruned.
 *
 * Deliberately NOT saved (privacy / correctness):
 *   - password fields, csrf_token, file inputs (browsers can't restore these),
 *     hidden inputs (server-managed context), and any [data-no-autosave] field.
 *   - Whole forms that: are GET, live inside a .modal, contain a password field,
 *     carry [data-no-autosave], or have no user-editable fields (e.g. one-click
 *     approve/reject/delete actions and filter bars).
 *
 * Storage is client-side only — which is exactly why it survives a *server*
 * crash mid-save, unlike a server-side session draft would.
 * ========================================================================== */
(function () {
  'use strict';

  var KEY_PREFIX = 'oxypc:autosave:v1';
  var DRAFT_TTL_MS = 24 * 60 * 60 * 1000; // 24h — older drafts are discarded
  var SAVE_DEBOUNCE_MS = 400;

  var username = (document.body && document.body.getAttribute('data-username')) || 'anon';

  // ---- localStorage guards ----------------------------------------------------
  function lsAvailable() {
    try {
      var t = '__oxypc_test__';
      window.localStorage.setItem(t, '1');
      window.localStorage.removeItem(t);
      return true;
    } catch (e) {
      return false; // private mode / disabled storage
    }
  }
  if (!lsAvailable()) return;

  // ---- field / form eligibility ----------------------------------------------
  var TEXTLIKE = {
    text: 1, number: 1, email: 1, tel: 1, url: 1, search: 1, date: 1,
    'datetime-local': 1, month: 1, week: 1, time: 1, color: 1, range: 1
  };

  function isSavableField(el) {
    if (!el.name && !el.id) return false;
    if (el.disabled) return false;
    if (el.hasAttribute('data-no-autosave')) return false;
    if (el.name === 'csrf_token') return false;
    var tag = el.tagName;
    if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
    if (tag === 'INPUT') {
      var type = (el.type || 'text').toLowerCase();
      if (type === 'password' || type === 'file' || type === 'hidden' ||
          type === 'submit' || type === 'button' || type === 'reset' ||
          type === 'image') return false;
      if (type === 'checkbox' || type === 'radio') return true;
      return !!TEXTLIKE[type];
    }
    return false;
  }

  function savableFields(form) {
    var out = [];
    var els = form.elements;
    for (var i = 0; i < els.length; i++) {
      if (isSavableField(els[i])) out.push(els[i]);
    }
    return out;
  }

  function isEligibleForm(form) {
    var method = (form.getAttribute('method') || 'get').toLowerCase();
    if (method !== 'post') return false;               // skip GET filter bars
    if (form.hasAttribute('data-no-autosave')) return false;
    if (form.closest('.modal')) return false;          // scope: full-page forms
    // Skip any form containing a password field (login / change-password /
    // user-create) — never persist alongside credentials.
    if (form.querySelector('input[type="password"]')) return false;
    // Skip forms with nothing worth saving (one-click actions, pure uploads).
    if (savableFields(form).length === 0) return false;
    return true;
  }

  // ---- serialize / apply a single field --------------------------------------
  function serialize(el) {
    var type = (el.type || '').toLowerCase();
    if (type === 'checkbox') return { t: 'cb', c: el.checked };
    if (type === 'radio')    return { t: 'rd', v: el.value, c: el.checked };
    if (el.tagName === 'SELECT' && el.multiple) {
      var vals = [];
      for (var i = 0; i < el.options.length; i++) {
        if (el.options[i].selected) vals.push(el.options[i].value);
      }
      return { t: 'ms', v: vals };
    }
    return { t: 'v', v: el.value };
  }

  function fireChange(el) {
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function apply(el, rec) {
    if (!rec) return;
    if (rec.t === 'cb') { el.checked = !!rec.c; fireChange(el); return; }
    if (rec.t === 'rd') { if (rec.c) { el.checked = true; fireChange(el); } return; }
    if (rec.t === 'ms') {
      var set = {}; (rec.v || []).forEach(function (v) { set[v] = 1; });
      for (var i = 0; i < el.options.length; i++) {
        el.options[i].selected = !!set[el.options[i].value];
      }
      fireChange(el); return;
    }
    // scalar. If this <select> is upgraded by Tom Select, drive the instance so
    // the visible widget updates (Tom Select stores itself on el.tomselect).
    if (el.tomselect) { el.tomselect.setValue(rec.v, true); return; }
    el.value = rec.v;
    fireChange(el);
  }

  // ---- snapshot / restore a whole form ---------------------------------------
  function snapshot(form) {
    var byName = {};
    savableFields(form).forEach(function (el) {
      var name = el.name || el.id;
      (byName[name] = byName[name] || []).push(serialize(el));
    });
    return byName;
  }

  function restore(form, byName) {
    var counters = {};
    savableFields(form).forEach(function (el) {
      var name = el.name || el.id;
      var arr = byName[name];
      if (!arr) return;
      var idx = counters[name] = (counters[name] === undefined ? 0 : counters[name] + 1);
      apply(el, arr[idx]);
    });
  }

  // ---- storage keys -----------------------------------------------------------
  function formKey(form, index) {
    var id = form.id || form.getAttribute('name') || ('f' + index);
    return [KEY_PREFIX, username, location.pathname, id].join('::');
  }

  function readDraft(key) {
    try {
      var raw = window.localStorage.getItem(key);
      if (!raw) return null;
      var obj = JSON.parse(raw);
      if (!obj || !obj.t || (Date.now() - obj.t) > DRAFT_TTL_MS) {
        window.localStorage.removeItem(key);
        return null;
      }
      return obj;
    } catch (e) {
      try { window.localStorage.removeItem(key); } catch (e2) {}
      return null;
    }
  }

  function writeDraft(key, data) {
    var payload = JSON.stringify({ t: Date.now(), data: data });
    try {
      window.localStorage.setItem(key, payload);
    } catch (e) {
      // Quota — prune our expired/oldest drafts and retry once.
      pruneStale(true);
      try { window.localStorage.setItem(key, payload); } catch (e2) {}
    }
  }

  function removeDraft(key) {
    try { window.localStorage.removeItem(key); } catch (e) {}
  }

  // Prune expired drafts (and, if aggressive, the single oldest) to reclaim space.
  function pruneStale(aggressive) {
    var oldestKey = null, oldestT = Infinity, now = Date.now();
    for (var i = window.localStorage.length - 1; i >= 0; i--) {
      var k = window.localStorage.key(i);
      if (!k || k.indexOf(KEY_PREFIX) !== 0) continue;
      try {
        var obj = JSON.parse(window.localStorage.getItem(k));
        if (!obj || !obj.t || (now - obj.t) > DRAFT_TTL_MS) {
          window.localStorage.removeItem(k);
        } else if (obj.t < oldestT) {
          oldestT = obj.t; oldestKey = k;
        }
      } catch (e) {
        window.localStorage.removeItem(k);
      }
    }
    if (aggressive && oldestKey) window.localStorage.removeItem(oldestKey);
  }

  // ---- restore banner ---------------------------------------------------------
  function relativeTime(ts) {
    var s = Math.round((Date.now() - ts) / 1000);
    if (s < 60) return 'just now';
    var m = Math.round(s / 60);
    if (m < 60) return m + (m === 1 ? ' minute' : ' minutes') + ' ago';
    var h = Math.round(m / 60);
    return h + (h === 1 ? ' hour' : ' hours') + ' ago';
  }

  function showBanner(form, key, draft) {
    if (form.querySelector('.autosave-restore-banner')) return;
    var bar = document.createElement('div');
    // Fixed just under the top header, sized to its content (app.css
    // .autosave-restore-banner). A second form's banner on the same page
    // stacks below the first instead of covering it.
    bar.className = 'alert alert-warning d-flex align-items-center flex-wrap gap-2 py-2 mb-0 autosave-restore-banner';
    var stacked = document.querySelectorAll('.autosave-restore-banner').length;
    if (stacked) bar.style.marginTop = (stacked * 56) + 'px';
    bar.setAttribute('role', 'alert');
    bar.innerHTML =
      '<i class="bi bi-clock-history"></i>' +
      '<span class="me-auto small">You have unsaved data from <strong>' +
        relativeTime(draft.t) + '</strong>. Restore it?</span>' +
      '<button type="button" class="btn btn-sm btn-success autosave-restore-btn">' +
        '<i class="bi bi-arrow-counterclockwise me-1"></i>Restore Last Values</button>' +
      '<button type="button" class="btn btn-sm btn-outline-secondary autosave-discard-btn">Close</button>';
    form.insertBefore(bar, form.firstChild);

    bar.querySelector('.autosave-restore-btn').addEventListener('click', function () {
      restore(form, draft.data);
      bar.remove();
      flash('Draft restored');
    });
    bar.querySelector('.autosave-discard-btn').addEventListener('click', function () {
      removeDraft(key);
      bar.remove();
    });
  }

  // ---- tiny "Draft saved" pill ------------------------------------------------
  var pill = null, pillTimer = null;
  function flash(text) {
    if (!pill) {
      pill = document.createElement('div');
      pill.className = 'autosave-pill';
      pill.style.cssText = 'position:fixed;bottom:16px;right:16px;z-index:1080;' +
        'background:#1f3864;color:#fff;padding:6px 12px;border-radius:16px;' +
        'font-size:0.78rem;box-shadow:0 2px 8px rgba(0,0,0,0.25);opacity:0;' +
        'transition:opacity .2s;pointer-events:none;';
      document.body.appendChild(pill);
    }
    pill.textContent = text;
    pill.style.opacity = '1';
    clearTimeout(pillTimer);
    pillTimer = setTimeout(function () { pill.style.opacity = '0'; }, 1200);
  }

  // ---- wire up one form -------------------------------------------------------
  function attach(form, index) {
    var key = formKey(form, index);

    // Offer restore if a fresh draft exists.
    var draft = readDraft(key);
    if (draft && draft.data) showBanner(form, key, draft);

    // Save (debounced) on genuine user edits only. Programmatic autofill
    // (e.g. the IQC diagnose agent) dispatches untrusted events, so it won't
    // start a draft on its own — but once the user touches the form, the whole
    // form (including any auto-filled values) gets captured.
    var timer = null;
    function scheduleSave() {
      clearTimeout(timer);
      timer = setTimeout(function () {
        writeDraft(key, snapshot(form));
        flash('Draft saved');
      }, SAVE_DEBOUNCE_MS);
    }
    form.addEventListener('input', function (e) {
      if (e.isTrusted && isSavableField(e.target)) scheduleSave();
    });
    form.addEventListener('change', function (e) {
      if (e.isTrusted && isSavableField(e.target)) scheduleSave();
    });

    // Clear the draft once the form is actually submitted.
    form.addEventListener('submit', function () { removeDraft(key); });
  }

  // ---- boot -------------------------------------------------------------------
  function init() {
    pruneStale(false);
    var forms = document.querySelectorAll('form');
    var idx = 0;
    for (var i = 0; i < forms.length; i++) {
      if (isEligibleForm(forms[i])) attach(forms[i], idx++);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
