/**
 * Shared keyboard and screen-reader navigation controller for Sphinx + Furo documentation.
 *
 * Restores WCAG 2.1/2.2 SC 2.1.1 (Keyboard), SC 2.4.3 (Focus Order), SC 2.4.7 (Focus Visible),
 * and SC 4.1.2 (Name, Role, Value) across:
 *   1. Sidebar submenu switches (input.toctree-checkbox) — Space + Enter toggling.
 *   2. Responsive drawers (.sidebar-drawer <= 63em, .toc-drawer <= 82em) — native
 *      <button type="button"> triggers, modal dialog semantics, sibling inert isolation,
 *      inline close buttons, Escape/backdrop restoration, and TOC heading focus transfer.
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
        if (!el || typeof globalThis.getComputedStyle !== "function") {
            return false;
        }
        let cur = el;
        while (cur && cur.nodeType === 1) {
            if (cur.hasAttribute("hidden") || cur.inert) {
                return false;
            }
            const style = globalThis.getComputedStyle(cur);
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
        const mainRegion = document.querySelector(".main");
        const contentRegion =
            document.querySelector(".main > .content") || document.querySelector(".content");
        const mobileHeader = document.querySelector("header.mobile-header");
        const skipLink = document.querySelector("a.skip-to-content");
        const announcements = Array.from(document.querySelectorAll(".announcement"));

        if (!sidebarDrawer && !tocDrawer) {
            return;
        }

        if (sidebarDrawer && !sidebarDrawer.id) {
            sidebarDrawer.id = SIDEBAR_DRAWER_ID;
        }
        if (tocDrawer && !tocDrawer.id) {
            tocDrawer.id = TOC_DRAWER_ID;
        }

        const rawNavTriggers = Array.from(document.querySelectorAll(".nav-overlay-icon"));
        const rawTocTriggers = Array.from(
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
            lbl.removeAttribute("for");
            lbl.setAttribute("aria-hidden", "true");
        });

        const lastTrigger = {
            nav: null,
            toc: null
        };

        const sidebarMql =
            typeof globalThis.matchMedia === "function"
                ? globalThis.matchMedia("(max-width: 63em)")
                : { matches: false };
        const tocMql =
            typeof globalThis.matchMedia === "function"
                ? globalThis.matchMedia("(max-width: 82em)")
                : { matches: false };

        function upgradeTrigger(trigger, kind, toggleInput, drawerEl, fallbackLabel) {
            if (!trigger || !toggleInput || !drawerEl) {
                return trigger;
            }
            const labelText =
                (toggleInput.getAttribute("aria-label") || "").trim() || fallbackLabel;

            let btn = trigger;
            if ((trigger.tagName || "").toUpperCase() !== "BUTTON") {
                btn = document.createElement("button");
                btn.type = "button";
                if (trigger.id) {
                    btn.id = trigger.id;
                }
                if (trigger.className) {
                    btn.className = trigger.className;
                }
                if (typeof trigger.hasAttribute === "function" && trigger.hasAttribute("title")) {
                    btn.setAttribute("title", trigger.getAttribute("title") || "");
                }
                while (trigger.firstChild) {
                    btn.appendChild(trigger.firstChild);
                }
                if (trigger.parentNode) {
                    trigger.parentNode.replaceChild(btn, trigger);
                }
            } else {
                btn.setAttribute("type", "button");
                btn.removeAttribute("role");
                btn.removeAttribute("for");
                btn.removeAttribute("tabindex");
            }

            btn.setAttribute("aria-controls", drawerEl.id);
            btn.setAttribute("aria-label", labelText);
            btn.setAttribute("aria-expanded", toggleInput.checked ? "true" : "false");

            btn.querySelectorAll(".icon, svg").forEach(function (iconEl) {
                iconEl.setAttribute("aria-hidden", "true");
            });

            if (btn.getAttribute("data-nav-upgraded") === "true") {
                return btn;
            }
            btn.setAttribute("data-nav-upgraded", "true");

            btn.addEventListener("click", function () {
                lastTrigger[kind] = btn;
                toggleInput.checked = !toggleInput.checked;
                toggleInput.dispatchEvent(new Event("change", { bubbles: true }));
            });

            return btn;
        }

        const navTriggers = rawNavTriggers
            .map(function (trigger) {
                return upgradeTrigger(
                    trigger,
                    "nav",
                    navInput,
                    sidebarDrawer,
                    "Toggle site navigation sidebar"
                );
            })
            .filter(Boolean);

        const tocTriggers = rawTocTriggers
            .map(function (trigger) {
                return upgradeTrigger(
                    trigger,
                    "toc",
                    tocInput,
                    tocDrawer,
                    "Toggle table of contents sidebar"
                );
            })
            .filter(Boolean);

        let navHeaderEl = null;
        let navCloseBtn = null;
        let tocHeaderEl = null;
        let tocCloseBtn = null;

        function createCloseButton(ariaLabel, kind, toggleInput) {
            const closeBtn = document.createElement("button");
            closeBtn.type = "button";
            closeBtn.className = "drawer-close-btn";
            closeBtn.setAttribute("aria-label", ariaLabel);
            closeBtn.hidden = true;
            const iconSpan = document.createElement("span");
            iconSpan.setAttribute("aria-hidden", "true");
            iconSpan.textContent = "\u00d7";
            closeBtn.appendChild(iconSpan);
            closeBtn.addEventListener("click", function () {
                if (toggleInput) {
                    toggleInput.checked = false;
                }
                syncDrawers();
                restoreTriggerFocus(kind, false);
            });
            return closeBtn;
        }

        if (sidebarDrawer) {
            navCloseBtn = sidebarDrawer.querySelector("button.drawer-close-btn");
            navHeaderEl = sidebarDrawer.querySelector(".drawer-header");
            if (!navCloseBtn) {
                navHeaderEl = document.createElement("div");
                navHeaderEl.className = "drawer-header";
                navHeaderEl.hidden = true;
                const titleSpan = document.createElement("span");
                titleSpan.className = "drawer-header-title";
                titleSpan.textContent = "Navigation";
                navCloseBtn = createCloseButton("Close site navigation", "nav", navInput);
                navHeaderEl.appendChild(titleSpan);
                navHeaderEl.appendChild(navCloseBtn);
                const sticky =
                    sidebarDrawer.querySelector(".sidebar-sticky") ||
                    sidebarDrawer.firstElementChild ||
                    sidebarDrawer;
                sticky.insertBefore(navHeaderEl, sticky.firstChild);
            }
        }

        if (tocDrawer && !tocDrawer.classList.contains("no-toc")) {
            tocCloseBtn = tocDrawer.querySelector("button.drawer-close-btn");
            tocHeaderEl = tocDrawer.querySelector(".drawer-header");
            if (!tocCloseBtn) {
                tocCloseBtn = createCloseButton("Close table of contents", "toc", tocInput);
                const titleContainer = tocDrawer.querySelector(".toc-title-container");
                if (titleContainer) {
                    titleContainer.appendChild(tocCloseBtn);
                } else {
                    tocHeaderEl = document.createElement("div");
                    tocHeaderEl.className = "drawer-header";
                    tocHeaderEl.hidden = true;
                    tocHeaderEl.appendChild(tocCloseBtn);
                    const sticky =
                        tocDrawer.querySelector(".toc-sticky") ||
                        tocDrawer.firstElementChild ||
                        tocDrawer;
                    sticky.insertBefore(tocHeaderEl, sticky.firstChild);
                }
            }
        }

        function syncDrawers() {
            const sidebarModal = Boolean(sidebarMql && sidebarMql.matches);
            const tocModal = Boolean(tocMql && tocMql.matches);
            const isNoToc = Boolean(tocDrawer && tocDrawer.classList.contains("no-toc"));

            if (!sidebarModal && navInput && navInput.checked) {
                navInput.checked = false;
            }
            if ((!tocModal || isNoToc) && tocInput && tocInput.checked) {
                tocInput.checked = false;
            }

            const navOpen = Boolean(sidebarDrawer && sidebarModal && navInput && navInput.checked);
            let tocOpen = Boolean(tocDrawer && !isNoToc && tocModal && tocInput && tocInput.checked);
            if (navOpen && tocOpen && tocInput) {
                tocInput.checked = false;
                tocOpen = false;
            }
            const anyModalOpen = navOpen || tocOpen;

            if (sidebarDrawer) {
                setInertState(sidebarDrawer, (sidebarModal && !navOpen) || tocOpen);
                if (!sidebarDrawer.getAttribute("aria-label")) {
                    sidebarDrawer.setAttribute("aria-label", "Site navigation");
                }
                if (navOpen) {
                    sidebarDrawer.setAttribute("role", "dialog");
                    sidebarDrawer.setAttribute("aria-modal", "true");
                } else {
                    sidebarDrawer.removeAttribute("role");
                    sidebarDrawer.removeAttribute("aria-modal");
                }
                if (navHeaderEl) {
                    navHeaderEl.hidden = !navOpen;
                }
                if (navCloseBtn) {
                    navCloseBtn.hidden = !navOpen;
                }
                navTriggers.forEach(function (trigger) {
                    trigger.setAttribute("aria-expanded", navOpen ? "true" : "false");
                });
            }

            if (tocDrawer) {
                setInertState(tocDrawer, isNoToc || (tocModal && !tocOpen) || navOpen);
                if (!isNoToc && !tocDrawer.getAttribute("aria-label")) {
                    tocDrawer.setAttribute("aria-label", "Table of contents");
                }
                if (tocOpen) {
                    tocDrawer.setAttribute("role", "dialog");
                    tocDrawer.setAttribute("aria-modal", "true");
                } else {
                    tocDrawer.removeAttribute("role");
                    tocDrawer.removeAttribute("aria-modal");
                }
                if (tocHeaderEl) {
                    tocHeaderEl.hidden = !tocOpen;
                }
                if (tocCloseBtn) {
                    tocCloseBtn.hidden = !tocOpen;
                }
                tocTriggers.forEach(function (trigger) {
                    trigger.setAttribute("aria-expanded", tocOpen ? "true" : "false");
                });
            }

            const mainContainsToc = Boolean(
                mainRegion &&
                    tocDrawer &&
                    typeof mainRegion.contains === "function" &&
                    mainRegion.contains(tocDrawer)
            );
            setInertState(mainRegion, navOpen || (tocOpen && !mainContainsToc));
            if (mainRegion && mainRegion.children) {
                Array.from(mainRegion.children).forEach(function (child) {
                    if (child !== tocDrawer) {
                        setInertState(child, anyModalOpen);
                    }
                });
            } else if (contentRegion) {
                setInertState(contentRegion, anyModalOpen);
            }
            setInertState(mobileHeader, anyModalOpen);
            setInertState(skipLink, anyModalOpen);
            announcements.forEach(function (el) {
                setInertState(el, anyModalOpen);
            });
        }

        function focusInsideDrawer(drawerEl) {
            if (!drawerEl) return;
            void drawerEl.offsetWidth;
            const preferred = drawerEl.querySelectorAll(
                ".toc-tree a[href], .sidebar-brand[href], .sidebar-search, .sidebar-tree a[href]"
            );
            for (let i = 0; i < preferred.length; i++) {
                if (isElementVisible(preferred[i])) {
                    preferred[i].focus();
                    return;
                }
            }
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
                    syncDrawers();
                    if (sidebarModal) {
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
                    syncDrawers();
                    if (tocModal && !isNoToc) {
                        restoreTriggerFocus("toc", false);
                    }
                }
            });
        }

        function resolveTocTarget(link) {
            if (!link || typeof link.getAttribute !== "function") {
                return null;
            }
            const rawHref = (link.getAttribute("href") || "").trim();
            if (!rawHref || rawHref.charAt(0) !== "#") {
                return null;
            }
            if (rawHref === "#") {
                const topHeading = document.querySelector(
                    "#furo-main-content h1, article h1, .main h1"
                );
                const mainContent = document.getElementById("furo-main-content");
                return {
                    rawHref: rawHref,
                    scrollEl: null,
                    focusEl: topHeading || mainContent
                };
            }
            let targetId = rawHref.slice(1);
            try {
                targetId = decodeURIComponent(targetId);
            } catch (_err) {
                // Preserve raw fragment identifier when percent-decoding fails
            }
            const anchorEl = document.getElementById(targetId);
            if (!anchorEl) {
                return null;
            }
            let focusEl = anchorEl;
            const tagName = (anchorEl.tagName || "").toUpperCase();
            if (
                !/^H[1-6]$/.test(tagName) &&
                tagName !== "DT" &&
                typeof anchorEl.querySelector === "function"
            ) {
                const childHeading = anchorEl.querySelector(
                    ":scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > h5, :scope > h6, h1, h2, h3, h4, h5, h6"
                );
                if (childHeading) {
                    focusEl = childHeading;
                }
            }
            return {
                rawHref: rawHref,
                scrollEl: anchorEl,
                focusEl: focusEl
            };
        }

        if (tocDrawer && tocDrawer.getAttribute("data-nav-upgraded") !== "true") {
            tocDrawer.setAttribute("data-nav-upgraded", "true");
            tocDrawer.addEventListener("click", function (event) {
                const target = event.target;
                const link =
                    target && typeof target.closest === "function"
                        ? target.closest("a[href]")
                        : null;
                if (!link) {
                    return;
                }
                const resolved = resolveTocTarget(link);
                const wasModalOpen = Boolean(
                    tocInput && tocInput.checked && tocMql && tocMql.matches
                );
                if (!resolved) {
                    if (wasModalOpen) {
                        tocInput.checked = false;
                        syncDrawers();
                    }
                    return;
                }
                if (wasModalOpen || resolved.focusEl) {
                    if (event && typeof event.preventDefault === "function") {
                        event.preventDefault();
                    }
                    if (tocInput && tocInput.checked) {
                        tocInput.checked = false;
                    }
                    syncDrawers();
                    if (
                        globalThis.location &&
                        globalThis.history &&
                        typeof globalThis.history.pushState === "function"
                    ) {
                        const expectedHash = resolved.rawHref === "#" ? "" : resolved.rawHref;
                        const nextUrl =
                            resolved.rawHref === "#"
                                ? globalThis.location.pathname + globalThis.location.search
                                : resolved.rawHref;
                        if (globalThis.location.hash !== expectedHash) {
                            globalThis.history.pushState(null, "", nextUrl);
                        }
                    }
                    if (resolved.rawHref === "#") {
                        if (typeof globalThis.scrollTo === "function") {
                            try {
                                globalThis.scrollTo({ top: 0, behavior: "instant" });
                            } catch (_err) {
                                globalThis.scrollTo(0, 0);
                            }
                        }
                    } else if (
                        resolved.scrollEl &&
                        typeof resolved.scrollEl.scrollIntoView === "function"
                    ) {
                        try {
                            resolved.scrollEl.scrollIntoView({
                                block: "start",
                                behavior: "instant"
                            });
                        } catch (_err) {
                            resolved.scrollEl.scrollIntoView(true);
                        }
                    }
                    if (resolved.focusEl) {
                        if (
                            typeof resolved.focusEl.hasAttribute === "function" &&
                            !resolved.focusEl.hasAttribute("tabindex") &&
                            typeof resolved.focusEl.setAttribute === "function"
                        ) {
                            resolved.focusEl.setAttribute("tabindex", "-1");
                        }
                        if (typeof resolved.focusEl.focus === "function") {
                            resolved.focusEl.focus({ preventScroll: true });
                        }
                    }
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

        function getOpenModalDrawer() {
            const isNoToc = Boolean(tocDrawer && tocDrawer.classList.contains("no-toc"));
            if (tocDrawer && !isNoToc && tocInput && tocInput.checked && tocMql && tocMql.matches) {
                return tocDrawer;
            }
            if (sidebarDrawer && navInput && navInput.checked && sidebarMql && sidebarMql.matches) {
                return sidebarDrawer;
            }
            return null;
        }

        if (!document.documentElement.hasAttribute("data-nav-escape-bound")) {
            document.documentElement.setAttribute("data-nav-escape-bound", "true");
            bindMediaQuery(sidebarMql, syncDrawers);
            bindMediaQuery(tocMql, syncDrawers);
            if (typeof globalThis.addEventListener === "function") {
                globalThis.addEventListener("resize", syncDrawers);
            }

            document.addEventListener("keydown", function (event) {
                if (event.key === "Tab" && !event.altKey && !event.ctrlKey && !event.metaKey) {
                    const activeDrawer = getOpenModalDrawer();
                    if (!activeDrawer) {
                        return;
                    }
                    const focusables = Array.from(
                        activeDrawer.querySelectorAll(FOCUSABLE_SELECTOR)
                    ).filter(isElementVisible);
                    if (!focusables.length) {
                        event.preventDefault();
                        focusInsideDrawer(activeDrawer);
                        return;
                    }
                    const first = focusables[0];
                    const last = focusables[focusables.length - 1];
                    const active = document.activeElement;
                    if (!active || !activeDrawer.contains(active)) {
                        event.preventDefault();
                        (event.shiftKey ? last : first).focus();
                    } else if (event.shiftKey && active === first) {
                        event.preventDefault();
                        last.focus();
                    } else if (!event.shiftKey && active === last) {
                        event.preventDefault();
                        first.focus();
                    }
                    return;
                }

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

    // -------------------------------------------------------------------------
    // 4. Main landmark tabindex & Back-to-Top keyboard focus transfer (WCAG 2.4.7)
    // -------------------------------------------------------------------------
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
            link.addEventListener("click", function (event) {
                if (event && typeof event.preventDefault === "function") {
                    event.preventDefault();
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

    function initNavigation() {
        initMainContentFocus();
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
