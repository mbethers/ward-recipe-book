// Tab switching on the submit page (type it in / photo/PDF upload / paste a link)
document.addEventListener("DOMContentLoaded", () => {
  const tabButtons = document.querySelectorAll(".tab-btn");
  const panels = {
    type: document.getElementById("form-type"),
    photo: document.getElementById("form-photo"),
    link: document.getElementById("form-link"),
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
  const messages = {
    "photo-submit-btn": "Reading your photo… (can take up to a minute)",
    "photo-upload-btn": "Uploading your photo…",
    "link-submit-btn": "Reading that page… (can take up to a minute)",
    "text-submit-btn": "Checking it over…",
  };
  btn.textContent = messages[btn.id] || "Please wait…";
});
