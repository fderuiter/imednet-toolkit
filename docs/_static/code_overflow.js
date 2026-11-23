/**
 * Site-wide keyboard accessibility and ARIA label helper for overflowing
 * code blocks (<pre>) and Mermaid diagram containers (.mermaid).
 */
(function () {
  "use strict";

  var codeBlocksRaf = null;

  function checkCodeBlocks() {
    var blocks = document.querySelectorAll("pre");
    blocks.forEach(function (block) {
      var isOverflowing = block.clientWidth > 0 && block.scrollWidth > block.clientWidth;
      if (isOverflowing) {
        block.setAttribute("tabindex", "0");
        block.setAttribute("role", "region");
        if (!block.getAttribute("aria-label")) {
          block.setAttribute("aria-label", "Code snippet");
        }
      } else {
        if (block.hasAttribute("tabindex")) {
          block.removeAttribute("tabindex");
        }
        if (
          block.getAttribute("role") === "region" &&
          block.getAttribute("aria-label") === "Code snippet"
        ) {
          block.removeAttribute("role");
          block.removeAttribute("aria-label");
        }
      }
    });

    var mermaidBlocks = document.querySelectorAll(".mermaid");
    mermaidBlocks.forEach(function (diagram) {
      var svg =
        typeof diagram.querySelector === "function" ? diagram.querySelector("svg") : null;
      var titleText = "";
      if (svg && typeof svg.querySelector === "function") {
        var titleEl = svg.querySelector("title");
        if (titleEl && titleEl.textContent) {
          titleText = titleEl.textContent.trim();
        }
        if (titleText && !svg.getAttribute("aria-label")) {
          svg.setAttribute("aria-label", titleText);
        }
      }
      if (titleText) {
        diagram.setAttribute("data-mermaid-label", titleText);
      }
      var regionLabel =
        titleText || diagram.getAttribute("data-mermaid-label") || "Mermaid diagram";
      var isOverflowing =
        diagram.clientWidth > 0 && diagram.scrollWidth > diagram.clientWidth;
      if (isOverflowing) {
        diagram.setAttribute("tabindex", "0");
        diagram.setAttribute("role", "region");
        if (
          !diagram.getAttribute("aria-label") ||
          diagram.hasAttribute("data-mermaid-region")
        ) {
          diagram.setAttribute("aria-label", regionLabel);
          diagram.setAttribute("data-mermaid-region", "true");
        }
      } else {
        if (diagram.hasAttribute("tabindex")) {
          diagram.removeAttribute("tabindex");
        }
        if (diagram.hasAttribute("data-mermaid-region")) {
          diagram.removeAttribute("role");
          diagram.removeAttribute("aria-label");
          diagram.removeAttribute("data-mermaid-region");
        }
      }
    });
  }

  function scheduleCheckCodeBlocks() {
    if (typeof cancelAnimationFrame === "function" && codeBlocksRaf) {
      cancelAnimationFrame(codeBlocksRaf);
      codeBlocksRaf = null;
    }
    if (typeof requestAnimationFrame === "function") {
      codeBlocksRaf = requestAnimationFrame(function () {
        codeBlocksRaf = null;
        checkCodeBlocks();
      });
    } else {
      checkCodeBlocks();
    }
  }

  function initAccessibleCodeBlocks() {
    if (globalThis.__imednetCodeOverflowBound) {
      checkCodeBlocks();
      return;
    }
    globalThis.__imednetCodeOverflowBound = true;
    globalThis.imednetScheduleCheckCodeBlocks = scheduleCheckCodeBlocks;

    checkCodeBlocks();
    if (typeof window !== "undefined") {
      window.addEventListener("resize", scheduleCheckCodeBlocks);
      window.addEventListener("load", scheduleCheckCodeBlocks);
    }

    if (typeof MutationObserver !== "undefined") {
      var observer = new MutationObserver(function () {
        scheduleCheckCodeBlocks();
      });
      var target = document.body || document.documentElement;
      if (target) {
        observer.observe(target, {
          attributes: true,
          attributeFilter: ["hidden", "class"],
          childList: true,
          subtree: true,
        });
      }
    }
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", initAccessibleCodeBlocks);
    } else {
      initAccessibleCodeBlocks();
    }
  }
})();
