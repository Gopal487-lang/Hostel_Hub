
/* =========================================================
   HOSTELHUB — MAIN JAVASCRIPT
   Search, categories, navigation, animations, forms,
   mobile menu, marketplace and interactive components.
========================================================= */

"use strict";

document.addEventListener("DOMContentLoaded", () => {
    initializeApp();
});

function initializeApp() {
    initializeMobileMenu();
    initializeSearch();
    initializeCategoryFilters();
    initializeScrollAnimations();
    initializeBackToTop();
    initializeCurrentYear();
    initializeForms();
    initializeInteractiveButtons();
    initializeSmoothScrolling();

    console.log("HostelHub JavaScript initialized successfully.");
}

/* =========================================================
   1. HELPERS
========================================================= */

function selectOne(selectors, parent = document) {
    for (const selector of selectors) {
        const element = parent.querySelector(selector);
        if (element) return element;
    }

    return null;
}

function selectAll(selectors, parent = document) {
    const elements = new Set();

    for (const selector of selectors) {
        parent.querySelectorAll(selector).forEach(element => {
            elements.add(element);
        });
    }

    return [...elements];
}

function showNotice(message, type = "info") {
    let container = document.querySelector("#hh-notifications");

    if (!container) {
        container = document.createElement("div");
        container.id = "hh-notifications";

        Object.assign(container.style, {
            position: "fixed",
            top: "20px",
            right: "20px",
            zIndex: "99999",
            display: "flex",
            flexDirection: "column",
            gap: "10px",
            maxWidth: "min(360px, calc(100vw - 40px))"
        });

        document.body.appendChild(container);
    }

    const notice = document.createElement("div");
    notice.textContent = message;

    const backgrounds = {
        info: "#243b73",
        success: "#16865b",
        error: "#c0392b"
    };

    Object.assign(notice.style, {
        background: backgrounds[type] || backgrounds.info,
        color: "#ffffff",
        padding: "14px 18px",
        borderRadius: "12px",
        boxShadow: "0 8px 25px rgba(0,0,0,.18)",
        fontSize: "14px",
        lineHeight: "1.5",
        opacity: "0",
        transform: "translateY(-8px)",
        transition: "opacity .25s ease, transform .25s ease"
    });

    container.appendChild(notice);

    requestAnimationFrame(() => {
        notice.style.opacity = "1";
        notice.style.transform = "translateY(0)";
    });

    window.setTimeout(() => {
        notice.style.opacity = "0";
        notice.style.transform = "translateY(-8px)";

        window.setTimeout(() => notice.remove(), 300);
    }, 3500);
}

/* =========================================================
   2. MOBILE NAVIGATION
========================================================= */

function initializeMobileMenu() {
    const menuButton = selectOne([
        "#menu-toggle",
        "#mobile-menu-toggle",
        ".menu-toggle",
        ".hamburger",
        "[data-menu-toggle]"
    ]);

    const navigation = selectOne([
        "#mobile-menu",
        ".mobile-menu",
        ".nav-links",
        ".navigation-links",
        "[data-navigation]"
    ]);

    if (!menuButton || !navigation) return;

    menuButton.setAttribute("aria-expanded", "false");

    menuButton.addEventListener("click", () => {
        const isOpen = navigation.classList.toggle("is-open");

        navigation.classList.toggle("active", isOpen);
        menuButton.setAttribute("aria-expanded", String(isOpen));
    });

    navigation.querySelectorAll("a").forEach(link => {
        link.addEventListener("click", () => {
            navigation.classList.remove("is-open", "active");
            menuButton.setAttribute("aria-expanded", "false");
        });
    });
}

/* =========================================================
   3. SEARCH
========================================================= */

