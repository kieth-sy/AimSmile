// AimSmile — Dashboard & Neumorphic Interactions

document.addEventListener('DOMContentLoaded', function () {
  // Sidebar toggle (mobile)
  const sidebar = document.querySelector('.sidebar');
  const backdrop = document.querySelector('.sidebar-backdrop');
  const burger = document.querySelector('.hamburger');
  if (burger) {
    burger.addEventListener('click', () => {
      sidebar?.classList.toggle('open');
      backdrop?.classList.toggle('show');
    });
  }
  if (backdrop) {
    backdrop.addEventListener('click', () => {
      sidebar?.classList.remove('open');
      backdrop?.classList.remove('show');
    });
  }

  // Auto-dismiss flash messages
  document.querySelectorAll('[data-autohide]').forEach((el) => {
    setTimeout(() => {
      el.style.transition = 'opacity .4s';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 400);
    }, 4000);
  });

  // Dark/Light Theme toggle
  const themeToggle = document.getElementById('themeToggleBtn');
  const html = document.documentElement;
  const savedTheme = localStorage.getItem('aimsmile-theme');

  if (savedTheme === 'dark') {
    html.classList.add('dark-theme');
  }

  if (themeToggle) {
    const icon = themeToggle.querySelector('i');
    if (html.classList.contains('dark-theme')) {
      icon?.classList.remove('bi-moon');
      icon?.classList.add('bi-sun');
    }

    themeToggle.addEventListener('click', function() {
      const iconEl = this.querySelector('i');
      if (html.classList.contains('dark-theme')) {
        html.classList.remove('dark-theme');
        localStorage.setItem('aimsmile-theme', 'light');
        if (iconEl) {
          iconEl.classList.remove('bi-sun');
          iconEl.classList.add('bi-moon');
        }
      } else {
        html.classList.add('dark-theme');
        localStorage.setItem('aimsmile-theme', 'dark');
        if (iconEl) {
          iconEl.classList.remove('bi-moon');
          iconEl.classList.add('bi-sun');
        }
      }
    });
  }
});

// Chart palette helper
const DC = {
  colors: {
    primary: '#064dc7',
    primaryLight: '#dce7ff',
    accent: '#3e72ff',
    palette: ['#3e72ff', '#ffb21a', '#a855f7', '#22d3ee', '#0ea5e9', '#67e8f9'],
  },
};

function readJSON(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  try { return JSON.parse(el.textContent); } catch (e) { return null; }
}

function gradientFill(ctx, color) {
  const channels = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(color);
  const rgb = channels
    ? `${parseInt(channels[1], 16)}, ${parseInt(channels[2], 16)}, ${parseInt(channels[3], 16)}`
    : '46, 104, 219';
  const g = ctx.createLinearGradient(0, 0, 0, 260);
  g.addColorStop(0, `rgba(${rgb}, 0.55)`);
  g.addColorStop(0.7, `rgba(${rgb}, 0.24)`);
  g.addColorStop(1, `rgba(${rgb}, 0)`);
  return g;
}

function darkenHex(color, factor = 0.84) {
  const channels = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(color);
  if (!channels) return color;
  return '#' + channels.slice(1).map((channel) => (
    Math.round(parseInt(channel, 16) * factor).toString(16).padStart(2, '0')
  )).join('');
}

function chartColorGradient(ctx, color, endColor = darkenHex(color)) {
  const gradient = ctx.createLinearGradient(0, 0, 0, 260);
  gradient.addColorStop(0, color);
  gradient.addColorStop(1, endColor);
  return gradient;
}

function chartTheme() {
  const styles = getComputedStyle(document.documentElement);
  const isDark = document.documentElement.classList.contains('dark-theme');
  return {
    surface: styles.getPropertyValue('--nm-bg').trim() || '#e5ecf4',
    darkShadow: styles.getPropertyValue('--nm-shadow-dark').trim() || 'rgba(163, 177, 198, 0.48)',
    lightShadow: styles.getPropertyValue('--nm-shadow-light').trim() || 'rgba(255, 255, 255, 0.95)',
    primary: DC.colors.primary,
    text: styles.getPropertyValue('--nm-text-dark').trim() || '#1e293b',
    subText: styles.getPropertyValue('--nm-text-sub').trim() || '#64748b',
    mutedText: styles.getPropertyValue('--nm-text-muted').trim() || '#94a3b8',
    grid: isDark ? 'rgba(148, 163, 184, 0.14)' : 'rgba(163, 177, 198, 0.18)',
  };
}

