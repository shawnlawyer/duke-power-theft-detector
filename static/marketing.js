(function () {
  const header = document.querySelector("[data-hew-header]");
  const toggle = document.querySelector("[data-hew-menu-toggle]");
  if (header && toggle) {
    toggle.addEventListener("click", () => {
      const open = header.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    });
  }
  // Close the Resources menu when clicking elsewhere.
  document.addEventListener("click", (event) => {
    document.querySelectorAll(".hew-nav details[open]").forEach((menu) => {
      if (!menu.contains(event.target)) {
        menu.removeAttribute("open");
      }
    });
  });
})();