function initializeSearch() {
    const searchInput = selectOne([
        "#search-input",
        "#searchInput",
        "#marketplace-search",
        'input[type="search"]',
        'input[placeholder*="Search" i]',
        'input[placeholder*="search" i]'
    ]);

    if (!searchInput) return;

    const searchButton = selectOne([
        "#search-button",
        "#searchButton",
        ".search-button",
        "[data-search-button]"
    ]);

    const cards = selectAll([
        ".product-card",
        ".service-card",
        ".shop-card",
        ".category-card",
        "[data-searchable]"
    ]);

    function runSearch() {
        const query = searchInput.value.trim().toLowerCase();

        if (!query) {
            cards.forEach(card => {
                card.hidden = false;
            });

            return;
        }

        let matches = 0;

        cards.forEach(card => {
            const text = (
                card.innerText +
                " " +
                (card.dataset.searchable || "") +
                " " +
                (card.dataset.category || "")
            ).toLowerCase();

            const matched = text.includes(query);

            card.hidden = !matched;

            if (matched) matches++;
        });

        const resultContainer = selectOne([
            "#search-results",
            ".search-results",
            "[data-search-results]"
        ]);

        if (resultContainer) {
            resultContainer.textContent =
                matches === 0
                    ? "No matching items found."
                    : `${matches} matching item(s) found.`;
        }
    }

    searchInput.addEventListener("input", runSearch);

    searchInput.addEventListener("keydown", event => {
        if (event.key === "Enter") {
            event.preventDefault();
            runSearch();
        }
    });

    if (searchButton) {
        searchButton.addEventListener("click", event => {
            event.preventDefault();
            runSearch();
        });
    }
}

/* =========================================================
   4. CATEGORY FILTERS
========================================================= */

function initializeCategoryFilters() {
    const filterButtons = selectAll([
        "[data-filter]",
        ".filter-button",
        ".category-filter",
        ".filter-btn"
    ]);

    const cards = selectAll([
        ".product-card",
        ".service-card",
        ".shop-card",
        "[data-searchable]"
    ]);

    if (!filterButtons.length || !cards.length) return;

    filterButtons.forEach(button => {
        button.addEventListener("click", () => {
            const filter = (
                button.dataset.filter ||
                button.dataset.category ||
                button.textContent
            ).trim().toLowerCase();

            filterButtons.forEach(item => {
                item.classList.remove("active");
                item.setAttribute("aria-pressed", "false");
            });

            button.classList.add("active");
            button.setAttribute("aria-pressed", "true");

            cards.forEach(card => {
                const category = (
                    card.dataset.category ||
                    card.dataset.filter ||
                    ""
                ).toLowerCase();

                const text = card.innerText.toLowerCase();

                const showAll = [
                    "all",
                    "everything",
                    "all products",
                    "all services"
                ].includes(filter);

                const matched =
                    showAll ||
                    category.includes(filter) ||
                    filter.includes(category) ||
                    text.includes(filter);

                card.hidden = !matched;
            });
        });
    });
}

/* =========================================================
   5. SCROLL ANIMATIONS
========================================================= */

function initializeScrollAnimations() {
    const animatedElements = selectAll([
        "[data-aos]",
        ".reveal",
        ".animate-on-scroll"
    ]);

    if (!animatedElements.length) return;

    if (!("IntersectionObserver" in window)) {
        animatedElements.forEach(element => {
            element.classList.add("is-visible");
            element.style.opacity = "1";
            element.style.transform = "none";
        });

        return;
    }

    const observer = new IntersectionObserver(
        entries => {
            entries.forEach(entry => {
                if (!entry.isIntersecting) return;

                entry.target.classList.add("is-visible");
                entry.target.style.opacity = "1";
                entry.target.style.transform = "none";

                observer.unobserve(entry.target);
            });
        },
        {
            threshold: 0.08,
            rootMargin: "0px 0px 40px 0px"
        }
    );

    animatedElements.forEach(element => {
        observer.observe(element);
    });
}

/* =========================================================
   6. BACK TO TOP
========================================================= */

function initializeBackToTop() {
    const button = selectOne([
        "#back-to-top",
        ".back-to-top",
        "[data-back-to-top]"
    ]);

    if (!button) return;

    function updateButton() {
        button.hidden = window.scrollY < 400;
    }

    window.addEventListener("scroll", updateButton, {
        passive: true
    });

    button.addEventListener("click", () => {
        window.scrollTo({
            top: 0,
            behavior: "smooth"
        });
    });

    updateButton();
}

/* =========================================================
   7. CURRENT YEAR
========================================================= */

