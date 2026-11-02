/* Navigation only. Collection, model calls, preview and export stay in the legacy app. */
(() => {
  "use strict";
  if (window.WorkspaceShell && typeof window.WorkspaceShell.init === "function") {
    window.WorkspaceShell.init();
    return;
  }

  const pages = {
    collect: { title: "商品宣传与体验", accent: "落差分析", subtitle: "看清宣传与真实体验的距离，让每次购买更有依据。", eyebrow: "商品分析工作区" },
    reports: { title: "可视化报告", subtitle: "查看风险方面、独立支持评论和可展开的分析依据。", eyebrow: "分析结果" },
    files: { title: "文件与数据", subtitle: "在应用内预览，选择需要保留的文件再导出。", eyebrow: "预览与导出" },
    history: { title: "历史任务", subtitle: "重新打开已有评论与分析，继续有依据的判断。", eyebrow: "任务记录" },
    settings: { title: "API 设置", subtitle: "连接你自己的模型接口，密钥由你自主配置。", eyebrow: "应用设置" },
    help: { title: "使用帮助", subtitle: "了解采集、分析、平台验证和文件导出流程。", eyebrow: "使用指南" },
  };
  const aliases = { report: "reports", collection: "collect", artifacts: "files" };
  const bound = new WeakSet();
  const shellState = { view: "collect", artifacts: [], artifactId: "", productId: "", focused: false,
    collection: null, initialized: false, cancellingFocus: false };
  const byId = id => document.getElementById(id);
  const text = value => typeof value === "string" || typeof value === "number" ? String(value) : "";
  const nodes = selector => Array.from(document.querySelectorAll(selector));

  function legacyState() {
    try { return typeof state === "object" && state ? state : null; }
    catch { return null; }
  }
  function setHidden(node, hidden) {
    if (!node) return;
    node.hidden = hidden;
    node.classList.toggle("hidden", hidden);
  }
  function setText(id, value) { const node = byId(id); if (node) node.textContent = text(value); }
  function isReport(artifact) {
    return text(artifact.id).startsWith("report_") || ["html", "pdf"].includes(text(artifact.format).toLowerCase());
  }
  const artifactPage = artifact => isReport(artifact) ? "reports" : "files";
  const artifactsFor = view => shellState.artifacts.filter(artifact => artifactPage(artifact) === view);
  function activeId() { return text(legacyState()?.artifactId) || shellState.artifactId; }
  function activeArtifact() {
    return shellState.artifacts.find(artifact => text(artifact.id) === activeId());
  }
  function preferredArtifact(view) {
    const available = artifactsFor(view);
    const order = view === "reports" ? ["report_html", "report_pdf"] : ["reviews_xlsx", "reviews_json", "product_json"];
    return order.map(id => available.find(artifact => artifact.id === id)).find(Boolean) || available[0];
  }
  function closeMenu() {
    byId("appShell")?.classList.remove("sidebar-open");
    byId("mobileMenuBtn")?.setAttribute("aria-expanded", "false");
  }

  function renderAvailability() {
    const collectionAvailable = Boolean(shellState.collection);
    const shell = byId("appShell");
    if (shell) shell.dataset.hasCollection = String(collectionAvailable);
    setHidden(byId("reportsEmpty"), artifactsFor("reports").length > 0);
    setHidden(byId("filesEmpty"), artifactsFor("files").length > 0 || collectionAvailable);
    setHidden(byId("sidebarArtifactSection"), false);
    setHidden(byId("artifactEmpty"), shellState.artifacts.length > 0);
    setHidden(byId("rawReviewsPanel"), !collectionAvailable);
    // The existing app owns artifactWorkspace.hidden/class.hidden and iframe contents.
    const workspace = byId("artifactWorkspace");
    if (workspace) workspace.setAttribute("aria-hidden", String(!["reports", "files"].includes(shellState.view) || !shellState.artifacts.length));
    const tabs = byId("artifactTabs");
    if (tabs) Array.from(tabs.querySelectorAll(".artifact-tab")).forEach(button => {
      const artifact = shellState.artifacts.find(item => text(item.id) === text(button.dataset.artifactId));
      if (!artifact) return;
      button.dataset.artifactPage = artifactPage(artifact);
      button.dataset.format = text(artifact.format).toUpperCase();
      button.classList.toggle("active", artifact.id === activeId());
      button.setAttribute("aria-selected", String(artifact.id === activeId()));
    });
  }

  function renderRoute() {
    const shell = byId("appShell");
    if (!shell) return;
    shell.dataset.view = shellState.view;
    nodes(".view-panel[data-page]").forEach(panel => {
      const active = panel.dataset.page === shellState.view;
      setHidden(panel, !active);
      panel.setAttribute("aria-hidden", String(!active));
    });
    nodes("button.nav-item[data-view]").forEach(button => {
      const active = button.dataset.view === shellState.view;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const copy = pages[shellState.view];
    const title = byId("pageTitle");
    if (title) {
      title.textContent = copy.title;
      if (copy.accent) {
        const accent = document.createElement("span");
        accent.className = "title-accent";
        accent.textContent = copy.accent;
        title.appendChild(accent);
      }
    }
    setText("pageSubtitle", copy.subtitle);
    setText("pageEyebrow", copy.eyebrow);
    renderAvailability();
  }

  function navigate(requested, options = {}) {
    const view = aliases[requested] || requested;
    if (!Object.prototype.hasOwnProperty.call(pages, view)) return false;
    if (!["reports", "files"].includes(view) && options.manageFocus !== false
        && (shellState.focused || document.body.classList.contains("report-focus"))) {
      shellState.cancellingFocus = true;
      try {
        if (typeof setReportFocus === "function") setReportFocus(false);
      } finally {
        shellState.focused = false;
        shellState.cancellingFocus = false;
      }
    }
    shellState.view = view;
    if (view === "settings" && byId("apiSettings")) byId("apiSettings").open = true;
    closeMenu();
    renderRoute();
    if (options.selectArtifact !== false && ["reports", "files"].includes(view)) {
      const preferred = preferredArtifact(view);
      if (preferred && activeId() !== text(preferred.id) && typeof previewArtifact === "function") previewArtifact(preferred.id);
    }
    if (view === "history" && options.refreshHistory !== false && typeof refreshHistoryJobs === "function") {
      const result = refreshHistoryJobs();
      if (result && typeof result.catch === "function") result.catch(() => {});
    }
    return true;
  }

  function healthChanged(value = {}) {
    const ok = Boolean(value.ok);
    setText("sidebarHealthText", ok ? "采集浏览器已连接" : "采集浏览器待启动");
    setText("sidebarBrowser", text(value.browser) || "独立采集环境");
    setText("sidebarVersion", value.version ? "v" + text(value.version).replace(/^v/i, "") : "");
    const version = byId("sidebarVersion");
    if (version) version.hidden = !value.version;
    const dot = byId("sidebarHealthDot");
    if (dot) {
      dot.classList.toggle("ok", ok);
      dot.classList.toggle("bad", !ok);
      dot.dataset.status = ok ? "connected" : "idle";
    }
  }

  function artifactsChanged(value = {}) {
    const legacy = legacyState();
    const productId = text(value.productId) || text(legacy?.artifactProductId);
    if (productId && legacy?.productId && productId !== text(legacy.productId)) return false;
    const hadArtifacts = shellState.artifacts.length > 0;
    const input = Array.isArray(value.artifacts) ? value.artifacts : legacy?.artifacts;
    shellState.artifacts = Array.isArray(input) ? input.filter(item => item && typeof item.id === "string" && item.id) : [];
    shellState.productId = productId;
    if (!shellState.artifacts.some(item => item.id === shellState.artifactId)) shellState.artifactId = "";
    if (!hadArtifacts && shellState.artifacts.length) {
      const raw = byId("rawReviewsPanel");
      const detail = raw && typeof raw.querySelector === "function" ? raw.querySelector("details") : null;
      if (detail) detail.open = false;
    }
    renderAvailability();
    return true;
  }

  function artifactChanged(artifact) {
    if (!artifact || !text(artifact.id)) return;
    if (!shellState.artifacts.some(item => item.id === artifact.id)) artifactsChanged();
    shellState.artifactId = text(artifact.id);
    navigate(artifactPage(artifact), { selectArtifact: false, refreshHistory: false });
  }

  function focusChanged(focused) {
    const wasFocused = shellState.focused;
    shellState.focused = Boolean(focused);
    if (shellState.cancellingFocus) return;
    if (focused) {
      const artifact = activeArtifact() || preferredArtifact("reports") || preferredArtifact("files");
      navigate(artifact ? artifactPage(artifact) : "reports", { selectArtifact: false, manageFocus: false });
    } else if (wasFocused) navigate("collect", { selectArtifact: false, manageFocus: false });
  }

  function collectionChanged(value = {}) {
    const legacy = legacyState();
    const jobId = text(value.jobId), productId = text(value.productId);
    if (!jobId || !productId || (legacy?.jobId && jobId !== text(legacy.jobId))
        || (legacy?.productId && productId !== text(legacy.productId))) return false;
    shellState.collection = { jobId, productId, reviewCount: Number.isFinite(Number(value.reviewCount)) ? Math.max(0, Number(value.reviewCount)) : 0 };
    renderAvailability();
    return true;
  }

  function reset() {
    shellState.artifacts = [];
    shellState.artifactId = "";
    shellState.productId = "";
    renderAvailability();
  }
  function resetCollection() {
    shellState.collection = null;
    renderAvailability();
  }

  function bindNavigation() {
    nodes("button.nav-item[data-view]").forEach(button => {
      if (bound.has(button)) return;
      bound.add(button);
      button.addEventListener("click", event => { event.preventDefault(); navigate(button.dataset.view); });
    });
    nodes("[data-action]").forEach(button => {
      if (bound.has(button)) return;
      const action = aliases[button.dataset.action] || button.dataset.action;
      if (!Object.prototype.hasOwnProperty.call(pages, action)) return;
      bound.add(button);
      button.addEventListener("click", event => { event.preventDefault(); navigate(action); });
    });
    const menu = byId("mobileMenuBtn");
    if (menu && !bound.has(menu)) {
      bound.add(menu);
      menu.addEventListener("click", () => {
        const open = byId("appShell").classList.toggle("sidebar-open");
        menu.setAttribute("aria-expanded", String(open));
      });
    }
  }

  function init() {
    if (!byId("appShell")) return false;
    bindNavigation();
    if (!shellState.initialized) {
      shellState.initialized = true;
      const initial = byId("appShell").dataset.view;
      if (Object.prototype.hasOwnProperty.call(pages, initial)) shellState.view = initial;
      shellState.focused = document.body.classList.contains("report-focus");
      const legacy = legacyState();
      if (legacy?.artifacts?.length) {
        artifactsChanged({ artifacts: legacy.artifacts, productId: legacy.artifactProductId || legacy.productId });
        shellState.artifactId = text(legacy.artifactId);
        const artifact = activeArtifact();
        if (artifact) shellState.view = artifactPage(artifact);
      }
      if (shellState.focused) shellState.view = artifactPage(activeArtifact() || preferredArtifact("reports") || { id: "report_html" });
    }
    renderRoute();
    return true;
  }

  window.WorkspaceShell = { navigate, healthChanged, artifactsChanged, artifactChanged, focusChanged,
    collectionChanged, reset, resetCollection, init };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
