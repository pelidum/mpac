/**
 * Main application script.
 */
const chartColors = [
  "#CCA139", // 1. Brand Gold — primary anchor (unchanged)
  "#3F5E7A", // 2. True Steel Blue (clearly blue, no grey drift)
  "#5E7FA3", // 3. Light Desaturated Blue (same hue family, higher luminance)
  "#9B7A55", // 4. Burnt Bronze (warm contrast; replaces red)
  "#7F8F7A", // 5. Neutral Sage (green-leaning neutral, not blue-grey)
  "#8A84A6", // 6. Muted Purple Slate (separate axis from blue/grey)
  "#8F939A", // 7. Neutral Mid Grey (no blue cast — derived away from #c3cddc)
  "#BFA45A"  // 8. Antique Gold — highlight / threshold only
];



const App = {
  /**
   * Common functionalities
   */
  common: {
    init: function () {
      this.drawer = document.getElementById("navigation-drawer");
      this.mainContent = document.getElementById("main-content");
      this.overlay = document.getElementById("drawer-overlay");
      this.themeRoot = document.documentElement;
      this.themeToggleButton = document.querySelector(".theme-toggle");
      this.drawerToggleButtons =
        document.querySelectorAll(".drawer-toggle-btn");

      this.setInitialThemeIcon();
      this.setupEventListeners();
    },

    setupEventListeners: function () {
      this.drawerToggleButtons?.forEach((btn) =>
        btn.addEventListener("click", () => this.toggleDrawer()),
      );
      this.overlay?.addEventListener("click", () => this.toggleDrawer());
      this.themeToggleButton?.addEventListener("click", () => this.toggleTheme());
    },

    setInitialThemeIcon: function () {
      if (this.themeToggleButton) {
        if (this.themeRoot.classList.contains("dark")) {
          this.themeToggleButton.textContent = "brightness_2";
        } else {
          this.themeToggleButton.textContent = "wb_sunny";
        }
      }
    },

    toggleDrawer: function () {
      if (!this.drawer || !this.mainContent || !this.overlay) return;
      this.drawer.classList.toggle("open");
      this.mainContent.classList.toggle("drawer-open");
      this.overlay.classList.toggle("visible");
    },

    toggleTheme: function () {
      this.themeRoot.classList.toggle("dark");
      const newTheme = this.themeRoot.classList.contains("dark") ? "dark" : "light";
      localStorage.setItem("theme", newTheme);
      this.setInitialThemeIcon();
    },
  },
  /**
   * Utility functions
   */
  utils: {
    escapeHTML(s) {
      if (s == null) return "";
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    },
    getNested(obj, path, defaultValue = undefined) {
      if (!obj || typeof path !== 'string') return defaultValue;
      const parts = path.split('.');
      let current = obj;
      for (let i = 0; i < parts.length; i++) {
        if (current === null || typeof current !== 'object' || !parts[i]) return defaultValue;
        current = current[parts[i]];
      }
      return current !== undefined ? current : defaultValue;
    },
    formatDate(timestamp) {
      if (!timestamp) return "N/A";
      try {
        const date = new Date(timestamp);
        if (isNaN(date.getTime())) return "N/A";
        return date.toLocaleString();
      } catch (e) {
        console.error("Error formatting date:", timestamp, e);
        return "Invalid Date";
      }
    },
    formatNum(num) {
      const n = parseInt(num, 10);
      return isNaN(n) ? '0' : n.toLocaleString();
    },
    formatCost(cost) {
      const c = parseFloat(cost);
      return isNaN(c) ? '$0.00000' : `$${c.toFixed(5)}`;
    },
    getRuntime(start, end) {
      if (!start || !end) return 'N/A';
      const s = new Date(start).getTime();
      const e = new Date(end).getTime();
      if (isNaN(s) || isNaN(e)) return 'N/A';
      return `${((e - s) / 1000).toFixed(2)} seconds`;
    },
    renderStatusBadge(status) {
      status = String(status || '').toUpperCase();
      let icon = 'help_outline';
      let text = 'Unknown';
      let classes = 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-200';

      switch (status) {
        case 'COMPLETED': case '1':
          icon = 'check_circle'; text = 'Completed';
          classes = 'bg-green-100 text-green-800 dark:bg-green-900/20 dark:text-green-200';
          break;
        case 'ERROR': case '2':
          icon = 'error'; text = 'Error';
          classes = 'bg-red-100 text-red-800 dark:bg-red-900/20 dark:text-red-200';
          break;
        case 'CANCELLED':
          icon = 'cancel'; text = 'Cancelled';
          classes = 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/20 dark:text-yellow-200';
          break;
        case 'RUNNING': case 'IN_PROGRESS': case '3':
          icon = 'hourglass_top'; text = 'In Progress';
          classes = 'bg-blue-100 text-blue-800 dark:bg-blue-900/20 dark:text-blue-200';
          break;
      }

      return `
            <span class="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-medium ${classes}">
                <span class="material-icons text-[16px]">${icon}</span>
                ${text}
            </span>
        `;
    }
  },

  /**
   * Data-driven table sorting.
   */
  sortable: {
    init: function (table, pageModule) {
      if (!table || !pageModule || typeof pageModule.renderTable !== 'function') {
        console.warn("Sortable init failed: Invalid table or page module.", table, pageModule);
        return;
      }
      this.attachSorter(table, pageModule);
    },

    keyFromValue: function (raw, type) {
      const value = raw === null || raw === undefined ? "" : String(raw).trim();
      if (type === "num") {
        const n = parseFloat(value.replace(/,/g, ""));
        return isNaN(n) ? Number.NEGATIVE_INFINITY : n;
      }
      if (type === "date") {
        const t = Date.parse(value);
        return isNaN(t) ? 0 : t;
      }
      return value.toLowerCase();
    },

    attachSorter: function (table, pageModule) {
      if (!table.tHead || table.tHead.rows.length === 0 || table.dataset.sortInit === "1") return;
      table.dataset.sortInit = "1";

      const headers = table.tHead.rows[0].cells;
      const util = this;

      Array.from(headers).forEach((th) => {
        const prop = th.dataset.prop;
        if (!prop || th.hasAttribute("data-nosort")) return;

        th.classList.add("th-sortable");
        th.setAttribute("aria-sort", "none");

        let btn = th.querySelector(".th-sort-btn");
        let sortIcon;
        if (!btn) {
          btn = document.createElement("button");
          btn.type = "button";
          btn.className = "th-sort-btn";
          while (th.firstChild) btn.appendChild(th.firstChild);
          sortIcon = document.createElement("span");
          sortIcon.className = "material-icons sort-icon";
          sortIcon.textContent = "unfold_more";
          btn.appendChild(sortIcon);
          th.appendChild(btn);
        } else {
          sortIcon = btn.querySelector(".sort-icon");
        }

        const applySort = () => {
          const currentOrder = th.getAttribute("aria-sort");
          const asc = currentOrder !== "ascending";
          const type = th.dataset.type || "text";

          if (!Array.isArray(pageModule.filtered)) {
            console.error("pageModule.filtered is not an array, cannot sort.");
            return;
          }

          pageModule.filtered.sort((a, b) => {
            const valA = util.keyFromValue(App.utils.getNested(a, prop), type);
            const valB = util.keyFromValue(App.utils.getNested(b, prop), type);
            if (typeof valA !== typeof valB) {
              if (typeof valA === 'number') return asc ? -1 : 1;
              if (typeof valB === 'number') return asc ? 1 : -1;
            }
            if (valA < valB) return asc ? -1 : 1;
            if (valA > valB) return asc ? 1 : -1;
            return 0;
          });

          Array.from(headers).forEach((h) => {
            const otherBtn = h.querySelector(".th-sort-btn");
            const otherIcon = otherBtn?.querySelector(".sort-icon");
            if (h !== th) {
              h.setAttribute("aria-sort", "none");
              if (otherIcon) otherIcon.textContent = "unfold_more";
            }
          });

          th.setAttribute("aria-sort", asc ? "ascending" : "descending");
          if (sortIcon) sortIcon.textContent = asc ? "arrow_drop_up" : "arrow_drop_down";

          pageModule.currentPage = 1;
          pageModule.renderTable();
        };

        // Define named handler for keydown to allow removal
        const handleKeyDown = (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            applySort();
          }
        };

        // Remove potentially existing listeners before adding new ones
        btn.removeEventListener("click", applySort);
        btn.removeEventListener("keydown", handleKeyDown);

        btn.addEventListener("click", applySort);
        btn.addEventListener("keydown", handleKeyDown);
      });
    },
  },
  /**
   * Reusable logic for expanding/collapsing table rows.
   */
  dropdown: {
    init() {
      // Use event delegation on document body, no need for specific element check
      this.setupEventListeners();
    },

    setupEventListeners() {
      document.body.addEventListener("click", (event) => {
        const cell = event.target.closest("td.description-cell.expandable");
        if (!cell) return;
        if (event.target.closest("a, button")) return;
        const icon = cell.querySelector(".expand-toggle");
        if (!icon) return;
        const isExpanded = cell.classList.toggle("is-expanded");
        cell.setAttribute("aria-expanded", String(isExpanded));
      });

      document.body.addEventListener("keydown", (e) => {
        const cell = document.activeElement?.closest?.("td.description-cell.expandable");
        if (!cell || (e.key !== "Enter" && e.key !== " ")) return;
        e.preventDefault();
        const icon = cell.querySelector(".expand-toggle");
        if (!icon) return;
        const isExpanded = cell.classList.toggle("is-expanded");
        cell.setAttribute("aria-expanded", String(isExpanded));
      });
    },
  },

  /**
   * Models page logic
   */
  models: {
    all: [],
    filtered: [],
    currentPage: 1,
    rowsPerPage: 100,
    elements: {},
    filterConfig: [
      { id: "filterModelProvider", param: "provider", prop: "provider", el: null, type: "select" },
      { id: "filterModelFamily", param: "family", prop: "family", el: null, type: "select" },
      { id: "filterModelQuantization", param: "quantization", prop: "quantization", el: null, type: "select" },
      { id: "filterCapabilitiesModality", param: "modality", prop: "capabilities.modality", el: null, type: "select" },
      { id: "filterLicense", param: "license", prop: "license", el: null, type: "select" },
    ],

    init: function (modelsData) {
      this.all = modelsData || [];
      if (!this.cacheElements()) return;
      this.setupEventListeners();
      this.populateFilters();
      this.setFiltersFromURL();
      this.applyFilters();
      const table = this.elements.tableBody?.closest("table");
      if (table) App.sortable.init(table, this);
    },

    cacheElements: function () {
      this.elements = {
        tableBody: document.getElementById("modelsTableBody"),
        modelCount: document.getElementById("model-count"),
        pageInfo: document.getElementById("model-page-info"),
        prevPageBtn: document.getElementById("model-prev-page-btn"),
        nextPageBtn: document.getElementById("model-next-page-btn"),
      };
      let allFound = Object.values(this.elements).every(el => el !== null);
      this.filterConfig.forEach(config => {
        config.el = document.getElementById(config.id);
        if (!config.el) allFound = false;
      });
      if (!allFound) console.warn("Models: Not all required elements found.");
      return allFound;
    },

    setupEventListeners: function () {
      this.elements.prevPageBtn?.addEventListener("click", () => this.changePage(-1));
      this.elements.nextPageBtn?.addEventListener("click", () => this.changePage(1));
      this.filterConfig.forEach(config => {
        config.el?.addEventListener("change", this.applyFilters.bind(this));
      });
    },

    setFiltersFromURL: function () {
      const params = new URLSearchParams(window.location.search);
      this.filterConfig.forEach(config => {
        const value = params.get(config.param);
        if (value && config.el && Array.from(config.el.options).some(opt => opt.value === value)) {
          config.el.value = value;
        }
      });
    },

    populateFilters: function () {
      const createOptions = (element, prop) => {
        const values = new Set(this.all.map(m => App.utils.getNested(m, prop)).filter(Boolean));
        while (element.options.length > 1) element.remove(1);
        Array.from(values).sort((a, b) => String(a).localeCompare(String(b), undefined, { sensitivity: 'base' })).forEach(val => {
          element.add(new Option(val, val));
        });
      };
      this.filterConfig.forEach(config => {
        if (config.type === 'select' && config.el) createOptions(config.el, config.prop);
      });
    },

    applyFilters: function () {
      const params = new URLSearchParams();
      const filterValues = {};
      this.filterConfig.forEach(config => {
        const value = config.el?.value;
        if (value && value !== "All") {
          filterValues[config.id] = value;
          params.set(config.param, value);
        }
      });
      history.pushState({ path: `${window.location.pathname}?${params}` }, '', `${window.location.pathname}?${params}`);

      this.filtered = this.all.filter(model => {
        return this.filterConfig.every(config => {
          const filterVal = filterValues[config.id];
          if (!filterVal) return true;
          const modelVal = App.utils.getNested(model, config.prop);
          return modelVal !== null && modelVal !== undefined && String(modelVal) === filterVal;
        });
      });
      this.currentPage = 1;
      this.renderTable();
    },

    renderTable: function () {
      if (!this.elements.tableBody) return;
      const totalPages = Math.ceil(this.filtered.length / this.rowsPerPage);
      this.currentPage = Math.max(1, Math.min(this.currentPage, totalPages || 1));
      const startIndex = (this.currentPage - 1) * this.rowsPerPage;
      const modelsToDisplay = this.filtered.slice(startIndex, startIndex + this.rowsPerPage);

      this.elements.tableBody.innerHTML = modelsToDisplay.length > 0
        ? modelsToDisplay.map((model) => {
          const safeDesc = App.utils.escapeHTML(model.description || "");
          const hasDesc = !!safeDesc;
          const displayDesc = hasDesc ? (safeDesc.length > 150 ? safeDesc.slice(0, 150) + "…" : safeDesc) : "N/A";
          const render = (path, fmt = null, def = "N/A") => {
            let v = App.utils.getNested(model, path, def);
            if (v === null || v === "") return def;
            return fmt ? fmt(v) : App.utils.escapeHTML(v);
          };
          const fmtCost = c => `$${parseFloat(c).toFixed(5)}`;
          const fmtNum = n => parseInt(n, 10).toLocaleString();

          return `
                  <tr>
                    <td data-col="provider">${render("provider")}</td>
                    <td data-col="id">${render("id")}</td>
                    <td data-col="family">${render("family")}</td>
                    <td data-col="params">${render("parameters_label")}</td>
                    <td data-col="context" class="text-right">${render("capabilities.context_window_length", fmtNum, '0')}</td>
                    <td data-col="modality">${render("capabilities.modality")}</td>
                    <td data-col="in_cost" class="text-right">${render("pricing.input_token_cost", fmtCost, '$0.00000')}</td>
                    <td data-col="out_cost" class="text-right">${render("pricing.output_token_cost", fmtCost, '$0.00000')}</td>
                    <td data-col="actions" class="whitespace-nowrap text-center">
                      <a href="/models/${encodeURIComponent(model.id || '')}" class="action-button view" title="View Details"><span class="material-icons">visibility</span></a>
                    </td>
                  </tr>`;
        }).join("")
        : `<tr><td colspan="10" class="text-center py-8 text-muted">No models match the selected criteria.</td></tr>`;
      this.updatePagination(totalPages);
    },

    updatePagination: function (totalPages) {
      if (this.elements.modelCount) this.elements.modelCount.textContent = `${this.filtered.length} model${this.filtered.length !== 1 ? 's' : ''}`;
      if (this.elements.pageInfo) this.elements.pageInfo.textContent = `Page ${totalPages > 0 ? this.currentPage : 0} of ${totalPages}`;
      if (this.elements.prevPageBtn) this.elements.prevPageBtn.disabled = this.currentPage <= 1;
      if (this.elements.nextPageBtn) this.elements.nextPageBtn.disabled = this.currentPage >= totalPages || totalPages === 0;
    },

    changePage: function (direction) {
      const totalPages = Math.ceil(this.filtered.length / this.rowsPerPage);
      const newPage = this.currentPage + direction;
      if (newPage >= 1 && newPage <= totalPages && newPage !== this.currentPage) {
        this.currentPage = newPage;
        this.renderTable();
      }
    },
  },

  /**
   * Reusable logic for dropdown buttons
   */
  dropdownButton: {
    init: function (buttonId, dropdownId) {
      const button = document.getElementById(buttonId);
      const dropdown = document.getElementById(dropdownId);

      if (!button || !dropdown) return;

      const toggle = (event) => {
        event.stopPropagation();
        dropdown.classList.toggle('hidden');
        button.setAttribute('aria-expanded', !dropdown.classList.contains('hidden'));
      };

      const close = (event) => {
        if (!button.contains(event.target) && !dropdown.contains(event.target)) {
          dropdown.classList.add('hidden');
          button.setAttribute('aria-expanded', 'false');
        }
      };

      button.addEventListener('click', toggle);
      document.addEventListener('click', close);
    }
  },

  /**
   * Main initializer
   */
  run: function () {
    this.common.init();
    this.dropdown.init();

    if (document.getElementById("modelsTableBody") && typeof pageData?.models !== "undefined") {
      this.models.init(pageData.models);
    }
  },
};

document.addEventListener("DOMContentLoaded", () => App.run());