function initializeCurrentYear() {
    document.querySelectorAll("[data-current-year], #current-year")
        .forEach(element => {
            element.textContent = new Date().getFullYear();
        });
}

/* =========================================================
   8. BASIC FORM VALIDATION
========================================================= */

function initializeForms() {
    document.querySelectorAll("form[data-validate]")
        .forEach(form => {
            form.addEventListener("submit", event => {
                const requiredFields = [
                    ...form.querySelectorAll("[required]")
                ];

                let valid = true;

                for (const field of requiredFields) {
                    if (!field.checkValidity()) {
                        valid = false;
                        field.reportValidity();
                        break;
                    }
                }

                if (!valid) {
                    event.preventDefault();
                }
            });
        });

    document.querySelectorAll("[data-demo-action]")
        .forEach(button => {
            button.addEventListener("click", event => {
                event.preventDefault();

                showNotice(
                    "This feature will be available when its backend is connected.",
                    "info"
                );
            });
        });
}

/* =========================================================
   9. INTERACTIVE BUTTONS
========================================================= */

function initializeInteractiveButtons() {
    document.querySelectorAll("[data-action='login']")
        .forEach(button => {
            button.addEventListener("click", () => {
                window.location.href = "/login";
            });
        });

    document.querySelectorAll("[data-action='register']")
        .forEach(button => {
            button.addEventListener("click", () => {
                window.location.href = "/register";
            });
        });

    document.querySelectorAll("[data-action='get-started']")
        .forEach(button => {
            button.addEventListener("click", () => {
                window.location.href = "/register";
            });
        });

    document.querySelectorAll("[data-action='explore']")
        .forEach(button => {
            button.addEventListener("click", () => {
                const section = selectOne([
                    "#marketplace",
                    "#categories",
                    "#services"
                ]);

                if (section) {
                    section.scrollIntoView({
                        behavior: "smooth",
                        block: "start"
                    });
                }
            });
        });

    document.querySelectorAll("[data-action='order']")
        .forEach(button => {
            button.addEventListener("click", () => {
                showNotice(
                    "Order placement will be enabled when the order backend is connected.",
                    "info"
                );
            });
        });

    document.querySelectorAll("[data-action='book']")
        .forEach(button => {
            button.addEventListener("click", () => {
                showNotice(
                    "Service booking will be enabled when the booking backend is connected.",
                    "info"
                );
            });
        });
}

/* =========================================================
   10. SMOOTH SCROLLING
========================================================= */

function initializeSmoothScrolling() {
    document.querySelectorAll('a[href^="#"]').forEach(link => {
        link.addEventListener("click", event => {
            const href = link.getAttribute("href");

            if (!href || href === "#") return;

            const target = document.querySelector(href);

            if (!target) return;

            event.preventDefault();

            target.scrollIntoView({
                behavior: "smooth",
                block: "start"
            });

            if (window.location.hash !== href) {
                history.replaceState(null, "", href);
            }
        });
    });
}

/* =========================================================
   11. GLOBAL ERROR LOGGING
========================================================= */

window.addEventListener("error", event => {
    console.error(
        "[HostelHub JavaScript Error]",
        event.message,
        event.filename,
        event.lineno
    );
});

window.addEventListener("unhandledrejection", event => {
    console.error(
        "[HostelHub Promise Error]",
        event.reason
    );
});

// HostelHub: Hide page loader after the page loads
window.addEventListener("load", function () {
    const loader = document.getElementById("pageLoader");

    if (loader) {
        loader.classList.add("loader-hidden");
    }
});

// Fallback: Hide the loader even if another resource loads slowly
setTimeout(function () {
    const loader = document.getElementById("pageLoader");

    if (loader) {
        loader.classList.add("loader-hidden");
    }
}, 3000);

// HostelHub: Automatically hide the page loader
window.addEventListener("load", function () {
    const loader = document.getElementById("pageLoader");

    if (loader) {
        loader.classList.add("loader-hidden");
    }
});

// Fallback: Hide loader after 3 seconds
setTimeout(function () {
    const loader = document.getElementById("pageLoader");

    if (loader) {
        loader.classList.add("loader-hidden");
    }
}, 3000);