function neumorphicChartPlugin(theme) {
  const shadows = [
    { color: theme.darkShadow, x: 3, y: 4 },
    { color: theme.lightShadow, x: -2, y: -2 },
  ];

  function drawWithShadow(ctx, drawShape, shadow) {
    ctx.save();
    ctx.shadowColor = shadow.color;
    ctx.shadowBlur = 7;
    ctx.shadowOffsetX = shadow.x;
    ctx.shadowOffsetY = shadow.y;
    drawShape();
    ctx.restore();
  }

  return {
    id: 'neumorphicChartDepth',
    beforeDatasetDraw(chart, args) {
      const meta = chart.getDatasetMeta(args.index);
      const dataset = chart.data.datasets[args.index];
      const ctx = chart.ctx;

      if (meta.type === 'line') {
        const points = meta.data.filter((point) => !point.skip);
        if (points.length < 2) return;
        shadows.forEach((shadow) => {
          drawWithShadow(ctx, () => {
            ctx.beginPath();
            points.forEach((point, index) => {
              if (index === 0) ctx.moveTo(point.x, point.y);
              else ctx.lineTo(point.x, point.y);
            });
            ctx.strokeStyle = dataset.borderColor || theme.primary;
            ctx.lineWidth = Math.max(2, dataset.borderWidth || 2);
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            ctx.stroke();
          }, shadow);
        });
      } else if (meta.type === 'bar') {
        meta.data.forEach((bar, index) => {
          const { x, y, base, width } = bar.getProps(['x', 'y', 'base', 'width'], true);
          const fill = Array.isArray(dataset.backgroundColor)
            ? dataset.backgroundColor[index]
            : dataset.backgroundColor;
          shadows.forEach((shadow) => {
            drawWithShadow(ctx, () => {
              ctx.beginPath();
              ctx.rect(x - width / 2, Math.min(y, base), width, Math.abs(base - y));
              ctx.fillStyle = fill || theme.primary;
              ctx.fill();
            }, shadow);
          });
        });
      } else if (meta.type === 'pie' || meta.type === 'doughnut' || meta.type === 'polarArea') {
        meta.data.forEach((arc, index) => {
          const { x, y, innerRadius, outerRadius, startAngle, endAngle } = arc.getProps(
            ['x', 'y', 'innerRadius', 'outerRadius', 'startAngle', 'endAngle'],
            true
          );
          const fill = Array.isArray(dataset.backgroundColor)
            ? dataset.backgroundColor[index]
            : dataset.backgroundColor;
          shadows.forEach((shadow) => {
            drawWithShadow(ctx, () => {
              ctx.beginPath();
              ctx.arc(x, y, outerRadius, startAngle, endAngle);
              if (innerRadius > 0) {
                ctx.arc(x, y, innerRadius, endAngle, startAngle, true);
              } else {
                ctx.lineTo(x, y);
              }
              ctx.closePath();
              ctx.fillStyle = fill || theme.primary;
              ctx.fill();
            }, shadow);
          });
        });
      }
    },
  };
}

// Chart.js plugin to draw values above peak dots (matching screenshot)
const peakCalloutPlugin = {
  id: 'peakCallout',
  afterDatasetsDraw(chart) {
    const { ctx } = chart;
    chart.data.datasets.forEach((dataset, datasetIndex) => {
      const meta = chart.getDatasetMeta(datasetIndex);
      if (!meta.visible) return;
      meta.data.forEach((element, index) => {
        const val = dataset.data[index];
        if (val > 0) {
          ctx.save();
          ctx.font = '700 11px Inter, sans-serif';
          ctx.fillStyle = chartTheme().primary;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'bottom';
          let labelText = val;
          if (typeof val === 'number') {
            if (val >= 100) {
              labelText = '₱' + Math.round(val);
            } else {
              labelText = val.toString();
            }
          }
          ctx.fillText(labelText, element.x, element.y - 7);
          ctx.restore();
        }
      });
    });
  }
};

