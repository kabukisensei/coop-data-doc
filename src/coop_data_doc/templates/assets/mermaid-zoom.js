/*
 * mermaid-zoom.js — offline pan/zoom for rendered Mermaid diagrams.
 *
 * First-party, dependency-free. coop-data-doc forbids CDN/network assets, so
 * rather than vendoring svg-pan-zoom this hand-rolls the behaviour: it watches
 * for Mermaid to replace a `.mermaid` block with an <svg>, wraps it in a
 * bounded viewport, and adds drag-to-pan, Ctrl/Cmd+wheel zoom, and a small
 * control bar (zoom in/out, reset, fullscreen). Large estate/lineage diagrams
 * become a pannable canvas instead of an unreadable wall.
 *
 * Plain wheel still scrolls the page — zoom needs a modifier or the buttons —
 * so reading is never hijacked just because the cursor is over a diagram.
 */
(function () {
  "use strict";

  var MIN_SCALE = 0.2;
  var MAX_SCALE = 12;

  function enhance(svg) {
    if (!svg || svg.dataset.zoomReady === "1") return;
    var host = svg.parentElement;
    if (!host) return;
    svg.dataset.zoomReady = "1";
    host.classList.add("mermaid-zoom-host");

    // let the diagram be transformed freely inside the viewport
    svg.style.transformOrigin = "0 0";
    svg.style.cursor = "grab";
    svg.style.maxWidth = "none";

    var state = { scale: 1, x: 0, y: 0 };
    function apply() {
      svg.style.transform =
        "translate(" + state.x + "px," + state.y + "px) scale(" + state.scale + ")";
    }
    function reset() {
      state.scale = 1;
      state.x = 0;
      state.y = 0;
      apply();
    }
    function zoomAt(clientX, clientY, factor) {
      var rect = host.getBoundingClientRect();
      var px = clientX - rect.left;
      var py = clientY - rect.top;
      var next = Math.min(MAX_SCALE, Math.max(MIN_SCALE, state.scale * factor));
      var k = next / state.scale;
      state.x = px - k * (px - state.x); // keep the point under the cursor fixed
      state.y = py - k * (py - state.y);
      state.scale = next;
      apply();
    }
    function zoomCenter(factor) {
      var r = host.getBoundingClientRect();
      zoomAt(r.left + r.width / 2, r.top + r.height / 2, factor);
    }

    // Ctrl/Cmd + wheel zooms; a plain wheel is left alone so the page scrolls.
    host.addEventListener(
      "wheel",
      function (e) {
        if (!(e.ctrlKey || e.metaKey)) return;
        e.preventDefault();
        zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.12 : 1 / 1.12);
      },
      { passive: false }
    );

    var dragging = false;
    var lastX = 0;
    var lastY = 0;
    svg.addEventListener("pointerdown", function (e) {
      dragging = true;
      lastX = e.clientX;
      lastY = e.clientY;
      svg.style.cursor = "grabbing";
      try {
        svg.setPointerCapture(e.pointerId);
      } catch (_) {}
    });
    svg.addEventListener("pointermove", function (e) {
      if (!dragging) return;
      state.x += e.clientX - lastX;
      state.y += e.clientY - lastY;
      lastX = e.clientX;
      lastY = e.clientY;
      apply();
    });
    function endDrag(e) {
      dragging = false;
      svg.style.cursor = "grab";
      try {
        svg.releasePointerCapture(e.pointerId);
      } catch (_) {}
    }
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", endDrag);
    svg.addEventListener("pointerleave", endDrag);
    svg.addEventListener("dblclick", reset);

    var bar = document.createElement("div");
    bar.className = "mermaid-zoom-controls";

    function button(label, title, onClick) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "mermaid-zoom-btn";
      b.textContent = label;
      b.title = title;
      b.setAttribute("aria-label", title);
      b.addEventListener("click", function (e) {
        e.preventDefault();
        onClick();
      });
      bar.appendChild(b);
    }

    button("+", "Zoom in", function () {
      zoomCenter(1.2);
    });
    button("−", "Zoom out", function () {
      zoomCenter(1 / 1.2);
    });
    button("↺", "Reset (or double-click)", reset);
    button("⛶", "Fullscreen", function () {
      // both calls return a Promise that rejects when the browser blocks the
      // request (common over file://) — swallow it so it can't hit the console
      var swallow = function () {};
      if (document.fullscreenElement) {
        var exit = document.exitFullscreen();
        if (exit && exit.catch) exit.catch(swallow);
      } else if (host.requestFullscreen) {
        var enter = host.requestFullscreen();
        if (enter && enter.catch) enter.catch(swallow);
      }
    });

    var hint = document.createElement("span");
    hint.className = "mermaid-zoom-hint";
    hint.textContent = "Ctrl+scroll to zoom · drag to pan";
    bar.appendChild(hint);

    host.appendChild(bar);
  }

  function scan() {
    var svgs = document.querySelectorAll(".mermaid > svg, pre.mermaid svg");
    for (var i = 0; i < svgs.length; i++) enhance(svgs[i]);
  }

  function init() {
    scan();
    if (document.body) {
      var observer = new MutationObserver(scan);
      observer.observe(document.body, { childList: true, subtree: true });
    }
    // Material for MkDocs swaps content via an RxJS subject on navigation.
    if (window.document$ && typeof window.document$.subscribe === "function") {
      window.document$.subscribe(function () {
        setTimeout(scan, 0);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
