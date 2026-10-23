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

  /* ---- Live search, filter, sort and paging without a full page load --------
     The server renders every state (see ?q=, ?source=, ?sort=, ?dir=, ?page=), so this only swaps
     the result list and the pip command into the page. Without JavaScript the same links and the
     search form work as plain navigation. */
  var form = doc.querySelector('[data-finder]');
  var input = doc.getElementById('pkg-search');
  if (!form || !input) { return; }

  var timer = null;
  var controller = null;
  var ticket = 0;

  function swap(selector, from) {
    var current = doc.querySelector(selector);
    var fresh = from.querySelector(selector);
    if (current && fresh) { current.replaceWith(fresh); }
  }

  function syncHiddenFields(from) {
    var fresh = from.querySelector('[data-finder]');
    if (!fresh) { return; }
    Array.prototype.forEach.call(form.querySelectorAll('input[type="hidden"]'), function (node) { node.remove(); });
    Array.prototype.forEach.call(fresh.querySelectorAll('input[type="hidden"]'), function (node) {
      form.appendChild(doc.importNode(node, true));
    });
  }

  // mode: 'push' adds a history entry, 'replace' updates the current one, 'none' leaves history alone
  function load(url, mode, scroll) {
    if (controller) { controller.abort(); }
    controller = window.AbortController ? new AbortController() : null;
    var mine = ++ticket;
    var options = { headers: { Accept: 'text/html' }, credentials: 'same-origin' };
    if (controller) { options.signal = controller.signal; }
    window.fetch(url, options).then(function (response) {
      if (!response.ok) { throw new Error('HTTP ' + response.status); }
      return response.text();
    }).then(function (text) {
      if (mine !== ticket) { return; }  // a newer request is already on its way
      var next = new DOMParser().parseFromString(text, 'text/html');
      swap('#results', next);
      swap('#hero-cmd', next);
      syncHiddenFields(next);
      doc.title = next.title;
      if (mode === 'push') { window.history.pushState(null, '', url); }
      else if (mode === 'replace') { window.history.replaceState(null, '', url); }
      var summary = doc.querySelector('.index__summary');
      var nomatch = doc.querySelector('.nomatch');
      announce((summary && summary.textContent.trim()) || (nomatch && nomatch.textContent.trim()) || '');
      if (scroll) {
        var results = doc.getElementById('results');
        if (results) { results.scrollIntoView({ block: 'start' }); }
      }
    }).catch(function (error) {
      if (error && error.name === 'AbortError') { return; }
      window.location.href = url;  // anything unexpected: fall back to a normal page load
    });
  }

  function searchUrl() {
    var params = new URLSearchParams(new window.FormData(form));
    var q = (params.get('q') || '').trim();
    if (q) { params.set('q', q); } else { params.delete('q'); }
    var query = params.toString();
    return new URL(form.getAttribute('action') || window.location.pathname, window.location.href).pathname + (query ? '?' + query : '');
  }

  input.addEventListener('input', function () {
    window.clearTimeout(timer);
    timer = window.setTimeout(function () { load(searchUrl(), 'replace', false); }, 250);
  });

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    window.clearTimeout(timer);
    load(searchUrl(), 'push', false);
  });

  input.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && input.value) {
      input.value = '';
      window.clearTimeout(timer);
      load(searchUrl(), 'replace', false);
    }
  });

  doc.addEventListener('click', function (event) {
    var link = event.target.closest('a[data-nav]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) { return; }
    event.preventDefault();
    window.clearTimeout(timer);
    var clears = link.textContent.indexOf('Clear') === 0;
    if (clears) { input.value = ''; }
    load(link.getAttribute('href'), 'push', !!link.closest('.pager, .pkgs'));
  });

  window.addEventListener('popstate', function () {
    var q = new URLSearchParams(window.location.search).get('q') || '';
    input.value = q;
    load(window.location.pathname + window.location.search, 'none', false);
  });

  doc.addEventListener('keydown', function (event) {
    if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey) { return; }
    var tag = (event.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || event.target.isContentEditable) { return; }
    event.preventDefault();
    input.focus();
    input.select();
  });
})();
