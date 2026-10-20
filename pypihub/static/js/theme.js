/* PyPihub theme: runs in <head> so the page never flashes the wrong colors.
   Preference is "auto" (follow the system), "light" or "dark". */
(function () {
  'use strict';
  var KEY = 'pypihub-theme';
  var root = document.documentElement;
  var query = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function stored() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === 'light' || value === 'dark' ? value : 'auto';
    } catch (err) {
      return 'auto';
    }
  }

  function apply(pref) {
    var theme = pref === 'auto' ? (query && query.matches ? 'dark' : 'light') : pref;
    root.setAttribute('data-bs-theme', theme);
    root.setAttribute('data-theme-pref', pref);
  }

  apply(stored());

  if (query) {
    var onChange = function () { if (stored() === 'auto') { apply('auto'); } };
    if (query.addEventListener) { query.addEventListener('change', onChange); }
    else if (query.addListener) { query.addListener(onChange); }
  }

  window.PyPihubTheme = {
    get: stored,
    set: function (pref) {
      try {
        if (pref === 'light' || pref === 'dark') { window.localStorage.setItem(KEY, pref); }
        else { window.localStorage.removeItem(KEY); pref = 'auto'; }
      } catch (err) { /* storage unavailable: the choice lasts for this page only */ }
      apply(pref);
    }
  };
})();
