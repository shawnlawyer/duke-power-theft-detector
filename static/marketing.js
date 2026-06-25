(function () {
  const patternData = {
    total_kwh: {
      title: "Total kWh",
      summary: "Month over month comparison for the main usage story.",
      subtitle: "Apr 2025 vs May 2025",
      stats: [
        { label: "This month", value: "1,198 kWh" },
        { label: "3-month change", value: "+7%" },
        { label: "What it means", value: "Usage stayed elevated through the end of the period." },
      ],
      note: "Total usage increased from April to May, and the rise stayed broad instead of isolated to one night.",
      earlier: [44, 56, 72, 71, 73, 74],
      later: [49, 63, 88, 75, 71, 88],
      deltas: ["+8%", "+14%", "+22%", "+5%", "-3%", "+18%"],
    },
    baseline_kw: {
      title: "Overnight baseline",
      summary: "Baseline drift across aligned periods.",
      subtitle: "Apr 2025 vs May 2025",
      stats: [
        { label: "Current baseline", value: "1.22 kW" },
        { label: "Year shift", value: "+0.31 kW" },
        { label: "What it means", value: "The meter never really settles back to the earlier overnight floor." },
      ],
      note: "The baseline moved up and stayed there, which matters more than a single spike if the question is persistent overnight load.",
      earlier: [22, 28, 31, 30, 34, 36],
      later: [25, 34, 42, 39, 41, 48],
      deltas: ["+0.08", "+0.12", "+0.19", "+0.11", "+0.07", "+0.12"],
    },
    flagged_nights: {
      title: "Flagged nights",
      summary: "Nights that stayed above the threshold.",
      subtitle: "Apr 2025 vs May 2025",
      stats: [
        { label: "Flagged nights", value: "11" },
        { label: "Month change", value: "+5 nights" },
        { label: "What it means", value: "The elevated nights cluster late in the month instead of fading out." },
      ],
      note: "A handful of nights may be noise. A repeated cluster is usually the faster place to start the follow-up.",
      earlier: [10, 12, 14, 15, 18, 17],
      later: [12, 16, 21, 20, 19, 28],
      deltas: ["+2", "+4", "+7", "+5", "+1", "+11"],
    },
  };

  function setExplorerView(viewKey) {
    const explorer = document.getElementById("pattern-explorer");
    if (!explorer || !patternData[viewKey]) {
      return;
    }

    const view = patternData[viewKey];
    explorer.querySelectorAll("[data-pattern-tab]").forEach((button) => {
      const isActive = button.dataset.patternTab === viewKey;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-selected", isActive ? "true" : "false");
    });

    const title = document.getElementById("pattern-title");
    const summary = document.getElementById("pattern-summary");
    const subtitle = document.getElementById("pattern-subtitle");
    const note = document.getElementById("pattern-note");
    const statOneLabel = document.getElementById("pattern-stat-one-label");
    const statOneValue = document.getElementById("pattern-stat-one-value");
    const statTwoLabel = document.getElementById("pattern-stat-two-label");
    const statTwoValue = document.getElementById("pattern-stat-two-value");
    const statThreeLabel = document.getElementById("pattern-stat-three-label");
    const statThreeValue = document.getElementById("pattern-stat-three-value");

    if (title) title.textContent = view.title;
    if (summary) summary.textContent = view.summary;
    if (subtitle) subtitle.textContent = view.subtitle;
    if (note) note.textContent = view.note;
    if (statOneLabel) statOneLabel.textContent = view.stats[0].label;
    if (statOneValue) statOneValue.textContent = view.stats[0].value;
    if (statTwoLabel) statTwoLabel.textContent = view.stats[1].label;
    if (statTwoValue) statTwoValue.textContent = view.stats[1].value;
    if (statThreeLabel) statThreeLabel.textContent = view.stats[2].label;
    if (statThreeValue) statThreeValue.textContent = view.stats[2].value;

    view.earlier.forEach((value, index) => {
      const earlierBar = explorer.querySelector(`[data-bar-earlier="${index}"]`);
      const laterBar = explorer.querySelector(`[data-bar-later="${index}"]`);
      const delta = explorer.querySelector(`[data-bar-delta="${index}"]`);
      if (earlierBar) earlierBar.style.height = `${value}%`;
      if (laterBar) laterBar.style.height = `${view.later[index]}%`;
      if (delta) {
        delta.textContent = view.deltas[index];
        delta.classList.toggle("negative", String(view.deltas[index]).startsWith("-"));
      }
    });
  }

  function setupPatternExplorer() {
    const explorer = document.getElementById("pattern-explorer");
    if (!explorer) {
      return;
    }

    const defaultView = explorer.dataset.defaultView || "total_kwh";
    explorer.querySelectorAll("[data-pattern-tab]").forEach((button) => {
      button.addEventListener("click", () => setExplorerView(button.dataset.patternTab));
    });
    setExplorerView(defaultView);
  }

  function setFlowStep(stepKey) {
    const flowRoot = document.getElementById("review-flow");
    if (!flowRoot) {
      return;
    }

    flowRoot.querySelectorAll("[data-flow-step]").forEach((button) => {
      const isActive = button.dataset.flowStep === stepKey;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-selected", isActive ? "true" : "false");
    });

    flowRoot.querySelectorAll("[data-flow-panel]").forEach((panel) => {
      const isActive = panel.dataset.flowPanel === stepKey;
      panel.classList.toggle("active", isActive);
      panel.hidden = !isActive;
    });

    flowRoot.querySelectorAll("[data-flow-visual]").forEach((panel) => {
      const isActive = panel.dataset.flowVisual === stepKey;
      panel.classList.toggle("active", isActive);
      panel.hidden = !isActive;
    });
  }

  function setupFlowSection() {
    const flowRoot = document.getElementById("review-flow");
    if (!flowRoot) {
      return;
    }

    const defaultStep = flowRoot.dataset.defaultStep || "inspect";
    flowRoot.querySelectorAll("[data-flow-step]").forEach((button) => {
      button.addEventListener("click", () => setFlowStep(button.dataset.flowStep));
    });
    setFlowStep(defaultStep);
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupPatternExplorer();
    setupFlowSection();
  });
})();