/* ==========================================
   HOSTELHUB: DARK MODE
========================================== */

(function () {
    const themeToggle = document.getElementById("themeToggle");

    if (!themeToggle) return;

    const themeIcon = themeToggle.querySelector("i");

    // Restore the saved theme
    const savedTheme = localStorage.getItem("hostelhub-theme");

    if (savedTheme === "dark") {
        document.body.classList.add("dark-mode");

        if (themeIcon) {
            themeIcon.classList.remove("fa-moon");
            themeIcon.classList.add("fa-sun");
        }
    }

    themeToggle.addEventListener("click", function () {
        const isDark = document.body.classList.toggle("dark-mode");

        localStorage.setItem(
            "hostelhub-theme",
            isDark ? "dark" : "light"
        );

        if (themeIcon) {
            themeIcon.classList.toggle("fa-moon", !isDark);
            themeIcon.classList.toggle("fa-sun", isDark);
        }
    });
})();


/* ==========================================
   HOSTELHUB: MOBILE MENU
========================================== */

(function () {
    const menuButton = document.getElementById("mobileMenuBtn");

    if (!menuButton) return;

    // On the signed-in student dashboard, route the shared header button to
    // the dashboard drawer instead of the hidden, empty guest navigation.
    const dashboardMenuButton = document.getElementById("hubMenuToggle");
    if (dashboardMenuButton) {
        menuButton.addEventListener("click", () => dashboardMenuButton.click());
        return;
    }

    // Locate the navigation associated with the menu button
    const header = menuButton.closest("header") || document;
    const navigation = header.querySelector(
        ".main-nav, .nav-links, .navigation-links, [data-navigation]"
    );

    if (!navigation) {
        console.warn("HostelHub: Navigation element not found.");
        return;
    }

    menuButton.addEventListener("click", function () {
        const isOpen = navigation.classList.toggle("is-open");

        navigation.classList.toggle("active", isOpen);
        menuButton.setAttribute("aria-expanded", String(isOpen));

        const icon = menuButton.querySelector("i");

        if (icon) {
            icon.classList.toggle("fa-bars", !isOpen);
            icon.classList.toggle("fa-xmark", isOpen);
        }
    });

    navigation.querySelectorAll("a").forEach(function (link) {
        link.addEventListener("click", function () {
            navigation.classList.remove("is-open", "active");
            menuButton.setAttribute("aria-expanded", "false");

            const icon = menuButton.querySelector("i");

            if (icon) {
                icon.classList.remove("fa-xmark");
                icon.classList.add("fa-bars");
            }
        });
    });
})();

/* ==================================
HOSTELHUB THREE-DOT MENU
================================== */

(function () {
const menuButton = document.getElementById("moreMenuBtn");
const dropdown = document.getElementById("moreMenuDropdown");
const themeButton = document.getElementById("moreMenuThemeBtn");
const mainThemeButton = document.getElementById("themeToggle");


if (!menuButton || !dropdown) return;

function closeMenu() {
    dropdown.hidden = true;
    menuButton.setAttribute("aria-expanded", "false");
}

menuButton.addEventListener("click", function (event) {
    event.stopPropagation();

    const shouldOpen = dropdown.hidden;
    dropdown.hidden = !shouldOpen;
    menuButton.setAttribute("aria-expanded", String(shouldOpen));
});

dropdown.addEventListener("click", function (event) {
    event.stopPropagation();
});

document.addEventListener("click", closeMenu);

document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
        closeMenu();
    }
});

if (themeButton && mainThemeButton) {
    themeButton.addEventListener("click", function () {
        mainThemeButton.click();
        closeMenu();
    });
}


})();


document.querySelectorAll(".login-role-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
        const role = tab.dataset.role;

        document.querySelectorAll(".login-role-tab").forEach((button) => {
            const active = button === tab;
            button.classList.toggle("active", active);
            button.setAttribute("aria-selected", String(active));
        });

        document.getElementById("loginRole").value = role;

        const titles = {
            user: "User Login",
            warden: "Warden Login",
            admin: "Admin Login",
            shopkeeper: "Shopkeeper Login"
        };

        document.getElementById("loginFormTitle").textContent = titles[role];

        const registerFooter = document.getElementById("registerFooter");
        registerFooter.style.display = role === "user" ? "block" : "none";
    });
});