function lineChart(canvasId, dataId, label, neumorphic = true) {
  const canvas = document.getElementById(canvasId);
  const data = readJSON(dataId);
  if (!canvas || !data) return;
  const ctx = canvas.getContext('2d');
  const theme = chartTheme();
  
  new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.labels,
      datasets: [{
        label: label || '',
        data: data.data,
        borderColor: chartColorGradient(
          ctx,
          DC.colors.accent,
          DC.colors.primary,
        ),
        backgroundColor: gradientFill(ctx, theme.primary),
        fill: true,
        tension: 0.44,
        borderWidth: 2.8,
        pointBackgroundColor: theme.primary,
        pointBorderColor: theme.surface,
        pointBorderWidth: 3,
        pointRadius: data.data.map(v => v > 0 ? 4.5 : 0),
        pointHoverRadius: 6,
      }],
    },
    options: {
      ...baseOptions(theme, neumorphic),
      layout: { padding: 10 },
      plugins: {
        legend: { display: false },
        ...(neumorphic ? { tooltip: {
          backgroundColor: theme.surface,
          titleColor: theme.text,
          bodyColor: theme.subText,
          borderColor: theme.darkShadow,
          borderWidth: 1,
          titleFont: { size: 11, weight: '700' },
          bodyFont: { size: 12 },
          padding: 8,
          cornerRadius: 8,
          callbacks: {
            label: function(ctx) {
              return (ctx.parsed.y >= 100 ? '₱' : '') + ctx.parsed.y.toLocaleString();
            }
          }
        } } : {}),
      }
    },
    plugins: [peakCalloutPlugin, ...(neumorphic ? [neumorphicChartPlugin(theme)] : [])],
  });
}

function barChart(canvasId, dataId, label, options = {}) {
  const canvas = document.getElementById(canvasId);
  const data = readJSON(dataId);
  if (!canvas || !data) return;
  const theme = chartTheme();
  const yTicks = {
    color: theme.mutedText,
    font: { size: 10 },
    ...(options.currency
      ? {
          callback: (value) => `₱${Number(value).toLocaleString()}`,
        }
      : { stepSize: 0.5 }),
  };
  const chartOptions = baseOptions(theme);
  new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels: data.labels,
      datasets: [{
        label: label || '',
        data: data.data,
        backgroundColor: chartColorGradient(
          canvas.getContext('2d'),
          DC.colors.accent,
          DC.colors.primary,
        ),
        borderColor: theme.surface,
        borderWidth: 2,
        borderSkipped: false,
        borderRadius: 8,
        maxBarThickness: options.maxBarThickness || 16,
      }],
    },
    options: {
      ...chartOptions,
      plugins: {
        ...chartOptions.plugins,
        ...(options.currency
          ? {
              tooltip: {
                ...chartOptions.plugins.tooltip,
                callbacks: {
                  label: (context) => (
                    `₱${Number(context.parsed.y).toLocaleString()}`
                  ),
                },
              },
            }
          : {}),
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            color: theme.mutedText,
            font: { size: 10 },
            maxRotation: 0,
            callback: function(val, index) {
              // Show fewer labels on x-axis like Dec 25, Mar 26, Jun 26, Sep 26, Nov 26
              const labels = data.labels;
              if (index === 0 || index === labels.length - 1 || index % 3 === 0) {
                return labels[index];
              }
              return '';
            }
          },
          border: { display: false },
        },
        y: {
          grid: { color: theme.grid },
          ticks: yTicks,
          beginAtZero: true,
          border: { display: false },
        },
      }
    },
    plugins: [neumorphicChartPlugin(theme)],
  });
}

function doughnutChart(canvasId, dataId, options = {}) {
  const canvas = document.getElementById(canvasId);
  const data = readJSON(dataId);
  if (!canvas) return;
  const theme = chartTheme();

  const labels = (data && data.labels && data.labels.length) ? data.labels : ['Appointments'];
  const hasValues = data && data.data && data.data.length && data.data.some(v => v > 0);
  const values = hasValues ? data.data : (options.showLegend ? labels.map(() => 0) : [1]);
  const ctx = canvas.getContext('2d');
  const colors = labels.map((label, index) => {
    let color;
    if (options.colors && options.colors[label]) {
      color = options.colors[label];
    } else if (values.length === 1 || values.filter(value => value > 0).length === 1) {
      color = theme.primary;
    } else {
      color = DC.colors.palette[index % DC.colors.palette.length];
    }
    return chartColorGradient(ctx, color);
  });

  new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: labels,
      datasets: [{
        data: values,
        backgroundColor: colors,
        borderColor: theme.surface,
        borderWidth: 3,
        borderAlign: 'inner',
        borderRadius: 4,
        spacing: 2,
        hoverOffset: 4,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '74%',
      plugins: {
        legend: options.showLegend ? {
          display: true,
          position: 'bottom',
          labels: {
            color: theme.subText,
            usePointStyle: true,
            pointStyle: 'circle',
            boxWidth: 10,
            padding: 8,
            font: { size: 10 },
          },
        } : { display: false },
        tooltip: {
          backgroundColor: theme.surface,
          titleColor: theme.text,
          bodyColor: theme.subText,
          borderColor: theme.darkShadow,
          borderWidth: 1,
          cornerRadius: 8,
          bodyFont: { size: 11 }
        }
      },
    },
    plugins: [neumorphicChartPlugin(theme)],
  });
}

