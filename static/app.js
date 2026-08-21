(function () {
  function setupUtilityLookups() {
    document.querySelectorAll("[data-utility-lookup]").forEach((form) => {
      const zipInput = form.querySelector("[data-utility-zip]");
      const addressInput = form.querySelector("[data-utility-address]");
      const matchOutput = form.querySelector("[data-utility-match]");
      const apiUrl = form.dataset.utilityUrl;
      if (!zipInput || !matchOutput || !apiUrl) {
        return;
      }

      let timer = null;
      let activeRequest = null;

      async function findUtility() {
        const zipCode = zipInput.value.trim();
        if (!/^\d{5}(?:-\d{4})?$/.test(zipCode)) {
          matchOutput.textContent = "Enter the service ZIP code and address.";
          return;
        }

        if (activeRequest) {
          activeRequest.abort();
        }
        activeRequest = new AbortController();
        matchOutput.textContent = "Finding the electric company...";

        const params = new URLSearchParams({ zip_code: zipCode });
        const address = addressInput ? addressInput.value.trim() : "";
        if (address) {
          params.set("address", address);
        }

        try {
          const response = await fetch(`${apiUrl}?${params.toString()}`, {
            headers: { Accept: "application/json" },
            signal: activeRequest.signal,
          });
          const result = await response.json();
          if (!response.ok) {
            throw new Error(result.error || "We could not identify the electric company.");
          }
          matchOutput.textContent = result.energy_company || "We could not identify the electric company.";
        } catch (error) {
          if (error.name === "AbortError") {
            return;
          }
          matchOutput.textContent = error.message;
        }
      }

      function scheduleLookup() {
        window.clearTimeout(timer);
        timer = window.setTimeout(findUtility, 450);
      }

      zipInput.addEventListener("input", scheduleLookup);
      if (addressInput) {
        addressInput.addEventListener("input", scheduleLookup);
      }
      if (/^\d{5}(?:-\d{4})?$/.test(zipInput.value.trim())) {
        scheduleLookup();
      }
    });
  }

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

  function setupLoadAutocomplete() {
    document.querySelectorAll("[data-load-autocomplete]").forEach((form) => {
      if (form.dataset.loadAutocompleteReady === "true") {
        return;
      }
      form.dataset.loadAutocompleteReady = "true";

      const labelInput = form.querySelector("[data-load-label]");
      const wattsInput = form.querySelector("[data-load-watts]");
      const alwaysOnInput = form.querySelector("[data-load-always-on]");
      const options = Array.from(form.querySelectorAll("datalist option"));
      if (!labelInput || !wattsInput || !options.length) {
        return;
      }

      let suggestedWatts = "";
      labelInput.addEventListener("input", () => {
        const normalizedLabel = labelInput.value.trim().toLowerCase();
        const match = options.find((option) => option.value.trim().toLowerCase() === normalizedLabel);
        if (!match || !match.dataset.watts) {
          return;
        }

        if (
          !wattsInput.value ||
          wattsInput.dataset.autofilled === "true" ||
          wattsInput.value === suggestedWatts
        ) {
          wattsInput.value = match.dataset.watts;
          wattsInput.dataset.autofilled = "true";
          suggestedWatts = match.dataset.watts;
        }

        if (alwaysOnInput && alwaysOnInput.dataset.userChanged !== "true") {
          alwaysOnInput.checked = match.dataset.alwaysOn === "true";
        }
      });

      wattsInput.addEventListener("input", () => {
        wattsInput.dataset.autofilled = "false";
      });

      if (alwaysOnInput) {
        alwaysOnInput.addEventListener("change", () => {
          alwaysOnInput.dataset.userChanged = "true";
        });
      }
    });
  }

  function bufferToBase64Url(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    bytes.forEach((byte) => {
      binary += String.fromCharCode(byte);
    });
    return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function base64UrlToBuffer(value) {
    const normalized = String(value || "").replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
    const binary = window.atob(padded);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index);
    }
    return bytes.buffer;
  }

  function normalizeWebAuthnRequest(value) {
    if (Array.isArray(value)) {
      return value.map((item) => normalizeWebAuthnRequest(item));
    }
    if (value && typeof value === "object") {
      const normalized = {};
      Object.entries(value).forEach(([key, item]) => {
        if (key === "challenge") {
          normalized[key] = base64UrlToBuffer(item);
          return;
        }
        if ((key === "allowCredentials" || key === "excludeCredentials") && Array.isArray(item)) {
          normalized[key] = item.map((credential) => ({
            ...credential,
            id: base64UrlToBuffer(credential.id),
          }));
          return;
        }
        if (key === "user" && item && typeof item === "object") {
          normalized[key] = {
            ...item,
            id: base64UrlToBuffer(item.id),
          };
          return;
        }
        normalized[key] = normalizeWebAuthnRequest(item);
      });
      return normalized;
    }
    return value;
  }

  function credentialToJSON(credential) {
    const response = credential.response || {};
    const payload = {
      id: credential.id,
      rawId: bufferToBase64Url(credential.rawId),
      type: credential.type,
      response: {},
      clientExtensionResults: credential.getClientExtensionResults ? credential.getClientExtensionResults() : {},
    };

    if (response.clientDataJSON) {
      payload.response.clientDataJSON = bufferToBase64Url(response.clientDataJSON);
    }
    if (response.attestationObject) {
      payload.response.attestationObject = bufferToBase64Url(response.attestationObject);
    }
    if (response.authenticatorData) {
      payload.response.authenticatorData = bufferToBase64Url(response.authenticatorData);
    }
    if (response.signature) {
      payload.response.signature = bufferToBase64Url(response.signature);
    }
    if (response.userHandle) {
      payload.response.userHandle = bufferToBase64Url(response.userHandle);
    }

    return payload;
  }

  function csrfTokenForForm(form) {
    return form.querySelector('input[name="_csrf_token"]')?.value || "";
  }

  function passkeyStatus(form) {
    return form.querySelector("[data-passkey-status]");
  }

  async function handlePasskeyLogin(form) {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error("This browser does not support passkeys.");
    }

    const startUrl = form.dataset.passkeyStartUrl;
    const finishUrl = form.dataset.passkeyFinishUrl;
    const status = passkeyStatus(form);
    const emailInput = form.querySelector('input[name="email"]');
    const nextInput = form.querySelector('input[name="next"]');
    const csrfToken = csrfTokenForForm(form);
    const email = emailInput ? emailInput.value.trim() : "";
    const next = nextInput ? nextInput.value : "";

    if (!email) {
      throw new Error("Enter your email first.");
    }

    if (status) {
      status.textContent = "Waiting for your passkey...";
    }

    const startResponse = await fetch(startUrl, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRF-Token": csrfToken,
      },
      body: new URLSearchParams({ email, next }),
    });
    const startPayload = await startResponse.json();
    if (!startResponse.ok) {
      throw new Error(startPayload.error || "Passkey sign-in could not start.");
    }

    const publicKey = normalizeWebAuthnRequest(startPayload.publicKey || {});
    const credential = await navigator.credentials.get({ publicKey });
    if (!credential) {
      throw new Error("Passkey sign-in was cancelled.");
    }

    const finishResponse = await fetch(finishUrl, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(credentialToJSON(credential)),
    });
    const finishPayload = await finishResponse.json();
    if (!finishResponse.ok) {
      throw new Error(finishPayload.error || "Passkey sign-in could not finish.");
    }

    window.location.assign(finishPayload.redirect || "/");
  }

  async function handlePasskeyEnrollment(form) {
    if (!window.PublicKeyCredential || !navigator.credentials) {
      throw new Error("This browser does not support passkeys.");
    }

    const startUrl = form.action;
    const finishUrl = form.dataset.passkeyFinishUrl;
    const status = passkeyStatus(form);
    const nicknameInput = form.querySelector('input[name="nickname"]');
    const csrfToken = csrfTokenForForm(form);
    const nickname = nicknameInput ? nicknameInput.value.trim() : "";

    if (status) {
      status.textContent = "Creating your passkey...";
    }

    const startResponse = await fetch(startUrl, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "X-CSRF-Token": csrfToken,
      },
      body: new URLSearchParams({ nickname }),
    });
    const startPayload = await startResponse.json();
    if (!startResponse.ok) {
      throw new Error(startPayload.error || "Passkey setup could not start.");
    }

    const publicKey = normalizeWebAuthnRequest(startPayload.publicKey || {});
    const credential = await navigator.credentials.create({ publicKey });
    if (!credential) {
      throw new Error("Passkey setup was cancelled.");
    }

    const finishResponse = await fetch(finishUrl, {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify(credentialToJSON(credential)),
    });
    const finishPayload = await finishResponse.json();
    if (!finishResponse.ok) {
      throw new Error(finishPayload.error || "Passkey setup could not finish.");
    }

    if (status) {
      status.textContent = "Passkey saved.";
    }
    window.location.reload();
  }

  function setupPasskeys() {
    document.querySelectorAll("[data-passkey-login]").forEach((form) => {
      if (form.dataset.passkeyReady === "true") {
        return;
      }
      form.dataset.passkeyReady = "true";
      const button = form.querySelector("[data-passkey-login-button]");
      const status = passkeyStatus(form);
      if (!button) {
        return;
      }
      button.addEventListener("click", async () => {
        const original = button.textContent;
        button.disabled = true;
        if (status) {
          status.textContent = "Waiting for your passkey...";
        }
        try {
          await handlePasskeyLogin(form);
        } catch (error) {
          if (status) {
            status.textContent = error.message;
          }
        } finally {
          button.disabled = false;
          button.textContent = original;
        }
      });
    });

    document.querySelectorAll("[data-passkey-enroll]").forEach((form) => {
      if (form.dataset.passkeyReady === "true") {
        return;
      }
      form.dataset.passkeyReady = "true";
      const button = form.querySelector("[data-passkey-enroll-button]");
      const status = passkeyStatus(form);
      if (!button) {
        return;
      }
      button.addEventListener("click", async () => {
        const original = button.textContent;
        button.disabled = true;
        if (status) {
          status.textContent = "Creating your passkey...";
        }
        try {
          await handlePasskeyEnrollment(form);
        } catch (error) {
          if (status) {
            status.textContent = error.message;
          }
        } finally {
          button.disabled = false;
          button.textContent = original;
        }
      });
    });
  }

  function setupNoteTemplates() {
    document.querySelectorAll("[data-note-template-button]").forEach((button) => {
      if (button.dataset.noteTemplateReady === "true") {
        return;
      }
      button.dataset.noteTemplateReady = "true";
      button.addEventListener("click", () => {
        const form = button.closest("form");
        const textarea = form ? form.querySelector('textarea[name="body"]') : null;
        const template = button.dataset.noteTemplate || "";
        if (!textarea || !template) {
          return;
        }
        textarea.value = textarea.value.trim() ? `${textarea.value.trim()}\n\n${template}` : template;
        textarea.focus();
        const end = textarea.value.length;
        textarea.setSelectionRange(end, end);
      });
    });
  }

  setupUtilityLookups();
  setupPasswordToggles();
  setupLoadAutocomplete();
  setupPasskeys();
  setupNoteTemplates();

  const root = document.getElementById("day-detail-root");
  if (!root) {
    return;
  }

  const metricsTarget = document.getElementById("detail-metrics");
  const comparisonTarget = document.getElementById("detail-comparison");
  const spikesTarget = document.getElementById("detail-spikes");
  const weatherTarget = document.getElementById("detail-weather");
  const weatherColumn = document.getElementById("detail-weather-column");
  const chartTarget = document.getElementById("detail-chart");
  const legendTarget = document.getElementById("detail-legend");
  const headingTarget = document.getElementById("detail-heading");
  const subheadingTarget = document.getElementById("detail-subheading");
  const dayButtons = Array.from(document.querySelectorAll(".day-select"));
  const dayRows = Array.from(document.querySelectorAll(".day-row"));
  const dayTableBody = document.querySelector("[data-day-table-body]");
  const dayFilterButtons = Array.from(document.querySelectorAll("[data-day-filter]"));
  const daySortSelect = document.getElementById("day-sort");
  const initialEl = document.getElementById("initial-day-detail");
  const notesEl = document.getElementById("analysis-notes-data");
  const noteModal = document.getElementById("note-modal");
  const noteModalTitle = document.getElementById("note-modal-title");
  const noteModalSubtitle = document.getElementById("note-modal-subtitle");
  const noteModalContent = document.getElementById("note-modal-content");
  const noteModalClose = document.getElementById("note-modal-close");
  const detailNoteButton = document.getElementById("detail-note-button");
  const detailNoteIndicator = document.getElementById("detail-note-indicator");
  const loadTestInterval = document.getElementById("load-test-interval");
  const loadTestExpected = document.getElementById("load-test-expected");
  const loadTestActual = document.getElementById("load-test-actual");
  const loadTestDifference = document.getElementById("load-test-difference");
  const loadTestStatus = document.getElementById("load-test-status");
  const loadTestAllOn = document.getElementById("load-test-all-on");
  const loadTestClear = document.getElementById("load-test-clear");
  const loadTestInputs = Array.from(document.querySelectorAll(".load-test-count"));
  let currentDetail = null;
  let activeDayFilter = "all";

  const settings = {
    account_number: root.dataset.accountNumber,
    tz: root.dataset.tz,
    night_start: root.dataset.nightStart,
    night_end: root.dataset.nightEnd,
    min_night_kw: root.dataset.minNightKw,
    night_multiplier: root.dataset.nightMultiplier,
  };
  let accountNotes = [];
  if (notesEl?.textContent) {
    try {
      accountNotes = JSON.parse(notesEl.textContent);
    } catch (error) {
      accountNotes = [];
    }
  }

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

  function describeChange(value, suffix) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "not available";
    }
    const numeric = Number(value);
    if (Math.abs(numeric) < 0.0005) {
      return `no change${suffix || ""}`;
    }
    const direction = numeric > 0 ? "higher" : "lower";
    return `${formatNumber(Math.abs(numeric), suffix)} ${direction}`;
  }

  function describeGap(value, subject) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return `Add the ${subject} to compare it.`;
    }
    const numeric = Number(value);
    if (Math.abs(numeric) < 0.0005) {
      return `Matches the ${subject}.`;
    }
    const direction = numeric > 0 ? "above" : "below";
    return `${formatNumber(Math.abs(numeric), " kW")} ${direction} the ${subject}.`;
  }

  function notesForDate(date) {
    return accountNotes.filter((note) => note.note_date === date);
  }

  function closeNotesModal() {
    if (!noteModal) {
      return;
    }
    noteModal.hidden = true;
    document.body.classList.remove("modal-open");
  }

  function openNotesModal(date) {
    if (!noteModal || !noteModalTitle || !noteModalSubtitle || !noteModalContent) {
      return;
    }
    const notes = notesForDate(date);
    noteModalTitle.textContent = `Notes for ${date}`;
    noteModalSubtitle.textContent = notes.length
      ? `${notes.length} note${notes.length === 1 ? "" : "s"} recorded for this day.`
      : "No saved notes for this day yet.";
    noteModalContent.innerHTML = notes.length
      ? notes
          .map(
            (note) => `
              <article class="modal-note-card">
                <div class="modal-note-meta">
                  <strong>${escapeHtml(note.note_date)}</strong>
                  <span>${escapeHtml(note.author_label || "Homeowner")}${note.created_at ? ` · ${escapeHtml(String(note.created_at).replace("T", " "))}` : ""}</span>
                </div>
                <div class="modal-note-body">${escapeHtml(note.body || "").replace(/\n/g, "<br>")}</div>
              </article>
            `
          )
          .join("")
      : '<div class="empty-note">No dated note is saved for this day yet.</div>';
    noteModal.hidden = false;
    document.body.classList.add("modal-open");
  }

  function updateDetailNoteState(detail) {
    if (!detailNoteButton || !detailNoteIndicator) {
      return;
    }
    const noteCount = Number(detail?.note_count || 0);
    if (!detail || !detail.date || noteCount <= 0) {
      detailNoteButton.hidden = true;
      detailNoteButton.removeAttribute("data-note-date");
      detailNoteIndicator.hidden = true;
      detailNoteIndicator.textContent = "";
      return;
    }
    detailNoteButton.hidden = false;
    detailNoteButton.dataset.noteDate = detail.date;
    detailNoteIndicator.hidden = false;
    detailNoteIndicator.textContent = `${noteCount} note${noteCount === 1 ? "" : "s"}`;
  }

  function sortRows(rows, mode) {
    const byNumber = (row, key, fallback = -1) => {
      const raw = row.dataset[key];
      const numeric = Number(raw);
      return Number.isFinite(numeric) ? numeric : fallback;
    };

    const sorted = [...rows];
    sorted.sort((left, right) => {
      switch (mode) {
        case "date_asc":
          return String(left.dataset.date).localeCompare(String(right.dataset.date));
        case "date_desc":
          return String(right.dataset.date).localeCompare(String(left.dataset.date));
        case "total_desc":
          return byNumber(right, "total") - byNumber(left, "total");
        case "night_desc":
          return byNumber(right, "night") - byNumber(left, "night");
        case "peak_desc":
          return byNumber(right, "peak") - byNumber(left, "peak");
        case "alerts_desc":
          return byNumber(right, "alerts") - byNumber(left, "alerts");
        case "notes_desc":
          return byNumber(right, "noteCount") - byNumber(left, "noteCount");
        case "severity_desc":
        default: {
          const severity = byNumber(right, "severity") - byNumber(left, "severity");
          if (severity !== 0) {
            return severity;
          }
          const suspicious = byNumber(right, "suspicious") - byNumber(left, "suspicious");
          if (suspicious !== 0) {
            return suspicious;
          }
          return String(right.dataset.date).localeCompare(String(left.dataset.date));
        }
      }
    });
    return sorted;
  }

  function applyDayExplorerState() {
    if (!dayTableBody || !dayRows.length) {
      return;
    }
    const filteredRows = dayRows.filter((row) => {
      if (activeDayFilter === "flagged") {
        return row.dataset.suspicious === "1";
      }
      if (activeDayFilter === "notes") {
        return Number(row.dataset.noteCount || 0) > 0;
      }
      if (activeDayFilter === "quiet") {
        return row.dataset.suspicious === "0";
      }
      return true;
    });

    dayRows.forEach((row) => {
      row.hidden = !filteredRows.includes(row);
    });

    const sortedRows = sortRows(filteredRows, daySortSelect?.value || "severity_desc");
    sortedRows.forEach((row) => {
      dayTableBody.appendChild(row);
    });
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
        label: "Total use",
        value: formatNumber(detail.current_day.total_kwh, " kWh"),
        // The full rule text is already shown once, in the subheading above.
        note: detail.current_day.suspicious ? "Flagged for review." : "No rule fired.",
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
            ? "Add your load list to compare this against the listed-load estimate."
            : describeGap(detail.inventory_alignment.all_on_gap_kw, "listed-load estimate"),
      },
      {
        label: "Mostly-off check",
        value: formatNumber(detail.load_summary?.off_kw, " kW"),
        note:
          detail.inventory_alignment?.off_gap_kw === null
            ? "Mark any always-on loads in your inventory first."
            : describeGap(detail.inventory_alignment.off_gap_kw, "always-on estimate"),
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
        body: `Total use was ${describeChange(detail.vs_previous_day.total_kwh, " kWh")}; night average was ${describeChange(
          detail.vs_previous_day.night_avg_kw,
          " kW"
        )}; peak was ${describeChange(detail.vs_previous_day.max_kw, " kW")}.`,
      });
    }
    if (detail.baseline_day && detail.vs_baseline_day) {
      rows.push({
        title: `Versus ${detail.baseline_day.label}`,
        body: `Total use was ${describeChange(detail.vs_baseline_day.total_kwh, " kWh")}; night average was ${describeChange(
          detail.vs_baseline_day.night_avg_kw,
          " kW"
        )}; peak was ${describeChange(detail.vs_baseline_day.max_kw, " kW")}.`,
      });
    }
    if (detail.load_summary) {
      rows.push({
        title: "House load list",
        body: `Listed loads at once: ${formatNumber(detail.load_summary.all_on_watts, " W")}. Marked always-on loads: ${formatNumber(
          detail.load_summary.off_watts,
          " W"
        )}. This is the inventory estimate, not the meter reading.`,
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
    const alertRows = (detail.alert_events || [])
      .filter((event) => event.is_spike && event.previous_kw !== null && event.previous_kw !== undefined)
      .map((event) => ({
        sortKey: event.timestamp,
        from: event.previous_timestamp_full || event.previous_timestamp_label || "Earlier interval",
        fromKw: event.previous_kw,
        to: event.timestamp_label || event.timestamp,
        toKw: event.kw,
        change: event.delta_kw,
      }));
    const jumpRows = (detail.top_jumps || [])
      .filter((jump) => jump.previous_kw !== null && jump.previous_kw !== undefined)
      .map((jump) => ({
        sortKey: jump.timestamp || jump.time,
        from: jump.previous_timestamp_full || jump.previous_time || "Earlier interval",
        fromKw: jump.previous_kw,
        to: jump.timestamp_label || jump.time,
        toKw: jump.kw,
        change: jump.delta_kw,
      }));
    const rows = (alertRows.length ? alertRows : jumpRows)
      .sort((left, right) => String(left.sortKey).localeCompare(String(right.sortKey)))
      .slice(0, 10);

    if (!rows.length) {
      spikesTarget.innerHTML = '<div class="empty-note">No interval jump stood out on this day with the current rules.</div>';
      return;
    }

    spikesTarget.innerHTML = `
      <div class="table-wrap spike-table-wrap">
        <table class="spike-table">
          <thead>
            <tr>
              <th>From</th>
              <th>From kW</th>
              <th>To</th>
              <th>To kW</th>
              <th>Change</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            ${rows
              .map(
                (row) => `
                  <tr>
                    <td>${escapeHtml(row.from)}</td>
                    <td>${escapeHtml(formatNumber(row.fromKw, " kW"))}</td>
                    <td>${escapeHtml(row.to)}</td>
                    <td>${escapeHtml(formatNumber(row.toKw, " kW"))}</td>
                    <td>${escapeHtml(formatSigned(row.change, " kW"))}</td>
                    <td><span class="pill danger">Spike</span></td>
                  </tr>
                `
              )
              .join("")}
          </tbody>
        </table>
      </div>
    `;
  }

  function renderWeather(detail) {
    const weather = detail.weather;
    if (!weatherTarget) {
      return;
    }
    if (weatherColumn) {
      weatherColumn.hidden = false;
    }
    if (!weather || !weather.available) {
      if (weather?.pending) {
        weatherTarget.innerHTML = '<div class="empty-note">Weather loads when you open a day.</div>';
      } else {
        weatherTarget.innerHTML = "";
        if (weatherColumn) {
          weatherColumn.hidden = true;
        }
      }
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
      subheadingTarget.textContent = "Click a day to see the curve, comparisons, weather, notes, and load checks.";
      metricsTarget.innerHTML = "";
      comparisonTarget.innerHTML = "";
      spikesTarget.innerHTML = "";
      if (weatherTarget) {
        weatherTarget.innerHTML = "";
      }
      if (weatherColumn) {
        weatherColumn.hidden = true;
      }
      chartTarget.innerHTML = '<div class="chart-empty">Pick a day to see the meter curve.</div>';
      legendTarget.innerHTML = "";
      updateDetailNoteState(null);
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
    updateDetailNoteState(detail);
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

  document.querySelectorAll(".note-marker[data-note-date]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openNotesModal(button.dataset.noteDate);
    });
  });

  if (detailNoteButton) {
    detailNoteButton.addEventListener("click", () => {
      if (detailNoteButton.dataset.noteDate) {
        openNotesModal(detailNoteButton.dataset.noteDate);
      }
    });
  }

  if (noteModalClose) {
    noteModalClose.addEventListener("click", closeNotesModal);
  }

  if (noteModal) {
    noteModal.addEventListener("click", (event) => {
      if (event.target === noteModal) {
        closeNotesModal();
      }
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && noteModal && !noteModal.hidden) {
      closeNotesModal();
    }
  });

  dayFilterButtons.forEach((button) => {
    button.addEventListener("click", () => {
      activeDayFilter = button.dataset.dayFilter || "all";
      dayFilterButtons.forEach((candidate) => {
        candidate.classList.toggle("active", candidate === button);
      });
      applyDayExplorerState();
    });
  });

  if (daySortSelect) {
    daySortSelect.addEventListener("change", applyDayExplorerState);
  }

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
  applyDayExplorerState();
  updateLoadTest();
})();
