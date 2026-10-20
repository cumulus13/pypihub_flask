/* PyPihub UI behaviour: theme menu, copy buttons, package filter and sort. No dependencies. */
(function () {
  'use strict';

  var doc = document;
  var live = doc.getElementById('live-status');

  function announce(message) {
    if (!live) { return; }
    live.textContent = '';
    window.setTimeout(function () { live.textContent = message; }, 30);
  }

  /* ---- Theme menu ------------------------------------------------------ */
  var themeButtons = doc.querySelectorAll('[data-theme-choice]');
  function markTheme() {
    var pref = window.PyPihubTheme ? window.PyPihubTheme.get() : 'auto';
    Array.prototype.forEach.call(themeButtons, function (button) {
      var on = button.getAttribute('data-theme-choice') === pref;
      button.classList.toggle('active', on);
      if (on) { button.setAttribute('aria-current', 'true'); } else { button.removeAttribute('aria-current'); }
    });
  }
  Array.prototype.forEach.call(themeButtons, function (button) {
    button.addEventListener('click', function () {
      if (window.PyPihubTheme) { window.PyPihubTheme.set(button.getAttribute('data-theme-choice')); }
      markTheme();
    });
  });
  markTheme();

  /* ---- Copy buttons ---------------------------------------------------- */
  // navigator.clipboard only exists on https or localhost. A server on a LAN address
  // is plain http, so fall back to a hidden textarea and execCommand.
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) { return navigator.clipboard.writeText(text); }
    return new Promise(function (resolve, reject) {
      var previous = doc.activeElement;
      var area = doc.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.className = 'visually-hidden';
      doc.body.appendChild(area);
      area.focus();
      area.select();
      area.setSelectionRange(0, text.length);
      var ok = false;
      try { ok = doc.execCommand('copy'); } catch (err) { ok = false; }
      doc.body.removeChild(area);
      if (previous && previous.focus) { previous.focus(); }
      if (ok) { resolve(); } else { reject(new Error('copy failed')); }
    });
  }

  function selectContents(node) {
    var range = doc.createRange();
    range.selectNodeContents(node);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  doc.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy-target]');
    if (!button) { return; }
    var target = doc.getElementById(button.getAttribute('data-copy-target'));
    if (!target) { return; }
    var label = button.querySelector('.copy__label');
    var text = target.textContent.trim();
    window.clearTimeout(button._reset);
    copyText(text).then(function () {
      button.setAttribute('data-state', 'copied');
      if (label) { label.textContent = 'Copied'; }
      announce('Copied to clipboard');
    }, function () {
      selectContents(target);
      button.setAttribute('data-state', 'failed');
      if (label) { label.textContent = 'Press Ctrl+C'; }
      announce('Could not copy automatically. The text is selected: press Ctrl+C.');
    }).then(function () {
      button._reset = window.setTimeout(function () {
        button.removeAttribute('data-state');
        if (label) { label.textContent = 'Copy'; }
      }, 2200);
    });
  });

  /* ---- Package filter and sort ----------------------------------------- */
  var input = doc.getElementById('pkg-search');
  var table = doc.querySelector('[data-pkg-table]');
  if (!input) { return; }

  var body = table ? table.tBodies[0] : null;
  var rows = body ? Array.prototype.slice.call(body.rows) : [];
  var summary = doc.querySelector('[data-summary]');
  var nomatch = doc.querySelector('[data-nomatch]');
  var cmdPkg = doc.querySelector('[data-cmd-pkg]');
  var cmdNote = doc.querySelector('[data-cmd-note]');
  var sourceButtons = doc.querySelectorAll('[data-source-filter]');
  var sortButtons = doc.querySelectorAll('[data-sort]');
  var PLACEHOLDER = 'package-name';
  var VALID_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
  var state = { query: '', source: 'all', sortKey: 'name', sortDir: 1 };

  function normalize(value) { return value.toLowerCase().replace(/[-_.]+/g, '-'); }

  function visibleRows() { return rows.filter(function (row) { return !row.hidden; }); }

  function commandPackage(visible) {
    var raw = state.query.trim();
    if (!raw) { return PLACEHOLDER; }
    var key = normalize(raw);
    for (var i = 0; i < visible.length; i++) {
      if (visible[i].getAttribute('data-key') === key) { return visible[i].getAttribute('data-name'); }
    }
    if (visible.length === 1) { return visible[0].getAttribute('data-name'); }
    if (visible.length === 0 && state.source === 'all' && VALID_NAME.test(raw)) { return raw; }
    return PLACEHOLDER;
  }

  function render() {
    var key = normalize(state.query.trim());
    rows.forEach(function (row) {
      var sources = (row.getAttribute('data-source') || '').split(' ');
      var okSource = state.source === 'all' || sources.indexOf(state.source) !== -1;
      var okQuery = !key || row.getAttribute('data-key').indexOf(key) !== -1;
      row.hidden = !(okSource && okQuery);
    });
    var visible = visibleRows();
    var filtering = state.query.trim() !== '' || state.source !== 'all';

    if (summary) {
      summary.textContent = filtering
        ? visible.length + ' of ' + rows.length + ' package' + (rows.length === 1 ? '' : 's')
        : summary.getAttribute('data-default');
    }

    var pkg = commandPackage(visible);
    if (cmdPkg) { cmdPkg.textContent = pkg; }

    var upstream = visible.length === 0 && state.source === 'all' && state.query.trim() !== '' && pkg !== PLACEHOLDER;
    if (cmdNote) {
      cmdNote.hidden = !upstream;
      cmdNote.textContent = upstream
        ? 'Not stored on this server yet. Installing it through this index fetches it from PyPI and keeps a copy here.'
        : '';
    }

    if (nomatch) {
      var empty = rows.length > 0 && visible.length === 0;
      nomatch.hidden = !empty;
      if (empty) {
        nomatch.textContent = state.query.trim()
          ? 'No ' + (state.source === 'all' ? '' : state.source + ' ') + 'packages match "' + state.query.trim() + '".'
          : 'No ' + state.source + ' packages yet.';
      }
    }

    if (filtering) { announce(visible.length + ' of ' + rows.length + ' packages shown'); }
  }

  function syncUrl() {
    if (!window.history || !window.history.replaceState) { return; }
    var url = new URL(window.location.href);
    if (state.query.trim()) { url.searchParams.set('q', state.query.trim()); } else { url.searchParams.delete('q'); }
    window.history.replaceState(null, '', url.pathname + url.search + url.hash);
  }

  input.addEventListener('input', function () {
    state.query = input.value;
    render();
    syncUrl();
  });

  input.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && input.value) {
      input.value = '';
      state.query = '';
      render();
      syncUrl();
    } else if (event.key === 'Enter') {
      var visible = visibleRows();
      if (visible.length === 1) {
        var link = visible[0].querySelector('a');
        if (link) { window.location.href = link.href; }
      }
    }
  });

  doc.addEventListener('keydown', function (event) {
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) { return; }
    var tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || event.target.isContentEditable) { return; }
    event.preventDefault();
    input.focus();
    input.select();
  });

  Array.prototype.forEach.call(sourceButtons, function (button) {
    button.addEventListener('click', function () {
      state.source = button.getAttribute('data-source-filter');
      Array.prototype.forEach.call(sourceButtons, function (other) {
        other.setAttribute('aria-pressed', other === button ? 'true' : 'false');
      });
      render();
    });
  });

  function sortValue(row, key) {
    if (key === 'name') { return row.getAttribute('data-key'); }
    return Number(row.getAttribute('data-' + key)) || 0;
  }

  function applySort() {
    var key = state.sortKey;
    var dir = state.sortDir;
    rows.sort(function (a, b) {
      var x = sortValue(a, key);
      var y = sortValue(b, key);
      var result = key === 'name' ? (x < y ? -1 : x > y ? 1 : 0) : x - y;
      if (result === 0) { result = sortValue(a, 'name') < sortValue(b, 'name') ? -1 : 1; }
      return result * dir;
    });
    var fragment = doc.createDocumentFragment();
    rows.forEach(function (row) { fragment.appendChild(row); });
    body.appendChild(fragment);
    Array.prototype.forEach.call(sortButtons, function (button) {
      var cell = button.parentNode;
      cell.setAttribute('aria-sort', button.getAttribute('data-sort') === key ? (dir === 1 ? 'ascending' : 'descending') : 'none');
    });
  }

  Array.prototype.forEach.call(sortButtons, function (button) {
    button.addEventListener('click', function () {
      var key = button.getAttribute('data-sort');
      if (state.sortKey === key) { state.sortDir = -state.sortDir; }
      else { state.sortKey = key; state.sortDir = key === 'name' ? 1 : -1; }
      applySort();
    });
  });

  var initial = new URLSearchParams(window.location.search).get('q');
  if (initial) { input.value = initial; state.query = initial; }
  render();
})();
