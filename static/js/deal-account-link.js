/* Deal ID <-> Account Name dropdowns (2026-11-11, Pankaj): Record New Payment /
   Record New Receipt. Picking a deal fills its account; picking an account
   narrows the deal list to that account's deals (and picks it when there is
   only one). Markup: <select data-link-deal> options carry data-contact;
   <select data-link-account> in the same form. */
(function () {
  document.querySelectorAll('form').forEach(function (f) {
    var deal = f.querySelector('[data-link-deal]'), acc = f.querySelector('[data-link-account]');
    if (!deal || !acc) return;
    // Payment Date defaults to today, like the Complete Payment form
    var dt = f.querySelector('input[type=date]');
    if (dt && !dt.value) { var d = new Date(); dt.value = new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); }
    deal.addEventListener('change', function () {
      var o = deal.options[deal.selectedIndex], c = o ? o.getAttribute('data-contact') : '';
      if (c) acc.value = c;
      filterDeals();
    });
    acc.addEventListener('change', function () {
      filterDeals();
      var cur = deal.options[deal.selectedIndex];
      if (cur && cur.value && cur.getAttribute('data-contact') !== acc.value) deal.value = '';
      var mine = Array.prototype.filter.call(deal.options, function (o) { return o.value && !o.hidden; });
      if (!deal.value && mine.length === 1) deal.value = mine[0].value;
    });
    function filterDeals() {
      Array.prototype.forEach.call(deal.options, function (o) {
        if (!o.value) return;
        var show = !acc.value || o.getAttribute('data-contact') === acc.value;
        o.hidden = !show; o.disabled = !show;
      });
    }
  });
})();
