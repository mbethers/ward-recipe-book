// Tab switching on the submit page (type vs. photo/PDF upload)
document.addEventListener("DOMContentLoaded", () => {
  const tabButtons = document.querySelectorAll(".tab-btn");
  const panels = {
    type: document.getElementById("form-type"),
    photo: document.getElementById("form-photo"),
  };

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabButtons.forEach((b) => b.classList.remove("tab-active"));
      btn.classList.add("tab-active");
      Object.values(panels).forEach((p) => p && p.classList.remove("tab-panel-active"));
      const target = panels[btn.dataset.tab];
      if (target) target.classList.add("tab-panel-active");
    });
  });
});

// Shows a loading state on whichever button triggered a form submission —
// prevents accidental double-submits and gives feedback during slow requests
// (photo uploads go through AI parsing server-side, which can take a while).
document.addEventListener("submit", (event) => {
  const btn = event.submitter;
  if (!btn || btn.dataset.loading) return;
  btn.dataset.loading = "true";
  btn.disabled = true;
  btn.classList.add("is-loading");
  btn.textContent =
    btn.id === "photo-submit-btn"
      ? "Reading your photo… (can take up to a minute)"
      : "Please wait…";
});

// Shows/hides the "new theme" text field when "+ Add a new theme..." is picked.
window.__toggleNewTheme = function (selectEl) {
  const prefix = selectEl.id.replace("_theme_tag", "");
  const wrap = document.getElementById(prefix + "_new_theme_wrap");
  if (!wrap) return;
  wrap.style.display = selectEl.value === "__new__" ? "" : "none";
};
