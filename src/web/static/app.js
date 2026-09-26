/* Temabot: меню на телефоне, ползунки и графики.
   Данные для графиков лежат в <script type="application/json"> рядом с холстом:
   шаблон кладёт их через |tojson, а сюда они попадают через JSON.parse —
   никакой вставки строк в HTML. */
(function () {
  "use strict";

  var nf = new Intl.NumberFormat("ru-RU");

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function plural(n, forms) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return forms[2];
    if (b > 1 && b < 5) return forms[1];
    if (b === 1) return forms[0];
    return forms[2];
  }

  function formatValue(value, unit) {
    if (unit === "usd") {
      return "$" + (value >= 1 ? value.toFixed(2) : value.toFixed(3));
    }
    var text = nf.format(Math.round(value * 10) / 10);
    if (Array.isArray(unit)) return text + " " + plural(Math.round(value), unit);
    return unit ? text + " " + unit : text;
  }

  function parseDay(iso) {
    var p = String(iso).split("-");
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }

  function shortDay(iso) {
    var d = parseDay(iso);
    return String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0");
  }

  function longDay(iso) {
    return parseDay(iso).toLocaleDateString("ru-RU", {
      weekday: "short", day: "numeric", month: "long"
    });
  }

  // ---------------------------------------------------------------- «Ещё»

  function initSheet() {
    document.querySelectorAll("[data-sheet-toggle]").forEach(function (el) {
      el.addEventListener("click", function (event) {
        event.preventDefault();
        var open = document.body.classList.toggle("sheet-open");
        document.querySelectorAll("[data-sheet-toggle]").forEach(function (b) {
          b.setAttribute("aria-expanded", open ? "true" : "false");
        });
      });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") document.body.classList.remove("sheet-open");
    });
  }

  // ------------------------------------------------------------- ползунки

  function initSliders(root) {
    (root || document).querySelectorAll(".slider input[type=range]").forEach(function (input) {
      var out = document.getElementById(input.dataset.output || "");
      if (!out || input.dataset.bound) return;
      input.dataset.bound = "1";
      var show = function () { out.textContent = Number(input.value).toFixed(2); };
      input.addEventListener("input", show);
      show();
    });
  }

  // --------------------------------------------------------------- графики

  var charts = [];

  // Подпись над самым высоким столбцом: одна, а не на каждом (остальное — в подсказке и таблице)
  var peakLabel = {
    id: "peakLabel",
    afterDatasetsDraw: function (chart, args, opts) {
      if (!opts || !opts.enabled) return;
      var data = chart.data.datasets[0].data;
      var max = -Infinity, at = -1;
      data.forEach(function (v, i) { if (v > max) { max = v; at = i; } });
      if (at < 0 || max <= 0) return;
      var bar = chart.getDatasetMeta(0).data[at];
      var ctx = chart.ctx;
      ctx.save();
      ctx.font = "600 12px " + css("--font");
      ctx.fillStyle = css("--ink-2");
      ctx.textAlign = "center";
      ctx.textBaseline = "bottom";
      ctx.fillText(formatValue(max, opts.unit === "usd" ? "usd" : null), bar.x, bar.y - 6);
      ctx.restore();
    }
  };

  // Значения у концов горизонтальных столбцов — это таблица лидеров.
  // Только число: единица уже в подзаголовке графика и в подсказке.
  var tipLabels = {
    id: "tipLabels",
    afterDatasetsDraw: function (chart, args, opts) {
      if (!opts || !opts.enabled) return;
      var ctx = chart.ctx;
      var meta = chart.getDatasetMeta(0);
      ctx.save();
      ctx.font = "600 12.5px " + css("--font");
      ctx.fillStyle = css("--ink-2");
      ctx.textBaseline = "middle";
      meta.data.forEach(function (bar, i) {
        var value = chart.data.datasets[0].data[i];
        ctx.fillText(formatValue(value, opts.unit === "usd" ? "usd" : null), bar.x + 8, bar.y);
      });
      ctx.restore();
    }
  };

  function tooltipStyle() {
    return {
      backgroundColor: css("--card"),
      borderColor: css("--line-strong"),
      borderWidth: 1,
      titleColor: css("--muted"),
      titleFont: { family: css("--font"), size: 12, weight: "400" },
      bodyColor: css("--ink"),
      bodyFont: { family: css("--font"), size: 14, weight: "600" },
      padding: 10,
      cornerRadius: 10,
      displayColors: false,
      caretSize: 0
    };
  }

  function columnsConfig(spec) {
    var accent = css("--accent");
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var narrow = window.matchMedia("(max-width: 719px)").matches;
    return {
      type: "bar",
      data: {
        labels: spec.labels,
        datasets: [{
          data: spec.values,
          backgroundColor: accent,
          hoverBackgroundColor: css("--accent-ink"),
          borderRadius: { topLeft: 4, topRight: 4, bottomLeft: 0, bottomRight: 0 },
          borderSkipped: false,
          maxBarThickness: 24,
          categoryPercentage: 0.84,
          barPercentage: 0.92
        }]
      },
      options: {
        maintainAspectRatio: false,
        animation: reduce ? false : { duration: 350 },
        layout: { padding: { top: 22 } },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          peakLabel: { enabled: true, unit: spec.unit },
          tooltip: Object.assign(tooltipStyle(), {
            callbacks: {
              title: function (items) { return longDay(spec.labels[items[0].dataIndex]); },
              label: function (item) { return formatValue(item.raw, spec.unit); }
            }
          })
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: css("--axis") },
            ticks: {
              color: css("--muted"),
              font: { family: css("--font"), size: 11.5 },
              maxRotation: 0,
              autoSkip: true,
              maxTicksLimit: narrow ? 5 : 10,
              callback: function (value, index) { return shortDay(spec.labels[index]); }
            }
          },
          y: {
            beginAtZero: true,
            grid: { color: css("--grid"), lineWidth: 1, drawTicks: false },
            border: { display: false },
            ticks: {
              color: css("--muted"),
              font: { family: css("--font"), size: 11.5 },
              padding: 8,
              maxTicksLimit: 5,
              precision: spec.unit === "usd" ? undefined : 0,
              callback: function (value) {
                return spec.unit === "usd" ? "$" + value : nf.format(value);
              }
            }
          }
        }
      },
      plugins: [peakLabel]
    };
  }

  function barsConfig(spec) {
    var reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    var max = Math.max.apply(null, spec.values.concat([1]));
    return {
      type: "bar",
      data: {
        labels: spec.labels,
        datasets: [{
          data: spec.values,
          backgroundColor: css("--accent"),
          hoverBackgroundColor: css("--accent-ink"),
          borderRadius: { topRight: 4, bottomRight: 4, topLeft: 0, bottomLeft: 0 },
          borderSkipped: false,
          maxBarThickness: 22,
          categoryPercentage: 0.8,
          barPercentage: 0.9
        }]
      },
      options: {
        indexAxis: "y",
        maintainAspectRatio: false,
        animation: reduce ? false : { duration: 350 },
        layout: { padding: { right: 44 } },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tipLabels: { enabled: true, unit: spec.unit },
          tooltip: Object.assign(tooltipStyle(), {
            callbacks: {
              title: function (items) { return spec.labels[items[0].dataIndex]; },
              label: function (item) { return formatValue(item.raw, spec.unit); }
            }
          })
        },
        scales: {
          x: { display: false, beginAtZero: true, suggestedMax: spec.max || max },
          y: {
            grid: { display: false },
            border: { color: css("--axis") },
            ticks: {
              color: css("--ink-2"),
              font: { family: css("--font"), size: 13 },
              callback: function (value, index) {
                var name = String(spec.labels[index]);
                return name.length > 22 ? name.slice(0, 21) + "…" : name;
              }
            }
          }
        }
      },
      plugins: [tipLabels]
    };
  }

  function drawCharts() {
    if (typeof window.Chart === "undefined") return;
    charts.forEach(function (c) { c.destroy(); });
    charts = [];
    document.querySelectorAll("canvas[data-chart]").forEach(function (canvas) {
      var source = document.getElementById(canvas.dataset.source || "");
      if (!source) return;
      var spec;
      try { spec = JSON.parse(source.textContent); } catch (e) { return; }
      if (!spec || !spec.values || !spec.values.length) return;
      var config = canvas.dataset.chart === "bars" ? barsConfig(spec) : columnsConfig(spec);
      charts.push(new window.Chart(canvas, config));
    });
  }

  function init() {
    initSheet();
    initSliders();
    drawCharts();
    // тёмная тема — свои шаги цвета, а не инверсия: перерисовываем с новыми токенами
    var scheme = window.matchMedia("(prefers-color-scheme: dark)");
    if (scheme.addEventListener) scheme.addEventListener("change", drawCharts);
    document.body.addEventListener("htmx:afterSwap", function (event) {
      initSliders(event.target);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
