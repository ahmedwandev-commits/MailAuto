/* Tiny look-alikes of the widgets used by the real screens:
   - select2 searchable dropdowns (same CSS class names as select2)
   - "select all" checkbox in table headers
   - DataTables-style client-side search box
   Mock only - not used by the bot. */
(function () {
  function norm(s) {
    return (s || '').toLowerCase().replace(/[إأآ]/g, 'ا').replace(/ة/g, 'ه').replace(/ى/g, 'ي').replace(/\s+/g, ' ').trim();
  }
  function closeAll() {
    document.querySelectorAll('.select2-dropdown-holder').forEach(function (e) { e.remove(); });
    document.querySelectorAll('.select2-container--open').forEach(function (e) { e.classList.remove('select2-container--open'); });
  }
  function enhance(sel) {
    sel.classList.add('select2-hidden-accessible');
    var cont = document.createElement('span');
    cont.className = 'select2 select2-container select2-container--default';
    cont.innerHTML = '<span class="selection"><span class="select2-selection select2-selection--single" role="combobox" tabindex="0">' +
      '<span class="select2-selection__rendered"></span><span class="select2-selection__arrow">&#9662;</span></span></span>';
    sel.parentNode.insertBefore(cont, sel.nextSibling);
    var rendered = cont.querySelector('.select2-selection__rendered');
    function render() { var o = sel.options[sel.selectedIndex]; rendered.textContent = o ? o.text : ''; }
    render();
    sel.addEventListener('change', render);
    cont.querySelector('.select2-selection').addEventListener('click', function (ev) {
      ev.stopPropagation();
      var wasOpen = cont.classList.contains('select2-container--open');
      closeAll();
      if (wasOpen) return;
      cont.classList.add('select2-container--open');
      var r = cont.getBoundingClientRect();
      var dd = document.createElement('span');
      dd.className = 'select2-container select2-container--default select2-container--open select2-dropdown-holder';
      dd.style.cssText = 'position:absolute;left:' + (r.left + window.scrollX) + 'px;top:' + (r.bottom + window.scrollY) +
        'px;width:' + r.width + 'px;z-index:9999';
      dd.innerHTML = '<span class="select2-dropdown"><span class="select2-search select2-search--dropdown">' +
        '<input class="select2-search__field" type="search" autocomplete="off"></span>' +
        '<span class="select2-results"><ul class="select2-results__options" role="listbox"></ul></span></span>';
      document.body.appendChild(dd);
      var input = dd.querySelector('input'), ul = dd.querySelector('ul'), hi = 0;
      function choose(v) { sel.value = v; sel.dispatchEvent(new Event('change', { bubbles: true })); closeAll(); }
      function fill() {
        var q = norm(input.value);
        ul.innerHTML = '';
        var opts = Array.prototype.filter.call(sel.options, function (o) { return o.value !== '' && (!q || norm(o.text).indexOf(q) >= 0); });
        opts.forEach(function (o, i) {
          var li = document.createElement('li');
          li.className = 'select2-results__option' + (i === hi ? ' select2-results__option--highlighted' : '');
          li.setAttribute('role', 'option');
          li.textContent = o.text;
          li.dataset.value = o.value;
          li.addEventListener('mousedown', function (e) { e.preventDefault(); choose(o.value); });
          ul.appendChild(li);
        });
        if (!opts.length) ul.innerHTML = '<li class="select2-results__option select2-results__message">No results found</li>';
      }
      input.addEventListener('input', function () { hi = 0; fill(); });
      input.addEventListener('keydown', function (e) {
        var items = ul.querySelectorAll('li[data-value]');
        if (e.key === 'Enter') { e.preventDefault(); if (items[hi]) choose(items[hi].dataset.value); }
        else if (e.key === 'ArrowDown') { hi = Math.min(hi + 1, items.length - 1); fill(); }
        else if (e.key === 'ArrowUp') { hi = Math.max(hi - 1, 0); fill(); }
        else if (e.key === 'Escape') { closeAll(); }
      });
      fill();
      input.focus();
    });
  }
  document.addEventListener('click', function (e) { if (!e.target.closest('.select2-dropdown-holder')) closeAll(); });
  window.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('select.select2').forEach(enhance);
    document.querySelectorAll('input.select-all').forEach(function (cb) {
      cb.addEventListener('change', function () {
        cb.closest('table').querySelectorAll('tbody input[type=checkbox]').forEach(function (x) {
          x.checked = cb.checked; x.dispatchEvent(new Event('change', { bubbles: true }));
        });
      });
    });
    document.querySelectorAll('input.dt-search').forEach(function (inp) {
      inp.addEventListener('input', function () {
        var t = document.getElementById(inp.dataset.table), q = norm(inp.value);
        t.querySelectorAll('tbody tr').forEach(function (tr) { tr.style.display = norm(tr.innerText).indexOf(q) >= 0 ? '' : 'none'; });
      });
    });
  });
})();
