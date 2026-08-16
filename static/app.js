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

// Shows/hides the "new theme" text field when "+ Add a new theme..." is picked.
window.__toggleNewTheme = function (selectEl) {
  const prefix = selectEl.id.replace("_theme_tag", "");
  const wrap = document.getElementById(prefix + "_new_theme_wrap");
  if (!wrap) return;
  wrap.style.display = selectEl.value === "__new__" ? "" : "none";
};
