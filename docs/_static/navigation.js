/**
 * Shared keyboard and screen-reader navigation controller for Sphinx + Furo documentation.
 *
 * Restores WCAG 2.1/2.2 SC 2.1.1 (Keyboard), SC 2.4.3 (Focus Order), SC 2.4.7 (Focus Visible),
 * and SC 4.1.2 (Name, Role, Value) across:
 *   1. Sidebar submenu switches (input.toctree-checkbox) — Space + Enter toggling.
 *   2. Responsive drawers (.sidebar-drawer <= 63em, .toc-drawer <= 82em) — inert syncing,
 *      accessible label button triggers, focus entry on open, and Escape / TOC link close.
 *   3. Module index toggles (img.toggler on py-modindex.html) — role="button", tabindex="0",
 *      package aria-label, keyboard Enter/Space activation, and aria-expanded + alt syncing.
 */
(function () {
    "use strict";

    const SIDEBAR_DRAWER_ID = "furo-sidebar-drawer";
    const TOC_DRAWER_ID = "furo-toc-drawer";
    const FOCUSABLE_SELECTOR = [
        "a[href]",
        "button:not(:disabled)",
        "input:not(:disabled):not(.sidebar-toggle)",
        "select:not(:disabled)",
        "textarea:not(:disabled)",
        '[tabindex]:not([tabindex="-1"])'
    ].join(", ");

    function isElementVisible(el) {
        if (!el || typeof window.getComputedStyle !== "function") {
            return false;
        }
        let cur = el;
        while (cur && cur.nodeType === 1) {
            if (cur.hasAttribute("hidden") || cur.inert) {
                return false;
            }
            const style = window.getComputedStyle(cur);
            if (style.display === "none" || style.visibility === "hidden") {
                return false;
            }
            cur = cur.parentElement;
        }
        return el.getClientRects().length > 0;
    }

    function setInertState(el, shouldBeInert) {
        if (!el) return;
        const next = Boolean(shouldBeInert);
        el.inert = next;
        if (next) {
            el.setAttribute("inert", "");
        } else {
            el.removeAttribute("inert");
        }
    }

    // -------------------------------------------------------------------------
    // 1. Sidebar submenu switches (.toctree-checkbox)
    // -------------------------------------------------------------------------
    function initToctreeSwitches() {
        const switches = document.querySelectorAll("input.toctree-checkbox");
        switches.forEach(function (checkbox) {
            if (!checkbox.getAttribute("role")) {
                checkbox.setAttribute("role", "switch");
            }
            if (!checkbox.getAttribute("aria-label")) {
                const parentItem = checkbox.closest("li");
                const link = parentItem ? parentItem.querySelector(":scope > a") : null;
                const linkText = link && link.textContent ? link.textContent.trim() : "submenu";
                checkbox.setAttribute("aria-label", "Toggle navigation of " + linkText);
            }
            const associatedLabel = checkbox.id
                ? document.querySelector('label[for="' + CSS.escape(checkbox.id) + '"]')
                : null;
            if (associatedLabel) {
                const iconSpan = associatedLabel.querySelector(".icon, svg");
                if (iconSpan) {
                    iconSpan.setAttribute("aria-hidden", "true");
                }
            }

            if (checkbox.getAttribute("data-nav-upgraded") === "true") {
                return;
            }
            checkbox.setAttribute("data-nav-upgraded", "true");

            checkbox.addEventListener("keydown", function (event) {
                if (event.altKey || event.ctrlKey || event.metaKey) {
                    return;
                }
                if (event.key === "Enter") {
                    event.preventDefault();
                    checkbox.checked = !checkbox.checked;
                    checkbox.dispatchEvent(new Event("change", { bubbles: true }));
                }
            });
        });
    }

    // -------------------------------------------------------------------------
    // 2. Responsive drawers (.sidebar-drawer & .toc-drawer) and triggers
    // -------------------------------------------------------------------------
    function initResponsiveDrawers() {
        const navInput = document.getElementById("__navigation");
        const tocInput = document.getElementById("__toc");
        const sidebarDrawer = document.querySelector(".sidebar-drawer");
        const tocDrawer = document.querySelector(".toc-drawer");

        if (!sidebarDrawer && !tocDrawer) {
            return;
        }

        if (sidebarDrawer && !sidebarDrawer.id) {
            sidebarDrawer.id = SIDEBAR_DRAWER_ID;
        }
        if (tocDrawer && !tocDrawer.id) {
            tocDrawer.id = TOC_DRAWER_ID;
        }

        const navTriggers = Array.from(document.querySelectorAll(".nav-overlay-icon"));
        const tocTriggers = Array.from(
            document.querySelectorAll(
                ".toc-header-icon:not(.no-toc), .toc-content-icon:not(.no-toc)"
            )
        );
        const hiddenTocLabels = document.querySelectorAll(
            ".toc-header-icon.no-toc, .toc-content-icon.no-toc"
        );
        hiddenTocLabels.forEach(function (lbl) {
            lbl.removeAttribute("tabindex");
            lbl.removeAttribute("role");
            lbl.setAttribute("aria-hidden", "true");
        });

        const lastTrigger = {
            nav: null,
            toc: null
        };

        const sidebarMql =
            typeof window.matchMedia === "function"
                ? window.matchMedia("(max-width: 63em)")
                : { matches: false };
        const tocMql =
            typeof window.matchMedia === "function"
                ? window.matchMedia("(max-width: 82em)")
                : { matches: false };

        function upgradeTrigger(trigger, kind, toggleInput, drawerEl, fallbackLabel) {
            if (!trigger || !toggleInput || !drawerEl) {
                return;
            }
            const labelText =
                (toggleInput.getAttribute("aria-label") || "").trim() || fallbackLabel;
            trigger.setAttribute("role", "button");
            trigger.setAttribute("tabindex", "0");
            trigger.setAttribute("aria-controls", drawerEl.id);
            trigger.setAttribute("aria-label", labelText);
            trigger.setAttribute("aria-expanded", toggleInput.checked ? "true" : "false");

            trigger.querySelectorAll(".icon, svg").forEach(function (iconEl) {
                iconEl.setAttribute("aria-hidden", "true");
            });

            if (trigger.getAttribute("data-nav-upgraded") === "true") {
                return;
            }
            trigger.setAttribute("data-nav-upgraded", "true");

            trigger.addEventListener("click", function () {
                lastTrigger[kind] = trigger;
            });

            trigger.addEventListener("keydown", function (event) {
                if (event.altKey || event.ctrlKey || event.metaKey) {
                    return;
                }
                if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
                    event.preventDefault();
                    lastTrigger[kind] = trigger;
                    toggleInput.checked = !toggleInput.checked;
                    toggleInput.dispatchEvent(new Event("change", { bubbles: true }));
                }
            });
        }

        navTriggers.forEach(function (trigger) {
            upgradeTrigger(
                trigger,
                "nav",
                navInput,
                sidebarDrawer,
                "Toggle site navigation sidebar"
            );
        });
        tocTriggers.forEach(function (trigger) {
            upgradeTrigger(
                trigger,
                "toc",
                tocInput,
                tocDrawer,
                "Toggle table of contents sidebar"
            );
        });

        function syncDrawers() {
            const sidebarModal = Boolean(sidebarMql && sidebarMql.matches);
            if (sidebarDrawer) {
                if (!sidebarModal && navInput && navInput.checked) {
                    navInput.checked = false;
                }
                const navOpen = Boolean(sidebarModal && navInput && navInput.checked);
                setInertState(sidebarDrawer, sidebarModal && !navOpen);
                navTriggers.forEach(function (trigger) {
                    trigger.setAttribute("aria-expanded", navOpen ? "true" : "false");
                });
            }

            const tocModal = Boolean(tocMql && tocMql.matches);
            if (tocDrawer) {
                const isNoToc = tocDrawer.classList.contains("no-toc");
                if ((!tocModal || isNoToc) && tocInput && tocInput.checked) {
                    tocInput.checked = false;
                }
                const tocOpen = Boolean(!isNoToc && tocModal && tocInput && tocInput.checked);
                setInertState(tocDrawer, isNoToc || (tocModal && !tocOpen));
                tocTriggers.forEach(function (trigger) {
                    trigger.setAttribute("aria-expanded", tocOpen ? "true" : "false");
                });
            }
        }

        function focusInsideDrawer(drawerEl) {
            if (!drawerEl) return;
            void drawerEl.offsetWidth;
            const candidates = drawerEl.querySelectorAll(FOCUSABLE_SELECTOR);
            for (let i = 0; i < candidates.length; i++) {
                const candidate = candidates[i];
                if (isElementVisible(candidate)) {
                    candidate.focus();
                    return;
                }
            }
            if (!drawerEl.hasAttribute("tabindex")) {
                drawerEl.setAttribute("tabindex", "-1");
            }
            drawerEl.focus();
        }

        function restoreTriggerFocus(kind, preventScroll) {
            const list = kind === "nav" ? navTriggers : tocTriggers;
            const preferred = lastTrigger[kind];
            let target = isElementVisible(preferred) ? preferred : null;
            if (!target) {
                for (let i = 0; i < list.length; i++) {
                    if (isElementVisible(list[i])) {
                        target = list[i];
                        break;
                    }
                }
            }
            if (target && typeof target.focus === "function") {
                if (preventScroll) {
                    target.focus({ preventScroll: true });
                } else {
                    target.focus();
                }
            }
        }

        if (navInput && navInput.getAttribute("data-nav-upgraded") !== "true") {
            navInput.setAttribute("data-nav-upgraded", "true");
            navInput.addEventListener("change", function () {
                const sidebarModal = Boolean(sidebarMql && sidebarMql.matches);
                if (navInput.checked && sidebarModal) {
                    if (tocInput && tocInput.checked) {
                        tocInput.checked = false;
                    }
                    syncDrawers();
                    focusInsideDrawer(sidebarDrawer);
                } else {
                    const active = document.activeElement;
                    const focusInside = Boolean(
                        sidebarDrawer && active && sidebarDrawer.contains(active)
                    );
                    syncDrawers();
                    if (focusInside || active === document.body || active === navInput) {
                        restoreTriggerFocus("nav", false);
                    }
                }
            });
        }

        if (tocInput && tocInput.getAttribute("data-nav-upgraded") !== "true") {
            tocInput.setAttribute("data-nav-upgraded", "true");
            tocInput.addEventListener("change", function () {
                const tocModal = Boolean(tocMql && tocMql.matches);
                const isNoToc = Boolean(tocDrawer && tocDrawer.classList.contains("no-toc"));
                if (tocInput.checked && tocModal && !isNoToc) {
                    if (navInput && navInput.checked && sidebarMql && sidebarMql.matches) {
                        navInput.checked = false;
                    }
                    syncDrawers();
                    focusInsideDrawer(tocDrawer);
                } else {
                    const active = document.activeElement;
                    const focusInside = Boolean(tocDrawer && active && tocDrawer.contains(active));
                    syncDrawers();
                    if (focusInside || active === document.body || active === tocInput) {
                        restoreTriggerFocus("toc", false);
                    }
                }
            });
        }

        if (tocDrawer && tocDrawer.getAttribute("data-nav-upgraded") !== "true") {
            tocDrawer.setAttribute("data-nav-upgraded", "true");
            tocDrawer.addEventListener("click", function (event) {
                const target = event.target;
                const link =
                    target && typeof target.closest === "function"
                        ? target.closest("a[href]")
                        : null;
                if (link && tocInput && tocInput.checked && tocMql && tocMql.matches) {
                    tocInput.checked = false;
                    syncDrawers();
                    restoreTriggerFocus("toc", true);
                    window.setTimeout(function () {
                        restoreTriggerFocus("toc", true);
                    }, 0);
                }
            });
        }

        function bindMediaQuery(mql, handler) {
            if (!mql) return;
            if (typeof mql.addEventListener === "function") {
                mql.addEventListener("change", handler);
            } else if (typeof mql.addListener === "function") {
                mql.addListener(handler);
            }
        }

        if (!document.documentElement.hasAttribute("data-nav-escape-bound")) {
            document.documentElement.setAttribute("data-nav-escape-bound", "true");
            bindMediaQuery(sidebarMql, syncDrawers);
            bindMediaQuery(tocMql, syncDrawers);
            window.addEventListener("resize", syncDrawers);

            document.addEventListener("keydown", function (event) {
                if (event.key !== "Escape") {
                    return;
                }
                if (tocInput && tocInput.checked && tocMql && tocMql.matches) {
                    event.preventDefault();
                    tocInput.checked = false;
                    syncDrawers();
                    restoreTriggerFocus("toc", false);
                } else if (navInput && navInput.checked && sidebarMql && sidebarMql.matches) {
                    event.preventDefault();
                    navInput.checked = false;
                    syncDrawers();
                    restoreTriggerFocus("nav", false);
                }
            });
        }

        syncDrawers();
    }

    // -------------------------------------------------------------------------
    // 3. Python Module Index group toggles (img.toggler on py-modindex.html)
    // -------------------------------------------------------------------------
    function syncTogglerState(img) {
        if (!img) return;
        const src = img.getAttribute("src") || img.src || "";
        const isExpanded = !src.endsWith("plus.png");
        const expectedExpanded = isExpanded ? "true" : "false";
        const expectedAlt = isExpanded ? "-" : "+";

        if (img.getAttribute("aria-expanded") !== expectedExpanded) {
            img.setAttribute("aria-expanded", expectedExpanded);
        }
        if (img.getAttribute("alt") !== expectedAlt) {
            img.setAttribute("alt", expectedAlt);
        }
    }

    function initModIndexTogglers() {
        const togglers = document.querySelectorAll("img.toggler");
        if (!togglers.length) {
            return;
        }

        togglers.forEach(function (img) {
            const row = img.closest("tr");
            const codeEl = row
                ? row.querySelector("code.xref, a code, td:nth-of-type(2) a")
                : null;
            const pkgName =
                codeEl && codeEl.textContent
                    ? codeEl.textContent.trim()
                    : img.id || "module group";

            img.setAttribute("role", "button");
            img.setAttribute("tabindex", "0");
            img.setAttribute("aria-label", "Toggle " + pkgName + " submodules");
            syncTogglerState(img);

            if (img.getAttribute("data-nav-upgraded") === "true") {
                return;
            }
            img.setAttribute("data-nav-upgraded", "true");

            img.addEventListener("click", function () {
                syncTogglerState(img);
            });

            img.addEventListener("keydown", function (event) {
                if (event.altKey || event.ctrlKey || event.metaKey) {
                    return;
                }
                if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
                    event.preventDefault();
                    img.click();
                    syncTogglerState(img);
                }
            });

            if (typeof MutationObserver !== "undefined") {
                const observer = new MutationObserver(function () {
                    syncTogglerState(img);
                });
                observer.observe(img, {
                    attributes: true,
                    attributeFilter: ["src"]
                });
            }
        });
    }

    function initNavigation() {
        initToctreeSwitches();
        initResponsiveDrawers();
        initModIndexTogglers();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initNavigation);
    } else {
        initNavigation();
    }
})();
