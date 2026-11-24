/**
 * WCAG 2.1 Level AA keyboard focus management for overflowing <pre> code blocks,
 * Mermaid diagram regions, and wide .table-wrapper containers across documentation pages.
 */
(function () {
    "use strict";

    let codeBlocksRaf = null;

    function resolveTableRegionLabel(wrapper) {
        if (!wrapper) return "Scrollable data table";
        if (typeof wrapper.querySelector === "function") {
            const captionEl = wrapper.querySelector("caption");
            if (captionEl && captionEl.textContent && captionEl.textContent.trim()) {
                return captionEl.textContent.trim();
            }
        }
        let cur = wrapper.previousElementSibling;
        while (cur) {
            if (/^H[1-6]$/i.test(cur.tagName || "") && cur.textContent && cur.textContent.trim()) {
                return cur.textContent.trim() + " table";
            }
            cur = cur.previousElementSibling;
        }
        const section =
            typeof wrapper.closest === "function" ? wrapper.closest("section") : null;
        if (section && typeof section.querySelector === "function") {
            const heading = section.querySelector("h1, h2, h3, h4, h5, h6");
            if (heading && heading.textContent) {
                const cleaned = heading.textContent.replace(/¶|#$/g, "").trim();
                if (cleaned) {
                    return cleaned + " table";
                }
            }
        }
        return "Scrollable data table";
    }

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

        const tableWrappers = document.querySelectorAll(".table-wrapper");
        tableWrappers.forEach(function (wrapper) {
            const isOverflowing =
                wrapper.clientWidth > 0 && wrapper.scrollWidth > wrapper.clientWidth;
            if (isOverflowing) {
                const tableLabel = resolveTableRegionLabel(wrapper);
                wrapper.setAttribute("tabindex", "0");
                wrapper.setAttribute("role", "region");
                if (!wrapper.getAttribute("aria-label") || wrapper.hasAttribute("data-table-region")) {
                    wrapper.setAttribute("aria-label", tableLabel);
                    wrapper.setAttribute("data-table-region", "true");
                }
            } else {
                if (wrapper.hasAttribute("tabindex")) {
                    wrapper.removeAttribute("tabindex");
                }
                if (wrapper.hasAttribute("data-table-region")) {
                    wrapper.removeAttribute("role");
                    wrapper.removeAttribute("aria-label");
                    wrapper.removeAttribute("data-table-region");
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
        if (globalThis.__imednetCodeBlocksInitialized) {
            checkCodeBlocks();
            return;
        }
        globalThis.__imednetCodeBlocksInitialized = true;
        globalThis.imednetCheckCodeBlocks = checkCodeBlocks;
        globalThis.imednetScheduleCheckCodeBlocks = scheduleCheckCodeBlocks;

        checkCodeBlocks();
        if (typeof globalThis.addEventListener === "function") {
            globalThis.addEventListener("resize", scheduleCheckCodeBlocks);
            globalThis.addEventListener("load", scheduleCheckCodeBlocks);
        }

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
