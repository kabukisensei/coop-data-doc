/*
 * nav-collapse.js — desktop toggle to collapse the left navigation sidebar.
 *
 * First-party, dependency-free. Wide relationship grids and lineage tables
 * benefit from every rem of width, so this adds a small fixed button that
 * hides the primary sidebar and hands its width to the content column. The
 * choice persists per browser via localStorage (works over file://; a blocked
 * localStorage — e.g. hardened privacy modes — degrades to per-page toggling).
 * The matching CSS lives in custom.css (.nav-collapse-toggle / .nav-collapsed)
 * and only applies at Material's desktop breakpoint; below it the sidebar is
 * already behind the hamburger and the button hides itself.
 */
(function () {
  "use strict";

  var KEY = "coop-data-doc-nav-collapsed";

  function stored() {
    try {
      return window.localStorage.getItem(KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function store(collapsed) {
    try {
      window.localStorage.setItem(KEY, collapsed ? "1" : "0");
    } catch (e) {
      /* per-page toggling still works */
    }
  }

  function render(button, collapsed) {
    document.body.classList.toggle("nav-collapsed", collapsed);
    // ‹ points at the sidebar it will hide; › offers to bring it back
    button.textContent = collapsed ? "›" : "‹";
    button.setAttribute("aria-expanded", collapsed ? "false" : "true");
    button.title = collapsed ? "Show the navigation menu" : "Hide the navigation menu (more room for wide grids)";
  }

  function init() {
    if (!document.querySelector(".md-sidebar--primary")) return;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "nav-collapse-toggle";
    button.setAttribute("aria-label", "Toggle the navigation menu");
    var collapsed = stored();
    render(button, collapsed);
    button.addEventListener("click", function () {
      collapsed = !collapsed;
      store(collapsed);
      render(button, collapsed);
    });
    document.body.appendChild(button);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
