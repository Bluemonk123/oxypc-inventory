// OxyPC Inventory — General JS

// Tile counts (2026-11-08): Indian short form from one lakh up, same rule as
// templates_config.tile_num -- oxtShort(125000) -> "1.250 Lk",
// oxtShort(24000000, true) -> "₹2.400 Cr". Below a lakh: plain grouping
// (en-US for counts, en-IN for rupees). Use for numbers page JS writes into
// .oxt-* tiles; set el.title = full figure if wanted.
window.oxtShort = function (n, rupee) {
  n = Number(n) || 0;
  var a = Math.abs(n), sign = n < 0 ? '-' : '', cur = rupee ? '\u20B9' : '';
  if (a < 1e5) return sign + cur + Math.round(a).toLocaleString(rupee ? 'en-IN' : 'en-US');
  var big = a >= 1e7, v = (a / (big ? 1e7 : 1e5)).toFixed(3);   // always 3 decimals (2026-11-08)
  return sign + cur + v + (big ? ' Cr' : ' Lk');
};

// Auto-dismiss alerts after 4 seconds — excludes .no-autohide, used by
// persistent status banners (e.g. Attendance's Check-In/Check-Out card)
// that happen to reuse alert-success/alert-info styling but aren't a
// transient toast message.
document.addEventListener('DOMContentLoaded', function () {
  setTimeout(function () {
    document.querySelectorAll('.alert.alert-success:not(.no-autohide), .alert.alert-info:not(.no-autohide)').forEach(function (el) {
      // close() REMOVES the node. An alert inside a modal is a scope/preview
      // panel the modal's JS writes into every time it opens, never a flash
      // message — removing it makes the modal work exactly once per page load.
      if (el.closest('.modal')) return;
      var bsAlert = bootstrap.Alert.getOrCreateInstance(el);
      if (bsAlert) bsAlert.close();
    });
  }, 4000);

  // Sidebar toggle
  var toggleBtn = document.getElementById('sidebarToggle');
  var sidebar = document.getElementById('sidebar');
  var overlay = document.getElementById('sidebar-overlay');

  function closeMobileSidebar() {
    sidebar.classList.remove('show');
    if (overlay) overlay.classList.remove('show');
  }

  if (toggleBtn && sidebar) {
    toggleBtn.addEventListener('click', function () {
      if (window.innerWidth <= 768) {
        sidebar.classList.toggle('show');
        if (overlay) overlay.classList.toggle('show');
      } else {
        sidebar.classList.toggle('collapsed');
      }
    });
  }

  if (overlay) {
    overlay.addEventListener('click', closeMobileSidebar);
  }

  // Currency formatting helper (Indian Rupees)
  window.formatINR = function (amount) {
    return '₹' + Number(amount).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  };

  // Confirm destructive actions
  document.querySelectorAll('[data-confirm]').forEach(function (el) {
    el.addEventListener('click', function (e) {
      if (!confirm(el.dataset.confirm || 'Are you sure?')) {
        e.preventDefault();
      }
    });
  });

  // Initialize all DataTables not already initialized
  if (typeof $.fn.DataTable !== 'undefined') {
    $('table.auto-datatable').DataTable({ pageLength: 25 });
  }

  // Highlight active nav
  var path = window.location.pathname;
  document.querySelectorAll('#sidebar .nav-link').forEach(function (link) {
    if (link.getAttribute('href') === path) {
      link.classList.add('active');
    }
  });
});
