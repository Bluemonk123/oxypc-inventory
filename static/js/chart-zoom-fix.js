/* Chart.js hover fix for the app-wide CSS zoom (body { zoom: var(--app-zoom) },
   static/css/app.css). Under zoom, Chart.js reads mouse positions scaled by
   the zoom factor, so tooltips/hover land on the wrong bar. This plugin
   rescales each event back. ratio = rendered width / layout width — it is 1
   (no-op) when no zoom applies. Load right after chart.umd.min.js. */
(function () {
  if (!window.Chart) return;
  Chart.register({
    id: 'appZoomFix',
    beforeEvent: function (chart, args) {
      var c = chart.canvas, e = args.event;
      if (!c || !e || e.x == null) return;
      var w = c.offsetWidth;
      if (!w) return;
      var ratio = c.getBoundingClientRect().width / w;
      if (Math.abs(ratio - 1) < 0.01) return;
      e.x = e.x / ratio;
      e.y = e.y / ratio;
    }
  });
})();
