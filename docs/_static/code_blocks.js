/**
 * WCAG 2.1 Level AA keyboard focus management for overflowing <pre> code blocks
 * and Mermaid diagram regions across all documentation pages.
 */
(function () {
    "use strict";

    let codeBlocksRaf = null;

    function checkCodeBlocks() {
        const blocks = document.querySelectorAll("pre");
        blocks.forEach(function (block) {
            const isOverflowing = block.clientWidth > 0 && block.scrollWidth > block.clientWidth;
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
                if (block.getAttribute("role") === "region" && block.getAttribute("aria-label") === "Code snippet") {
                    block.removeAttribute("role");
                    block.removeAttribute("aria-label");
                }
            }
        });

        const mermaidBlocks = document.querySelectorAll(".mermaid");
        mermaidBlocks.forEach(function (diagram) {
            const svg = typeof diagram.querySelector === "function" ? diagram.querySelector("svg") : null;
            let titleText = "";
            if (svg && typeof svg.querySelector === "function") {
                const titleEl = svg.querySelector("title");
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
            const regionLabel = titleText || diagram.getAttribute("data-mermaid-label") || "Mermaid diagram";
            const isOverflowing = diagram.clientWidth > 0 && diagram.scrollWidth > diagram.clientWidth;
            if (isOverflowing) {
                diagram.setAttribute("tabindex", "0");
                diagram.setAttribute("role", "region");
                if (!diagram.getAttribute("aria-label") || diagram.hasAttribute("data-mermaid-region")) {
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

    function initMainContentFocus() {
        const mainContent =
            typeof document.getElementById === "function"
                ? document.getElementById("furo-main-content")
                : null;
        if (
            mainContent &&
            typeof mainContent.hasAttribute === "function" &&
            !mainContent.hasAttribute("tabindex") &&
            typeof mainContent.setAttribute === "function"
        ) {
            mainContent.setAttribute("tabindex", "-1");
        }

        if (typeof document.querySelectorAll !== "function") {
            return;
        }
        const backToTopLinks = document.querySelectorAll("a.back-to-top");
        Array.from(backToTopLinks || []).forEach(function (link) {
            if (
                !link ||
                typeof link.addEventListener !== "function" ||
                (typeof link.hasAttribute === "function" && link.hasAttribute("data-btt-bound"))
            ) {
                return;
            }
            if (typeof link.setAttribute === "function") {
                link.setAttribute("data-btt-bound", "true");
            }
            link.addEventListener("click", function (e) {
                if (e && typeof e.preventDefault === "function") {
                    e.preventDefault();
                }
                const mql =
                    typeof globalThis.matchMedia === "function"
                        ? globalThis.matchMedia("(prefers-reduced-motion: reduce)")
                        : null;
                const prefersReducedMotion = Boolean(mql && mql.matches);
                if (typeof globalThis.scrollTo === "function") {
                    globalThis.scrollTo({
                        top: 0,
                        behavior: prefersReducedMotion ? "auto" : "smooth"
                    });
                }
                const target =
                    typeof document.getElementById === "function"
                        ? document.getElementById("furo-main-content")
                        : mainContent;
                if (target) {
                    if (
                        typeof target.hasAttribute === "function" &&
                        !target.hasAttribute("tabindex") &&
                        typeof target.setAttribute === "function"
                    ) {
                        target.setAttribute("tabindex", "-1");
                    }
                    if (typeof target.focus === "function") {
                        target.focus({ preventScroll: true });
                    }
                }
            });
        });
    }

    function initAccessibleCodeBlocks() {
        initMainContentFocus();
        if (globalThis.__imednetCodeBlocksInitialized) {
            checkCodeBlocks();
            return;
        }
        globalThis.__imednetCodeBlocksInitialized = true;
        globalThis.imednetCheckCodeBlocks = checkCodeBlocks;
        globalThis.imednetScheduleCheckCodeBlocks = scheduleCheckCodeBlocks;

        checkCodeBlocks();
        window.addEventListener("resize", scheduleCheckCodeBlocks);
        window.addEventListener("load", scheduleCheckCodeBlocks);

        if (typeof MutationObserver !== "undefined") {
            const observer = new MutationObserver(function () {
                scheduleCheckCodeBlocks();
            });
            const target = document.body || document.documentElement;
            if (target) {
                observer.observe(target, {
                    attributes: true,
                    attributeFilter: ["hidden", "class"],
                    subtree: true
                });
            }

            const mermaidObserver = new MutationObserver(function () {
                scheduleCheckCodeBlocks();
            });
            document.querySelectorAll(".mermaid").forEach(function (diagram) {
                mermaidObserver.observe(diagram, {
                    childList: true,
                    subtree: true
                });
            });
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initAccessibleCodeBlocks);
    } else {
        initAccessibleCodeBlocks();
    }
})();
