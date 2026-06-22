/*
 * doc-tree.js — collapsible "Upstream lineage" dependency trees.
 *
 * First-party, dependency-free. The trace-to-source tree can be a wall of text
 * (deep d365fo chains), so this turns the nested list under each "## Upstream
 * lineage" heading into a collapsible tree: every node with children starts
 * COLLAPSED with a ▸/▾ toggle to drill down. With no JS the full tree shows, so
 * the Markdown still reads fine.
 */
(function () {
  "use strict";

  function childList(li) {
    for (var i = 0; i < li.children.length; i++) {
      var c = li.children[i];
      if (c.tagName === "UL" || c.tagName === "OL") return c;
    }
    return null;
  }

  function decorate(li) {
    var child = childList(li);
    if (!child) return; // a leaf — nothing to collapse
    li.classList.add("tree-branch", "tree-collapsed");
    child.hidden = true; // default collapsed
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tree-toggle";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-label", "Toggle children");
    toggle.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      var collapsed = li.classList.toggle("tree-collapsed");
      child.hidden = collapsed;
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    });
    li.insertBefore(toggle, li.firstChild);
  }

  function enhance() {
    var heads = document.querySelectorAll(".md-typeset h2");
    for (var i = 0; i < heads.length; i++) {
      var h = heads[i];
      if (h.textContent.trim().toLowerCase().indexOf("upstream lineage") !== 0) continue;
      // the tree is the first list after the heading (skip the intro paragraph)
      var el = h.nextElementSibling;
      while (el && el.tagName !== "UL") {
        if (el.tagName === "H2") {
          el = null;
          break;
        }
        el = el.nextElementSibling;
      }
      if (!el || el.dataset.treeReady === "1") continue;
      el.dataset.treeReady = "1";
      el.classList.add("tree-root");
      var items = el.querySelectorAll("li");
      for (var j = 0; j < items.length; j++) decorate(items[j]);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", enhance);
  } else {
    enhance();
  }
  // Material swaps content via an RxJS subject on navigation — re-run after each.
  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(function () {
      setTimeout(enhance, 0);
    });
  }
})();
