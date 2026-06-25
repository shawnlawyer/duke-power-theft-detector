(function () {
  function setupPasswordToggles() {
    document.querySelectorAll('input[type="password"]').forEach((input, index) => {
      if (input.dataset.passwordToggleReady === "true") {
        return;
      }

      input.dataset.passwordToggleReady = "true";
      const wrapper = document.createElement("div");
      wrapper.className = "password-field";
      input.parentNode.insertBefore(wrapper, input);
      wrapper.appendChild(input);

      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "button-secondary password-toggle";
      toggle.textContent = "Show";
      toggle.setAttribute("aria-label", "Show password");
      toggle.setAttribute("aria-pressed", "false");
      toggle.dataset.passwordToggle = String(index + 1);

      toggle.addEventListener("click", () => {
        const shouldShow = input.type === "password";
        input.type = shouldShow ? "text" : "password";
        toggle.textContent = shouldShow ? "Hide" : "Show";
        toggle.setAttribute("aria-label", shouldShow ? "Hide password" : "Show password");
        toggle.setAttribute("aria-pressed", shouldShow ? "true" : "false");
        input.focus({ preventScroll: true });
      });

      wrapper.appendChild(toggle);
    });
  }

  setupPasswordToggles();

  const root = document.getElementById("day-detail-root");
  if (!root) {
    return;
  }

  const metricsTarget = document.getElementById("detail-metrics");
  const comparisonTarget = document.getElementById("detail-comparison");
  const spikesTarget = document.getElementById("detail-spikes");
  const weatherTarget = document.getElementById("detail-weather");
  const chartTarget = document.getElementById("detail-chart");
  const legendTarget = document.getElementById("detail-legend");
  const headingTarget = document.getElementById("detail-heading");
  const subheadingTarget = document.getElementById("detail-subheading");
  const dayButtons = Array.from(document.querySelectorAll(".day-select"));
  const initialEl = document.getElementById("initial-day-detail");
  const loadTestInterval = document.getElementById("load-test-interval");
  const loadTestExpected = document.getElementById("load-test-expected");
  const loadTestActual = document.getElementById("load-test-actual");
  const loadTestDifference = document.getElementById("load-test-difference");
  const loadTestStatus = document.getElementById("load-test-status");
  const loadTestAllOn = document.getElementById("load-test-all-on");
  const loadTestClear = document.getElementById("load-test-clear");
  const loadTestInputs = Array.from(document.querySelectorAll(".load-test-count"));
  let currentDetail = null;

  const settings = {
    account_number: root.dataset.accountNumber,
    tz: root.dataset.tz,
    night_start: root.dataset.nightStart,
    night_end: root.dataset.nightEnd,
    min_night_kw: root.dataset.minNightKw,
    night_multiplier: root.dataset.nightMultiplier,
  };

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatNumber(value, suffix) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "n/a";
    }
    return `${Number(value).toFixed(3).replace(/\.?0+$/, "")}${suffix || ""}`;
  }

  function formatSigned(value, suffix) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "n/a";
    }
    const numeric = Number(value);
    const prefix = numeric > 0 ? "+" : "";
    return `${prefix}${numeric.toFixed(3).replace(/\.?0+$/, "")}${suffix || ""}`;
  }

  function setActiveRow(date) {
    document.querySelectorAll(".day-row").forEach((row) => {
      row.classList.toggle("active", row.querySelector(".day-select")?.dataset.date === date);
    });
  }

  function renderLegend(detail) {
    const items = [];
    if (detail.series.current.length) {
      items.push({ label: detail.current_day.label, className: "chart-series-current" });
    }
    if (detail.series.previous.length && detail.previous_day) {
      items.push({ label: detail.previous_day.label, className: "chart-series-previous" });
    }
    if (detail.series.baseline.length && detail.baseline_day) {
      items.push({ label: `Reference day: ${detail.baseline_day.label}`, className: "chart-series-baseline" });
    }

    legendTarget.innerHTML = items
      .map(
        (item) => `
          <span class="legend-item">
            <span class="legend-swatch ${item.className}"></span>
            ${escapeHtml(item.label)}
          </span>
        `
      )
      .join("");
  }

  function currentMeterPoint() {
    if (!currentDetail || !loadTestInterval) {
      return null;
    }
    const points = currentDetail.series?.current || [];
    return points.find((point) => String(point.minute) === loadTestInterval.value) || null;
  }

  function updateLoadTest() {
    if (!loadTestInputs.length || !loadTestExpected || !loadTestActual || !loadTestDifference || !loadTestStatus) {
      return;
    }

    let expectedWatts = 0;
    loadTestInputs.forEach((input) => {
      const count = Math.max(0, Math.min(Number(input.value || 0), Number(input.dataset.quantity || 0)));
      const wattsEach = Number(input.dataset.wattsEach || 0);
      const total = count * wattsEach;
      expectedWatts += total;
      const totalEl = document.querySelector(`[data-item-total="${input.dataset.itemId}"]`);
      if (totalEl) {
        totalEl.textContent = `${total.toFixed(1).replace(/\.0$/, "")} W`;
      }
    });

    const expectedKw = expectedWatts / 1000;
    const actualPoint = currentMeterPoint();
    const actualKw = actualPoint ? Number(actualPoint.kw) : null;
    const differenceKw = actualKw === null ? null : actualKw - expectedKw;

    loadTestExpected.textContent = `${formatNumber(expectedKw, " kW")}`;
    loadTestActual.textContent = actualKw === null ? "n/a" : formatNumber(actualKw, " kW");
    loadTestDifference.textContent = differenceKw === null ? "n/a" : formatSigned(differenceKw, " kW");

    if (expectedKw === 0) {
      loadTestStatus.textContent = "Add counts";
      return;
    }
    if (actualKw === null) {
      loadTestStatus.textContent = "Pick interval";
      return;
    }
    if (Math.abs(differenceKw) <= 0.25) {
      loadTestStatus.textContent = "Close match";
      return;
    }
    if (differenceKw > 0.25) {
      loadTestStatus.textContent = "Meter is higher";
      return;
    }
    loadTestStatus.textContent = "Inventory is higher";
  }

  function populateLoadTestIntervals(detail) {
    if (!loadTestInterval) {
      return;
    }
    const points = detail?.series?.current || [];
    if (!points.length) {
      loadTestInterval.innerHTML = '<option value="">No meter intervals</option>';
      updateLoadTest();
      return;
    }

    const previousValue = loadTestInterval.value;
    const peakPoint = points.reduce((best, point) => (!best || Number(point.kw) > Number(best.kw) ? point : best), null);
    loadTestInterval.innerHTML = points
      .map(
        (point) =>
          `<option value="${point.minute}">${escapeHtml(point.label)} | ${escapeHtml(formatNumber(point.kw, " kW"))}</option>`
      )
      .join("");
    const nextValue = points.some((point) => String(point.minute) === previousValue)
      ? previousValue
      : peakPoint
        ? String(peakPoint.minute)
        : String(points[0].minute);
    loadTestInterval.value = nextValue;
    updateLoadTest();
  }

  function renderChart(detail) {
    const seriesList = [
      { points: detail.series.current, className: "chart-series-current", label: detail.current_day.label },
      { points: detail.series.previous, className: "chart-series-previous", label: detail.previous_day?.label },
      { points: detail.series.baseline, className: "chart-series-baseline", label: detail.baseline_day?.label },
    ].filter((series) => series.points.length);

    if (!seriesList.length) {
      chartTarget.innerHTML = '<div class="chart-empty">Pick a day to see the meter curve.</div>';
      return;
    }

    const width = 920;
    const height = 320;
    const padding = { top: 18, right: 18, bottom: 34, left: 46 };
    const allPoints = seriesList.flatMap((series) => series.points);
    const maxKw = Math.max(1, ...allPoints.map((point) => Number(point.kw)));
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const x = (minute) => padding.left + (minute / 1440) * plotWidth;
    const y = (kw) => padding.top + plotHeight - (Number(kw) / maxKw) * plotHeight;

    const gridLines = [];
    for (let step = 0; step <= 4; step += 1) {
      const value = (maxKw / 4) * step;
      const yPos = y(value);
      gridLines.push(`<line class="chart-grid-line" x1="${padding.left}" y1="${yPos}" x2="${width - padding.right}" y2="${yPos}"></line>`);
      gridLines.push(
        `<text class="chart-axis-label" x="${padding.left - 8}" y="${yPos + 4}" text-anchor="end">${value.toFixed(1)}</text>`
      );
    }

    const xLabels = [
      { minute: 0, label: "12a" },
      { minute: 360, label: "6a" },
      { minute: 720, label: "12p" },
      { minute: 1080, label: "6p" },
      { minute: 1440, label: "12a" },
    ]
      .map(
        (mark) =>
          `<text class="chart-axis-label" x="${x(mark.minute)}" y="${height - 8}" text-anchor="${mark.minute === 1440 ? "end" : mark.minute === 0 ? "start" : "middle"}">${mark.label}</text>`
      )
      .join("");

    const lines = seriesList
      .map((series) => {
        const polyline = series.points.map((point) => `${x(point.minute)},${y(point.kw)}`).join(" ");
        return `<polyline class="chart-series-line ${series.className}" points="${polyline}"></polyline>`;
      })
      .join("");

    chartTarget.innerHTML = `
      <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Meter load chart">
        ${gridLines.join("")}
        ${lines}
        ${xLabels}
      </svg>
    `;
  }

  function renderMetrics(detail) {
    const cards = [
      {
        label: "Total use that day",
        value: formatNumber(detail.current_day.total_kwh, " kWh"),
        note: detail.current_day.reasons || "No alert rule fired.",
      },
      {
        label: "Night average",
        value: formatNumber(detail.current_day.night_avg_kw, " kW"),
        note: detail.baseline_kw === null ? "No reference yet." : `Reference night average is ${formatNumber(detail.baseline_kw, " kW")}.`,
      },
      {
        label: "Peak reading",
        value: formatNumber(detail.current_day.max_kw, " kW"),
        note:
          detail.inventory_alignment?.all_on_gap_kw === null
            ? "Add your load list to compare this against the all-on check."
            : `${formatSigned(detail.inventory_alignment.all_on_gap_kw, " kW")} against the all-on check.`,
      },
      {
        label: "Mostly-off check",
        value: formatNumber(detail.load_summary?.off_kw, " kW"),
        note:
          detail.inventory_alignment?.off_gap_kw === null
            ? "Add overnight loads or wait for a night average."
            : `${formatSigned(detail.inventory_alignment.off_gap_kw, " kW")} against the mostly-off check.`,
      },
    ];

    metricsTarget.innerHTML = cards
      .map(
        (card) => `
          <article class="detail-card">
            <span>${escapeHtml(card.label)}</span>
            <strong>${escapeHtml(card.value)}</strong>
            <p>${escapeHtml(card.note)}</p>
          </article>
        `
      )
      .join("");
  }

  function renderComparison(detail) {
    const rows = [];
    if (detail.previous_day && detail.vs_previous_day) {
      rows.push({
        title: `Versus ${detail.previous_day.label}`,
        body: `Total ${formatSigned(detail.vs_previous_day.total_kwh, " kWh")}, night average ${formatSigned(
          detail.vs_previous_day.night_avg_kw,
          " kW"
        )}, peak ${formatSigned(detail.vs_previous_day.max_kw, " kW")}.`,
      });
    }
    if (detail.baseline_day && detail.vs_baseline_day) {
      rows.push({
        title: `Versus ${detail.baseline_day.label}`,
        body: `Total ${formatSigned(detail.vs_baseline_day.total_kwh, " kWh")}, night average ${formatSigned(
          detail.vs_baseline_day.night_avg_kw,
          " kW"
        )}, peak ${formatSigned(detail.vs_baseline_day.max_kw, " kW")}.`,
      });
    }
    if (detail.load_summary) {
      rows.push({
        title: "House load list",
        body: `Everything on adds up to ${formatNumber(detail.load_summary.all_on_kw, " kW")}. Mostly off adds up to ${formatNumber(
          detail.load_summary.off_kw,
          " kW"
        )}.`,
      });
    }

    if (!rows.length) {
      comparisonTarget.innerHTML = '<div class="empty-note">Save a reference day or add a load list to get side-by-side comparisons here.</div>';
      return;
    }

    comparisonTarget.innerHTML = rows
      .map(
        (row) => `
          <article class="detail-list-item">
            <span>${escapeHtml(row.title)}</span>
            <strong>${escapeHtml(row.body)}</strong>
          </article>
        `
      )
      .join("");
  }

  function renderSpikes(detail) {
    const rows = [];
    (detail.top_jumps || []).forEach((jump) => {
      rows.push({
        title: `${jump.time}`,
        body: `${formatNumber(jump.kw, " kW")} after a ${formatNumber(jump.delta_kw, " kW")} jump.`,
      });
    });
    (detail.alert_events || []).forEach((event) => {
      rows.push({
        title: event.timestamp,
        body: `${formatNumber(event.kw, " kW")}. ${event.reasons}`,
      });
    });

    if (!rows.length) {
      spikesTarget.innerHTML = '<div class="empty-note">No sharp jump stood out on this day with the current rules.</div>';
      return;
    }

    spikesTarget.innerHTML = rows
      .slice(0, 8)
      .map(
        (row) => `
          <article class="detail-list-item">
            <span>${escapeHtml(row.title)}</span>
            <strong>${escapeHtml(row.body)}</strong>
          </article>
        `
      )
      .join("");
  }

  function renderWeather(detail) {
    const weather = detail.weather;
    if (!weatherTarget) {
      return;
    }
    if (!weather || !weather.available) {
      weatherTarget.innerHTML = `<div class="empty-note">${escapeHtml(weather?.reason || "Weather is not available for this day yet.")}</div>`;
      return;
    }

    const summary = weather.summary || {};
    const summaryCards = [
      { label: "Location", value: weather.location_name || "Weather" },
      { label: "High / low", value: `${formatNumber(summary.high_temp_f, " F")} / ${formatNumber(summary.low_temp_f, " F")}` },
      { label: "Rain", value: formatNumber(summary.precipitation_in, " in") },
      { label: "Wind", value: formatNumber(summary.max_wind_mph, " mph") },
      { label: "Feel", value: formatNumber(summary.high_apparent_f, " F") },
      { label: "Conditions", value: summary.conditions || "Weather" },
    ];

    const hourlyRows = (weather.hourly || [])
      .map(
        (row) => `
          <tr>
            <td>${escapeHtml(row.hour)}</td>
            <td>${escapeHtml(formatNumber(row.temperature_f, " F"))}</td>
            <td>${escapeHtml(formatNumber(row.apparent_temperature_f, " F"))}</td>
            <td>${escapeHtml(formatNumber(row.precipitation_in, " in"))}</td>
            <td>${escapeHtml(row.weather_label)}</td>
          </tr>
        `
      )
      .join("");

    weatherTarget.innerHTML = `
      <div class="detail-list weather-summary">
        ${summaryCards
          .map(
            (card) => `
              <article class="detail-list-item">
                <span>${escapeHtml(card.label)}</span>
                <strong>${escapeHtml(card.value)}</strong>
              </article>
            `
          )
          .join("")}
      </div>
      <div class="weather-table-wrap">
        <table class="weather-table">
          <thead>
            <tr>
              <th>Hour</th>
              <th>Temp</th>
              <th>Feels</th>
              <th>Rain</th>
              <th>Sky</th>
            </tr>
          </thead>
          <tbody>${hourlyRows}</tbody>
        </table>
      </div>
    `;
  }

  function renderDetail(detail) {
    currentDetail = detail;
    if (!detail || !detail.current_day) {
      headingTarget.textContent = "Choose a flagged day";
      subheadingTarget.textContent = "Click any day below to load the curve and the comparisons.";
      metricsTarget.innerHTML = "";
      comparisonTarget.innerHTML = "";
      spikesTarget.innerHTML = "";
      if (weatherTarget) {
        weatherTarget.innerHTML = "";
      }
      chartTarget.innerHTML = '<div class="chart-empty">Pick a day to see the meter curve.</div>';
      legendTarget.innerHTML = "";
      populateLoadTestIntervals(null);
      return;
    }

    headingTarget.textContent = detail.label;
    subheadingTarget.textContent = detail.current_day.reasons || "This day is in view for closer review.";
    renderLegend(detail);
    renderChart(detail);
    renderMetrics(detail);
    renderComparison(detail);
    renderSpikes(detail);
    renderWeather(detail);
    populateLoadTestIntervals(detail);
    setActiveRow(detail.date);
  }

  async function loadDetail(date) {
    const params = new URLSearchParams({ ...settings, date });
    const response = await fetch(`${root.dataset.apiUrl}?${params.toString()}`, {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error("Unable to load that day.");
    }
    return response.json();
  }

  dayButtons.forEach((button) => {
    button.addEventListener("click", async () => {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "Loading...";
      try {
        const detail = await loadDetail(button.dataset.date);
        renderDetail(detail);
      } catch (error) {
        subheadingTarget.textContent = error.message;
      } finally {
        button.disabled = false;
        button.textContent = original;
      }
    });
  });

  if (loadTestInterval) {
    loadTestInterval.addEventListener("change", updateLoadTest);
  }

  loadTestInputs.forEach((input) => {
    input.addEventListener("input", updateLoadTest);
  });

  if (loadTestAllOn) {
    loadTestAllOn.addEventListener("click", () => {
      loadTestInputs.forEach((input) => {
        input.value = input.dataset.quantity || "0";
      });
      updateLoadTest();
    });
  }

  if (loadTestClear) {
    loadTestClear.addEventListener("click", () => {
      loadTestInputs.forEach((input) => {
        input.value = "0";
      });
      updateLoadTest();
    });
  }

  let initialDetail = null;
  if (initialEl?.textContent) {
    try {
      initialDetail = JSON.parse(initialEl.textContent);
    } catch (error) {
      initialDetail = null;
    }
  }

  renderDetail(initialDetail);
  updateLoadTest();
})();