/* ==========================================
HOSTELHUB PRODUCT SEARCH
========================================== */

document.addEventListener("DOMContentLoaded", function () {
const searchInput = document.getElementById("hubProductSearch");
const clearButton = document.getElementById("hubClearSearch");
const searchMessage = document.getElementById("hubSearchMessage");


if (!searchInput) return;

const productCards = Array.from(
    document.querySelectorAll("#section-shopping .hub-product-card")
);

function filterProducts() {
    const query = searchInput.value.trim().toLowerCase();
    let visibleCount = 0;

    productCards.forEach(function (card) {
        const searchableText = card.textContent.toLowerCase();
        const matches = searchableText.includes(query);

        card.classList.toggle("hub-search-hidden", !matches);

        if (matches) visibleCount++;
    });

    if (clearButton) {
        clearButton.style.display = query ? "inline-block" : "none";
    }

    if (searchMessage) {
        if (query && visibleCount === 0) {
            searchMessage.textContent =
                'No products found for "' + query + '". Try another name.';
        } else if (query) {
            searchMessage.textContent =
                visibleCount + " product(s) found.";
        } else {
            searchMessage.textContent = "";
        }
    }
}

searchInput.addEventListener("input", filterProducts);

if (clearButton) {
    clearButton.addEventListener("click", function () {
        searchInput.value = "";
        filterProducts();
        searchInput.focus();
    });
}


});


/* ==========================================
HOSTELHUB SHOPPING PAGE NAVIGATION
========================================== */

document.addEventListener("DOMContentLoaded", function () {
const shoppingHome = document.getElementById("hubShoppingHome");
const productsPage = document.getElementById("hubProductsPage");
const openProducts = document.getElementById("hubOpenProducts");
const backToShopping = document.getElementById("hubBackToShopping");


if (!shoppingHome || !productsPage) return;

function showShoppingHome() {
    shoppingHome.hidden = false;
    productsPage.hidden = true;
}

function showProductsPage() {
    shoppingHome.hidden = true;
    productsPage.hidden = false;

    const searchInput = document.getElementById("hubProductSearch");

    if (searchInput) {
        searchInput.focus();
    }

    productsPage.scrollIntoView({
        behavior: "smooth",
        block: "start"
    });
}

if (openProducts) {
    openProducts.addEventListener("click", showProductsPage);
}

if (backToShopping) {
    backToShopping.addEventListener("click", showShoppingHome);
}

showShoppingHome();

});




function initializePWAInstall() {
    const button = document.getElementById("installAppBtn");

    if (!button) return;

    let installPrompt = null;

    const isStandalone =
        window.matchMedia("(display-mode: standalone)").matches ||
        window.navigator.standalone === true;

    if (isStandalone) {
        button.innerHTML =
            '<i class="fa-solid fa-check"></i><span>Installed</span>';

        button.disabled = true;
        return;
    }

    window.addEventListener("beforeinstallprompt", (event) => {
        event.preventDefault();
        installPrompt = event;
    });

    window.addEventListener("appinstalled", () => {
        installPrompt = null;
        button.innerHTML = '<i class="fa-solid fa-check"></i><span>Installed</span>';
        button.disabled = true;
    });

    button.addEventListener("click", async () => {
        if (installPrompt) {
            installPrompt.prompt();

            await installPrompt.userChoice;

            installPrompt = null;
        } else {
            alert(
                "To install HostelHub, open the browser menu (⋮) " +
                "and choose Install app or Add to Home screen."
            );
        }
    });

    if ("serviceWorker" in navigator &&
        location.protocol === "https:") {

        navigator.serviceWorker
            .register("/service-worker.js")
            .catch(error => {
                console.error(
                    "Service Worker registration failed:",
                    error
                );
            });
    }
}

if (document.readyState === "loading") {
    document.addEventListener(
        "DOMContentLoaded",
        initializePWAInstall
    );
} else {
    initializePWAInstall();
}