function appointmentStatusChart(canvasId, dataId, showLegend = true) {
  doughnutChart(canvasId, dataId, {
    showLegend,
    colors: {
      Pending: '#ffb21a',
      Confirmed: '#08b9f4',
      Completed: '#22d3ee',
      Cancelled: '#fb923c',
      'No Show': '#ef4444',
    },
  });
}

function polarAreaChart(canvasId, dataId) {
  const canvas = document.getElementById(canvasId);
  const data = readJSON(dataId);
  if (!canvas || !data || !data.labels?.length || !data.data?.length) return;

  const ctx = canvas.getContext('2d');
  const colors = data.labels.map((_, index) => chartColorGradient(
    ctx,
    DC.colors.palette[index % DC.colors.palette.length],
  ));
  const total = data.data.reduce((sum, value) => sum + value, 0);
  const theme = chartTheme();

  new Chart(ctx, {
    type: 'polarArea',
    data: {
      labels: data.labels,
      datasets: [{
        data: data.data,
        backgroundColor: colors,
        borderColor: theme.surface,
        borderWidth: 3,
        borderAlign: 'inner',
        borderRadius: 5,
        spacing: 2,
        hoverOffset: 7,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: 10 },
      scales: {
        r: {
          beginAtZero: true,
          grid: { color: theme.grid },
          angleLines: { color: theme.grid },
          pointLabels: { display: false },
          ticks: {
            color: theme.mutedText,
            backdropColor: 'transparent',
            showLabelBackdrop: false,
            font: { size: 9 },
          },
        },
      },
      plugins: {
        legend: {
          position: 'right',
          labels: {
            color: theme.subText,
            usePointStyle: true,
            pointStyle: 'circle',
            boxWidth: 9,
            boxHeight: 9,
            padding: 12,
            font: { family: 'Inter, sans-serif', size: 10 },
          },
        },
        tooltip: {
          backgroundColor: theme.surface,
          titleColor: theme.text,
          bodyColor: theme.subText,
          borderColor: theme.darkShadow,
          borderWidth: 1,
          cornerRadius: 8,
          bodyFont: { size: 11 },
          callbacks: {
            label(context) {
              const value = context.parsed.r;
              const percentage = total ? Math.round((value / total) * 100) : 0;
              return `${context.label}: ${value} (${percentage}%)`;
            },
          },
        },
      },
    },
    plugins: [neumorphicChartPlugin(theme)],
  });
}

function baseOptions(theme = chartTheme(), neumorphic = true) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
      legend: { display: false },
      ...(neumorphic ? { tooltip: {
        backgroundColor: theme.surface,
        titleColor: theme.text,
        bodyColor: theme.subText,
        borderColor: theme.darkShadow,
        borderWidth: 1,
        cornerRadius: 8,
        titleFont: { size: 11, weight: '700' },
        bodyFont: { size: 11 },
        padding: 8,
      } } : {}),
    },
    scales: {
      x: {
        grid: { display: false },
        ticks: { color: theme.mutedText, font: { size: 10 } },
        border: { display: false },
      },
      y: {
        grid: { color: theme.grid, borderDash: [4, 4] },
        ticks: { color: theme.mutedText, font: { size: 10 } },
        beginAtZero: true,
        border: { display: false },
      },
    },
  };
}

// Dynamic formset helper
function addFormsetRow(prefix) {
  const totalForms = document.querySelector(`#id_${prefix}-TOTAL_FORMS`);
  const container = document.querySelector(`#${prefix}-rows`);
  const template = document.querySelector(`#${prefix}-empty`);
  if (!totalForms || !container || !template) return;
  const index = parseInt(totalForms.value, 10);
  const html = template.innerHTML.replace(/__prefix__/g, index);
  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  container.appendChild(wrapper);
  totalForms.value = index + 1;
}
