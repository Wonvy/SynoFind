"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const paths = {
    folder: ["M3 7a2 2 0 0 1 2-2h5l2 2h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z", "M3 10h18"],
    search: ["M20 20l-4.8-4.8", "M10.5 17a6.5 6.5 0 1 0 0-13 6.5 6.5 0 0 0 0 13"],
    settings: ["M9.5 4.5l.5-2h4l.5 2 2 1.2 2-.6 2 3.5-1.5 1.4v2.3l1.5 1.4-2 3.5-2-.6-2 1.2-.5 2h-4l-.5-2-2-1.2-2 .6-2-3.5 1.5-1.4V10L3.5 8.6l2-3.5 2 .6Z", "M15 11a3 3 0 1 0-6 0 3 3 0 0 0 6 0"],
    power: ["M12 3v8", "M7.5 5.5a8 8 0 1 0 9 0"],
    filter: ["M4 6h16M7 12h10M10 18h4"],
    sort: ["M8 4v16m-3-3 3 3 3-3", "M13 5h7m-7 5h5m-5 5h3"],
    "chevron-down": ["m6 9 6 6 6-6"],
    "chevron-left": ["m15 5-7 7 7 7"],
    "chevron-right": ["m9 5 7 7-7 7"],
    close: ["m6 6 12 12M18 6 6 18"],
    copy: ["M9 9h11v12H9Z", "M15 9V3H3v12h6"],
    eye: ["M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12", "M15 12a3 3 0 1 0-6 0 3 3 0 0 0 6 0"],
    shield: ["m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z", "m8 12 3 3 5-6"],
    lock: ["M5 10h14v11H5Z", "M8 10V7a4 4 0 0 1 8 0v3", "M12 14v3"],
    link: ["m10 13 4-4", "M8.5 15.5 7 17a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0", "M12.5 8.5 14 7a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"],
    warning: ["m12 3 10 18H2Z", "M12 9v5m0 3v.1"],
    file: ["M5 3h9l5 5v13H5Z", "M14 3v6h5M8 13h8M8 17h6"],
  };
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    for (const d of paths[name] || paths.file) {
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("d", d);
      svg.append(path);
    }
    return svg;
  }
  document.querySelectorAll("[data-icon]").forEach((node) => node.append(icon(node.dataset.icon)));

  const state = { csrf: "", status: null, searching: false, connecting: false, stopped: false, query: null, results: null, previewAbort: null, searchAbort: null, poll: null, toastTimer: null, connectionStage: "login", connectionEpoch: 0, selectionId: null, selectionDeadline: 0, selectionTimer: null, shares: [], smbEdited: false, view: "details", selectedPath: null, previewFile: null, nativeBusy: false };
  const defaults = { url: "", username: "", root: "", smb_host: "", local_root: "", preview_mode: "web", preview_executable: "" };
  const imageExtensions = new Set(["jpg", "jpeg", "png", "gif", "webp", "bmp"]);
  const textExtensions = new Set(["txt", "md", "log", "csv", "json", "yaml", "yml", "toml", "ini", "xml", "py", "js", "ts", "css", "go"]);
  const MAX_TEXT = 2 * 1024 * 1024;
  const MAX_IMAGE = 10 * 1024 * 1024;
  const MAX_PDF = 20 * 1024 * 1024;
  let pdfModulePromise = null;
  const integerFormatter = new Intl.NumberFormat("zh-CN");
  const dateFormatter = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  class APIError extends Error {
    constructor(message, status, code, apiName, method) { super(message); this.status = status; this.code = code; this.api = typeof apiName === "string" ? apiName : ""; this.method = typeof method === "string" ? method : ""; }
  }

  async function api(path, options = {}) {
    if (state.stopped) throw new Error("应用已退出，请重新打开 SynoFind。");
    const headers = { Accept: "application/json", "X-SynoFind-CSRF": state.csrf };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(path, { method: options.method || "GET", credentials: "same-origin", cache: "no-store", headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), signal: options.signal, keepalive: !!options.keepalive });
    let data;
    try { data = await response.json(); } catch { throw new APIError("本机服务返回了无法识别的内容，请重新打开应用。", response.status); }
    if (!response.ok) {
      const error = new APIError(typeof data?.error?.message === "string" ? data.error.message : "操作未完成，请稍后重试。", response.status, data?.error?.code, data?.error?.api, data?.error?.method);
      if (response.status === 401 && state.status?.connected && !path.startsWith("/api/connection/")) renderStatus({ ...state.status, connected: false, last_error: message(error) });
      throw error;
    }
    return data;
  }
  function message(error) {
    const text = error instanceof TypeError ? "无法连接本机服务。请确认 SynoFind 仍在运行，然后重试。" : error?.message || "操作未完成，请稍后重试。";
    const code = typeof error?.code === "number" && Number.isFinite(error.code) ? String(error.code) : typeof error?.code === "string" && /^[A-Za-z0-9_-]{1,32}$/.test(error.code) ? error.code : "";
    return code && !text.includes("错误码 " + code) ? `${text}（错误码 ${code}）` : text;
  }
  function connectionMessage(error) {
    const text = message(error);
    // The same numeric DSM error can mean different things for login and file
    // access. Only explain a failed password check when the source is known.
    const isAuth = error?.api === "SYNO.API.Auth" || (!error?.api && error?.status === 401);
    const code = Number(error?.code);
    if (error?.api?.startsWith("SYNO.FileStation")) return `群晖登录已完成，但文件库访问检查未通过。${text} 请检查提示后重试。`;
    if (isAuth && code === 400) return `${text} 请检查用户名和密码后重新连接。`;
    if (isAuth && [403, 404, 406].includes(code)) return `${text} 请重新输入密码和当前两步验证码后连接。`;
    if (isAuth && code === 401) return `${text} 请检查此群晖账户是否已启用。`;
    return `${text} 请检查提示后重试。`;
  }
  function showToast(text) { clearTimeout(state.toastTimer); $("toast").textContent = text; $("toast").hidden = false; state.toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3200); }
  function showNotice(text, action = "") { $("global-notice-text").textContent = text; $("global-notice").hidden = !text; $("notice-action").hidden = !action; $("notice-action").textContent = action; }
  function clearResults() { clearTimeout(liveTimer); clearHover(); $("live-results-hint").hidden = true; state.query = null; state.results = null; state.selectedPath = null; $("file-rows").replaceChildren(); $("results-count").hidden = true; $("table-wrap").hidden = true; $("pagination").hidden = true; $("search-error").hidden = true; renderEmpty(); }
  function statusError(status) { return status?.last_error ? message({ message: status.last_error, code: status.last_error_code }) : ""; }

  function renderStatus(status) {
    state.status = { ...status, local_sources: status.local_sources || state.status?.local_sources || [] };
    status = state.status; renderSources();
    const profile = { ...defaults, ...(status.profile || {}) };
    $("connection-pill").classList.toggle("connected", !!status.connected);
    $("connection-label").textContent = status.connected ? "已连接群晖" : status.local_sources?.length ? "本机文件库就绪" : status.configured ? "NAS 尚未连接" : "等待添加数据源";
    const name = String(profile.root || "").split("/").filter(Boolean).pop() || "尚未选择共享文件夹";
    $("library-name").textContent = status.local_sources?.length ? `${status.local_sources.length} 个本机文件夹${status.connected ? " + NAS" : ""}` : name;
    $("library-name").title = profile.root || name;
    $("library-name").classList.toggle("unconfigured-library", !profile.root && !status.local_sources?.length);
    $("profile-url").textContent = profile.url;
    $("profile-username").textContent = profile.username;
    $("profile-root").textContent = profile.root || "尚未选择共享文件夹";
    $("disconnect-button").hidden = !status.connected;
    $("forget-button").hidden = !status.remembered;
    $("saved-connection").hidden = !status.remembered || ["select", "library"].includes(state.connectionStage);
    $("reconnect-button").hidden = !!status.connected;
    $("connection-state-text").textContent = status.connected ? `已连接 ${profile.username || "群晖账户"}` : "凭据仅用于连接你的群晖。";
    $("connection-remember").disabled = status.can_remember === false;
    if (status.can_remember === false) { $("connection-remember").checked = false; $("remember-description").textContent = "当前系统不支持 Windows 用户加密保存，本次连接结束后需要重新输入。"; }
    else $("remember-description").textContent = "仅当前 Windows 用户加密保存；不保存两步验证码。";
    if (status.connected) showNotice("");
    else if (status.last_error) showNotice(statusError(status), status.remembered ? "重新连接" : "连接设置");
    else if (status.configured) showNotice("尚未连接群晖。连接后即可搜索和获取文件。", status.remembered ? "重新连接" : "连接设置");
    else showNotice("");
    setControls();
    if (!state.results && !state.searching) renderEmpty();
  }
  function setControls() {
    $("search-button").disabled = state.connecting || !hasSources() || state.stopped;
    $("stop-search").hidden = !state.searching && !state.stoppingSearch;
    $("stop-search").disabled = !!state.stoppingSearch;
    $("stop-search").textContent = state.stoppingSearch ? "正在停止…" : "停止搜索";
    $("search-button-label").textContent = state.searching ? "正在搜索" : "搜索文件";
    $("sort-select").disabled = state.searching || state.stopped;
    const busy = state.connecting || state.searching || state.stopped;
    const choosing = ["select", "library"].includes(state.connectionStage);
    $("connect-button").disabled = busy || (choosing && !$("connection-root").value);
    $("connect-button-label").textContent = state.connecting ? "正在处理…" : state.connectionStage === "login" ? "登录并读取共享文件夹" : state.connectionStage === "library" ? "保存文件库设置" : "使用这个共享文件夹";
    for (const id of ["connection-url", "connection-username", "connection-password", "connection-otp"]) $(id).disabled = busy || state.connectionStage !== "login";
    $("connection-root").disabled = busy || !choosing || !state.shares.length;
    $("change-library-button").disabled = busy || !state.status?.connected;
    $("change-account-button").disabled = busy;
    $("edit-account-button").disabled = busy;
    $("reload-shares-button").disabled = busy;
    $("empty-action").disabled = busy;
    $("reconnect-button").disabled = state.connecting || state.searching || state.stopped;
    $("disconnect-button").disabled = state.connecting || state.searching || state.stopped;
    $("forget-button").disabled = state.connecting || state.searching || state.stopped;
    $("previous-page").disabled = state.searching || !state.query || state.query.offset === 0;
    $("next-page").disabled = state.searching || !state.results || !state.query || state.query.offset + state.query.limit >= state.results.total;
  }
  function renderEmpty(title, description) {
    $("empty-state").hidden = false;
    if (title) { $("empty-title").textContent = title; $("empty-description").textContent = description || ""; $("empty-action").hidden = true; return; }
    if (state.stopped) { $("empty-title").textContent = "SynoFind 已退出"; $("empty-description").textContent = "你可以关闭这个窗口。下次使用时重新打开应用即可。"; $("empty-action").hidden = true; }
    else if (!state.status) { $("empty-title").textContent = "正在准备你的文件库"; $("empty-description").textContent = "正在连接这台电脑上的 SynoFind 服务。"; $("empty-action").hidden = true; }
    else if (!hasSources()) { $("empty-title").textContent = state.status.configured ? "连接文件库，继续查找" : "添加你的第一个数据源"; $("empty-description").textContent = "在顶部添加本机文件夹，或连接 NAS 后选择共享文件夹。"; $("empty-action").textContent = state.status.remembered ? "使用已保存账户连接" : "连接群晖"; $("empty-action").hidden = false; }
    else { $("empty-title").textContent = "从一个关键词开始"; $("empty-description").textContent = "输入文件名或其中的一部分，点击搜索。也可以组合类型、大小和日期筛选。"; $("empty-action").hidden = true; }
  }
  function connectionError(text) { $("connection-error").textContent = text; $("connection-error").hidden = !text; }
  function setConnectionStage(stage) {
    state.connectionStage = stage;
    $("connection-overview").hidden = stage !== "overview";
    $("connection-form").hidden = stage === "overview";
    $("connection-credentials").hidden = stage !== "login";
    $("connection-selection").hidden = !["select", "library"].includes(stage);
    $("library-options").hidden = !["select", "library"].includes(stage);
    $("remember-option").hidden = stage !== "select";
    $("saved-connection").hidden = !state.status?.remembered || ["select", "library"].includes(stage);
    $("reload-shares-button").hidden = stage !== "library";
    $("selection-step").textContent = stage === "library" ? "文件库设置" : "第 2 步 · 选择共享文件夹";
    $("connection-title").textContent = stage === "overview" ? "连接设置" : stage === "library" ? "共享文件夹与预览" : "连接群晖";
    $("connection-intro").textContent = stage === "overview" ? "管理当前连接，或为文件库设置电脑上的位置和预览方式。" : stage === "library" ? "切换文件夹无需重新输入密码；每个文件夹分别保存设置。" : stage === "select" ? "已登录。请选择共享文件夹，确认后才保存连接。" : "先登录你的 NAS，再选择要搜索的共享文件夹。";
    setControls();
  }
  function clearPasswordFields() { $("connection-password").value = ""; $("connection-otp").value = ""; }
  function resetSelection() { clearTimeout(state.selectionTimer); state.selectionTimer = null; state.selectionDeadline = 0; state.selectionId = null; }
  async function cancelSelection(id = state.selectionId, keepalive = false) {
    if (!id) return;
    if (state.selectionId === id) resetSelection();
    if (state.stopped) return;
    try { await api("/api/connection/cancel", { method: "POST", body: { selection_id: id }, keepalive }); }
    catch { if (!keepalive && !state.stopped) showToast("待选连接未能立即取消，稍后会自动失效。"); }
  }
  function fillAccount(profile) {
    $("connection-url").value = profile.url || ""; $("connection-username").value = profile.username || ""; $("connection-smb").value = profile.smb_host || "";
    state.smbEdited = false; clearPasswordFields();
    $("connection-remember").checked = state.status?.can_remember !== false;
  }
  function openConnection(forceLogin = false) {
    if (state.stopped) return;
    clearHover();
    state.connectionEpoch += 1; void cancelSelection();
    const profile = { ...defaults, ...(state.status?.profile || {}) };
    fillAccount(profile); populateShares([]); setConnectionStage(!forceLogin && profile.root && state.status?.configured ? "overview" : "login");
    connectionError(statusError(state.status));
    if (!$("connection-dialog").open) $("connection-dialog").showModal();
  }
  function leaveConnection() { state.connectionEpoch += 1; clearPasswordFields(); void cancelSelection(); }
  function closeConnection() { leaveConnection(); if ($("connection-dialog").open) $("connection-dialog").close(); }
  async function changeAccount() {
    if (state.connecting) return;
    state.connectionEpoch += 1; state.connecting = true; setControls();
    await cancelSelection();
    clearPasswordFields(); $("connection-smb").value = ""; state.smbEdited = false; populateShares([]); connectionError("");
    state.connecting = false; setConnectionStage("login"); $("connection-url").focus();
  }
  function populateShares(shares, currentRoot = "") {
    state.shares = []; const seen = new Set();
    for (const share of Array.isArray(shares) ? shares : []) {
      if (!share || typeof share.path !== "string" || !share.path.startsWith("/") || share.path === "/" || seen.has(share.path)) continue;
      seen.add(share.path); state.shares.push(share);
    }
    const placeholder = element("option", "", "请选择共享文件夹"); placeholder.value = "";
    $("connection-root").replaceChildren(placeholder);
    for (const share of state.shares) { const option = element("option", "", share.name ? `${share.name} · ${share.path}` : share.path); option.value = share.path; $("connection-root").append(option); }
    // Only a previously saved folder can be selected automatically. A new
    // connection always remains on the empty, required placeholder.
    $("connection-root").value = state.shares.some((share) => share.path === currentRoot) ? currentRoot : "";
    applyShareSettings();
  }
  function applyShareSettings() {
    const share = state.shares.find((item) => item.path === $("connection-root").value);
    const current = state.connectionStage === "library" && state.status?.profile?.root === share?.path ? state.status.profile : {};
    $("connection-local-root").value = share?.local_root ?? current.local_root ?? "";
    const mode = share?.preview_mode ?? current.preview_mode ?? "web";
    $("connection-preview-mode").value = ["web", "quicklook", "seer"].includes(mode) ? mode : "web";
    $("connection-preview-executable").value = share?.preview_executable ?? current.preview_executable ?? "";
    $("preview-executable-field").hidden = $("connection-preview-mode").value === "web"; setControls();
  }
  function libraryOptions() {
    const options = { root: $("connection-root").value, local_root: $("connection-local-root").value.trim(), preview_mode: $("connection-preview-mode").value, preview_executable: $("connection-preview-executable").value.trim() };
    const host = $("connection-smb").value.trim(); if (host) options.smb_host = host;
    if (!options.root || !state.shares.some((share) => share.path === options.root)) throw new Error("请先选择一个共享文件夹。");
    if (options.preview_mode !== "web" && !options.local_root) throw new Error("使用电脑上的预览工具前，请填写此文件夹在电脑上的位置。");
    if (options.preview_mode === "web") options.preview_executable = "";
    return options;
  }
  function scheduleSelectionExpiry() {
    clearTimeout(state.selectionTimer);
    if (!state.selectionId) return;
    const id = state.selectionId;
    state.selectionTimer = setTimeout(() => {
      if (state.selectionId !== id) return;
      void cancelSelection(id); clearPasswordFields(); populateShares([]); setConnectionStage("login"); connectionError("选择共享文件夹的时间已过，请重新登录后再试。");
    }, Math.max(0, state.selectionDeadline - Date.now()));
  }
  async function prepareConnection() {
    const url = $("connection-url").value.trim();
    try { const parsed = new URL(url); if (parsed.protocol !== "https:" || parsed.username || parsed.password) throw new Error(); } catch { connectionError("请使用有效的 HTTPS NAS 地址，不要在地址中填写账号或密码。"); return; }
    const body = { url, username: $("connection-username").value.trim(), password: $("connection-password").value, otp_code: $("connection-otp").value.trim() || null };
    if ($("connection-smb").value.trim()) body.smb_host = $("connection-smb").value.trim();
    const epoch = state.connectionEpoch; state.connecting = true; setControls(); connectionError("");
    try {
      const result = await api("/api/connection/prepare", { method: "POST", body }); clearPasswordFields();
      if (typeof result.selection_id !== "string" || !result.selection_id) throw new Error("未能准备共享文件夹选择，请重新登录。");
      if (epoch !== state.connectionEpoch || !$("connection-dialog").open) { await cancelSelection(result.selection_id); return; }
      state.selectionId = result.selection_id;
      const expires = Number(result.expires_in); state.selectionDeadline = Date.now() + (Number.isFinite(expires) && expires > 0 ? Math.min(expires, 300) : 300) * 1000;
      const profile = { ...defaults, ...(result.profile || {}) }; fillAccount(profile);
      $("selection-account").textContent = `${profile.username} · ${profile.url}`;
      populateShares(result.shares);
      if (!state.shares.length) { await cancelSelection(); throw new Error("已登录，但没有可选择的共享文件夹。请检查账户的共享文件夹读取权限后重新登录。"); }
      $("shares-description").textContent = "请选择一个共享文件夹；此步骤需在 5 分钟内完成。";
      setConnectionStage("select"); scheduleSelectionExpiry();
    } catch (error) { if (epoch === state.connectionEpoch && $("connection-dialog").open) { clearPasswordFields(); connectionError(connectionMessage(error)); } }
    finally { body.password = ""; body.otp_code = null; state.connecting = false; setControls(); }
  }
  async function loadShares() {
    if (state.connecting || state.searching || !state.status?.connected) return;
    state.connectionEpoch += 1; const epoch = state.connectionEpoch; await cancelSelection();
    fillAccount({ ...defaults, ...state.status.profile }); populateShares([]); setConnectionStage("library");
    $("selection-account").textContent = `${state.status.profile.username} · ${state.status.profile.url}`;
    $("shares-description").textContent = "正在读取当前账户可访问的共享文件夹…";
    state.connecting = true; connectionError(""); setControls();
    try {
      const result = await api("/api/shares");
      if (epoch !== state.connectionEpoch || !$("connection-dialog").open) return;
      populateShares(result.shares, state.status.profile.root);
      $("shares-description").textContent = state.shares.length ? "选择要使用的文件夹，或修改当前文件夹的电脑位置与预览方式。" : "当前账户没有可选择的共享文件夹。";
      if (!state.shares.length) connectionError("没有可选择的共享文件夹，请检查账户权限后重新读取。");
    } catch (error) { if (epoch === state.connectionEpoch) { connectionError(message(error)); $("shares-description").textContent = "读取未完成，可以重新尝试。"; } }
    finally { state.connecting = false; setControls(); }
  }
  async function connect(event) {
    event.preventDefault(); if (state.connecting || state.searching || state.stopped) return;
    if (state.connectionStage === "login") { await prepareConnection(); return; }
    if (!["select", "library"].includes(state.connectionStage)) return;
    let body; try { body = libraryOptions(); } catch (error) { connectionError(message(error)); return; }
    const selecting = state.connectionStage === "select";
    if (selecting && (!state.selectionId || Date.now() >= state.selectionDeadline)) { await cancelSelection(); setConnectionStage("login"); connectionError("选择已过期，请重新登录后再试。"); return; }
    if (selecting) { body.selection_id = state.selectionId; body.remember = $("connection-remember").checked && !$("connection-remember").disabled; clearTimeout(state.selectionTimer); }
    state.connecting = true; setControls(); connectionError("");
    try {
      const status = await api(selecting ? "/api/connection/commit" : "/api/library", { method: "POST", body });
      resetSelection(); renderStatus(status); clearResults(); closeConnection(); showToast(selecting ? "文件库已连接，可以开始搜索。" : "已保存文件库设置。"); $("search-input").focus();
    } catch (error) {
      if (selecting && (error.status === 410 || /(?:选择|待选|连接步骤).{0,12}(?:过期|失效)/.test(error.message || ""))) { await cancelSelection(); setConnectionStage("login"); }
      connectionError(connectionMessage(error)); if (selecting && state.selectionId) scheduleSelectionExpiry();
    }
    finally { state.connecting = false; setControls(); }
  }
  async function reconnect() {
    if (state.connecting || state.searching || state.stopped) return;
    state.connecting = true; setControls(); showNotice("正在使用已保存账户连接群晖…");
    try { renderStatus(await api("/api/reconnect", { method: "POST", body: {} })); clearResults(); closeConnection(); showToast("已重新连接群晖。"); }
    catch (error) { await refreshStatus(); openConnection(true); $("connection-error").textContent = connectionMessage(error); $("connection-error").hidden = false; }
    finally { state.connecting = false; setControls(); }
  }
  async function disconnect(forget) {
    if (state.connecting || state.searching) return;
    if (forget && !window.confirm("忘记此账户将删除这台电脑加密保存的登录凭据，并断开当前连接。继续吗？")) return;
    state.connecting = true; setControls();
    try { renderStatus(await api("/api/disconnect", { method: "POST", body: { forget } })); clearResults(); closeConnection(); showToast(forget ? "已删除保存的凭据并断开连接。" : "已断开连接。"); }
    catch (error) { $("connection-error").textContent = message(error); $("connection-error").hidden = false; }
    finally { state.connecting = false; setControls(); }
  }
  async function refreshStatus() {
    if (state.stopped || !state.csrf) return;
    try { renderStatus(await api("/api/status")); } catch (error) { if (!state.connecting) showNotice(message(error), "刷新连接"); }
  }

  function filterCount() { const ids = ["extension-filter", "min-size-filter", "max-size-filter", "after-filter", "before-filter"]; const count = ids.filter((id) => $(id).value !== "").length; $("filter-count").hidden = count === 0; $("filter-count").textContent = String(count); }
  function readQuery() {
    const [sort_by, order] = $("sort-select").value.split(":");
    const query = { source: $("source-select").value, pattern: $("search-input").value.trim(), limit: 50, offset: 0, sort_by, descending: order === "desc" };
    if ($("extension-filter").value) query.extension = $("extension-filter").value;
    for (const [id, key] of [["min-size-filter", "min_size"], ["max-size-filter", "max_size"]]) {
      if ($(id).value !== "") { const value = Number($(id).value); if (!Number.isFinite(value) || value < 0 || value * 1024 * 1024 > Number.MAX_SAFE_INTEGER) throw new Error("请填写有效的非负文件大小。"); query[key] = Math.round(value * 1024 * 1024); }
    }
    if (query.min_size !== undefined && query.max_size !== undefined && query.min_size > query.max_size) throw new Error("最小文件大小不能大于最大文件大小。");
    if ($("after-filter").value) query.modified_after = Math.floor(new Date($("after-filter").value + "T00:00:00").getTime() / 1000);
    if ($("before-filter").value) query.modified_before = Math.floor(new Date($("before-filter").value + "T23:59:59").getTime() / 1000);
    if (query.modified_after > query.modified_before) throw new Error("开始日期不能晚于结束日期。");
    return query;
  }
  let liveTimer = null, composing = false, searchSequence = 0;
  async function cancelSearchRequest(id, wait = false) {
    if (!id || state.stopped) return;
    for (let attempt = 0; attempt < (wait ? 100 : 1); attempt++) {
      const result = await api("/api/search/cancel", {method:"POST", body:{search_id:id}, keepalive:!wait});
      if (result.error) throw new Error(result.error);
      if (result.stopped) return;
      if (wait) await new Promise(resolve => setTimeout(resolve, 200));
    }
    if (wait) throw new Error("停止请求已发送，仍在等待 NAS 确认；请稍后再试。");
  }
  function invalidateSearch() {
    const id = state.activeSearchId;
    state.searchAbort?.abort(); state.searchAbort = null; state.searching = false; state.activeSearchId = null;
    $("loading-state").hidden = true; $("results-section").setAttribute("aria-busy", "false");
    if (id) void cancelSearchRequest(id).catch(error => showToast(message(error)));
    return id;
  }
  async function stopSearch() {
    clearTimeout(liveTimer); clearHover();
    const id = invalidateSearch(); const generation = searchSequence;
    state.stoppingSearch = true; setControls();
    try {
      await cancelSearchRequest(id, true);
      if (generation === searchSequence) { showToast("搜索已停止。"); state.results = null; $("table-wrap").hidden = true; $("pagination").hidden = true; $("results-count").hidden = true; renderEmpty("搜索已停止", "修改关键词即可开始新的搜索。"); }
    } catch(error) { if (generation === searchSequence) showNotice(message(error)); }
    finally { if (generation === searchSequence) { state.stoppingSearch = false; setControls(); } }
  }
  function scheduleLiveSearch(event) {
    clearTimeout(liveTimer); clearHover();
    invalidateSearch(); setControls();
    if (composing || event?.isComposing || !state.status?.local_sources?.length || $("source-select").value === "nas") return;
    liveTimer = setTimeout(() => {
      try { const query = readQuery(); if (query.source === "all") query.source = "local"; search(query, true); }
      catch(error) { $("search-error").textContent = message(error); $("search-error").hidden = false; }
    }, 180);
  }
  async function search(query, live = false) {
    clearTimeout(liveTimer);
    if (state.connecting || !hasSources() || state.stopped) return;
    invalidateSearch(); state.stoppingSearch = false;
    const searchId = Date.now().toString(36) + "-" + (++searchSequence);
    state.activeSearchId = searchId;
    clearHover();
    const controller = new AbortController();
    state.searchAbort = controller; state.searching = true; state.query = { ...query };
    $("live-results-hint").hidden = !live;
    $("live-results-hint").textContent = $("source-select").value === "all" && state.status?.connected ? "本机即时结果 · 按回车同时查询 NAS" : "本机即时结果 · 随输入更新";
    $("search-error").hidden = true; $("empty-state").hidden = true;
    if (!live || !state.results?.files?.length) $("table-wrap").hidden = true;
    $("pagination").hidden = true; $("results-count").hidden = true; $("loading-state").hidden = live; $("results-section").setAttribute("aria-busy", "true"); setControls();
    try {
      const result = await api("/api/search?" + new URLSearchParams({...query, search_id:searchId}).toString(), { signal: controller.signal });
      if (state.searchAbort !== controller || result.cancelled) return;
      if (!Array.isArray(result.files) || !Number.isFinite(result.total)) throw new Error("搜索结果格式不正确，请重试。");
      state.results = result; renderResults(result);
    } catch (error) {
      if (error.name !== "AbortError" && state.searchAbort === controller) { state.results = null; $("search-error").textContent = message(error); $("search-error").hidden = false; renderEmpty("这次搜索未完成", "调整条件后重试，或检查数据源设置。"); }
    } finally { if (state.searchAbort === controller) { state.searching = false; state.searchAbort = null; state.activeSearchId = null; $("loading-state").hidden = true; $("results-section").setAttribute("aria-busy", "false"); setControls(); } }
  }
  function formatSize(value) { const number = Number(value); if (value === null || value === undefined || !Number.isFinite(number) || number < 0) return "—"; if (number < 1024) return integerFormatter.format(number) + " B"; const units = ["KB", "MB", "GB", "TB", "PB"]; let size = number / 1024, unit = 0; while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; } return size.toFixed(size >= 10 ? 1 : 2).replace(/\.0$/, "") + " " + units[unit]; }
  function formatDate(value) { if (!Number.isFinite(Number(value)) || value === null || value === undefined) return "—"; const date = new Date(Number(value) * 1000); return Number.isNaN(date.getTime()) ? "—" : dateFormatter.format(date); }
  function extension(file) { const name = String(file.name || file.path || ""); const index = name.lastIndexOf("."); return index < 0 ? "" : name.slice(index + 1).toLowerCase(); }
  function sizeOf(file) { const value = file.additional?.size ?? file.size; return Number.isFinite(Number(value)) && value !== null && value !== undefined ? Number(value) : null; }
  function mtimeOf(file) { return file.additional?.time?.mtime ?? file.mtime; }
  function canPreview(file) { const size = sizeOf(file), ext = extension(file); return !file.isdir && size !== null && ((imageExtensions.has(ext) && size <= MAX_IMAGE) || (textExtensions.has(ext) && size <= MAX_TEXT) || (ext === "pdf" && size <= MAX_PDF)); }
  function element(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; }
  function actionButton(label, iconName, handler, className = "") { const button = element("button", "button icon-only " + className); button.type = "button"; button.title = label; button.setAttribute("aria-label", label); button.append(icon(iconName)); button.addEventListener("click", handler); return button; }
  function fileBadge(file) { const ext = extension(file); let tone = ""; if (file.isdir) tone = "folder"; else if (ext === "pdf") tone = "pdf"; else if (imageExtensions.has(ext)) tone = "image"; else if (["doc", "docx", "odt"].includes(ext)) tone = "document"; else if (["xls", "xlsx", "csv"].includes(ext)) tone = "spreadsheet"; else if (["zip", "rar", "7z", "gz", "tar"].includes(ext)) tone = "archive"; const badge = element("span", "file-badge " + tone); if (file.isdir) badge.append(icon("folder")); else badge.textContent = ext ? ext.slice(0, 4).toUpperCase() : "FILE"; return badge; }
  function renderResults(result) {
    const fragment = document.createDocumentFragment();
    for (const file of result.files) {
      if (!file || typeof file.path !== "string") continue;
      const row = element("tr");
      row.addEventListener("pointerenter", (event) => enterHover(file, event));
      row.addEventListener("pointermove", (event) => { if (hoveredFile !== file) enterHover(file, event); hoverX = event.clientX; hoverY = event.clientY; positionHover(); });
      row.addEventListener("pointerleave", clearHover); row.dataset.path = file.path; row.tabIndex = -1; row.setAttribute("aria-selected", "false");
      row.addEventListener("click", (event) => selectRow(file.path, !event.target.closest("button,a")));
      row.addEventListener("focus", () => selectRow(file.path, false));
      const cell = element("td"); const primary = element("div", "file-primary"); const text = element("div", "file-text");
      const name = element("button", "file-name", file.name || file.path.split("/").pop()); name.type = "button"; name.title = file.isdir ? "打开文件夹" : "预览 " + (file.name || file.path); name.addEventListener("click", () => { selectRow(file.path, false); preview(file); });
      const shownPath = file.display_path || file.path;
      const path = element("div", "file-path", shownPath); path.title = shownPath;
      text.append(element("span", "source-badge", file.source_kind === "local" ? "本机 · " + (file.source_name || "文件夹") : "NAS"));
      text.append(name, path, element("div", "file-mobile-meta", (file.isdir ? "文件夹" : formatSize(sizeOf(file))) + " · " + formatDate(mtimeOf(file)))); primary.append(fileBadge(file), text); cell.append(primary);
      const parentPath = shownPath.slice(0, Math.max(shownPath.lastIndexOf("/"), shownPath.lastIndexOf("\\"))) || "/"; const parent = element("td", "path-column", parentPath); parent.title = parentPath;
      const size = element("td", "size-column", file.isdir ? "文件夹" : formatSize(sizeOf(file))); const date = element("td", "date-column", formatDate(mtimeOf(file)));
      const actionCell = element("td", "actions-column"); const actions = element("div", "file-actions");
      actions.append(actionButton("复制文件路径", "copy", () => copySMB(file)));
      actions.append(actionButton((file.source_kind === "local" || state.status?.profile?.local_root) ? file.isdir ? "打开文件夹" : "打开所在文件夹" : "设置此文件库在电脑上的位置", "folder", () => reveal(file), "reveal-action"));
      actionCell.append(actions); row.append(cell, parent, size, date, actionCell); fragment.append(row);
    }
    if (result.warning) showNotice(result.warning);
    $("file-rows").replaceChildren(fragment); $("results-count").textContent = integerFormatter.format(result.total) + " 项"; $("results-count").hidden = false;
    if (result.files.some((file) => file.path === state.selectedPath)) selectRow(state.selectedPath, false);
    else { state.selectedPath = null; const first = $("file-rows").firstElementChild; if (first) first.tabIndex = 0; }
    if (!result.files.length) { $("table-wrap").hidden = true; renderEmpty(state.query.offset ? "这一页没有文件" : "没有找到匹配的文件", state.query.offset ? "文件可能发生了变化，请返回上一页或重新搜索。" : "试试更短的文件名，或减少类型、大小和日期限制。"); }
    else { $("empty-state").hidden = true; $("table-wrap").hidden = false; }
    const start = result.files.length ? state.query.offset + 1 : 0; const end = Math.min(state.query.offset + result.files.length, result.total);
    $("page-summary").textContent = `${integerFormatter.format(start)}–${integerFormatter.format(end)} 项，共 ${integerFormatter.format(result.total)} 项`;
    $("page-number").textContent = `${Math.floor(state.query.offset / state.query.limit) + 1} / ${Math.max(1, Math.ceil(result.total / state.query.limit))}`;
    $("pagination").hidden = result.total === 0 && state.query.offset === 0;
  }
  function selectRow(path, focus) {
    state.selectedPath = path;
    for (const row of $("file-rows").children) { const selected = row.dataset.path === path; row.classList.toggle("selected", selected); row.setAttribute("aria-selected", String(selected)); row.tabIndex = selected ? 0 : -1; if (selected && focus) row.focus(); }
  }
  function setView(view) { state.view = view; $("files-table").classList.toggle("view-paths", view === "paths"); $("view-details").setAttribute("aria-pressed", String(view === "details")); $("view-paths").setAttribute("aria-pressed", String(view === "paths")); }
  async function openLibrarySettings() { closePreview(); openConnection(); if (state.status?.connected) await loadShares(); }
  async function reveal(file) {
    if (!file || state.stopped) return;
    if (file.source_kind !== "local" && !state.status?.profile?.local_root) { await openLibrarySettings(); showToast("请先填写此共享文件夹在电脑上的位置。"); return; }
    try { await api("/api/files/reveal", { method: "POST", body: { path: file.path } }); }
    catch (error) { showToast(message(error)); }
  }
  async function preview(file) {
    if (!file || state.stopped || (file.source_kind !== "local" && !state.status?.connected)) return;
    clearHover();
    if (file.isdir) { await reveal(file); return; }
    const mode = file.preview_mode || state.status.profile?.preview_mode || "web";
    if (mode === "web") { await webPreview(file); return; }
    if (file.source_kind !== "local" && !state.status.profile?.local_root) { await openLibrarySettings(); showToast("使用电脑上的预览工具前，请先设置文件夹位置。"); return; }
    if (state.nativeBusy) return;
    state.nativeBusy = true;
    try { await api("/api/files/preview-native", { method: "POST", body: { path: file.path } }); showToast(`已交给 ${mode === "quicklook" ? "QuickLook" : "Seer"} 预览。`); }
    catch (error) { showToast(message(error)); }
    finally { state.nativeBusy = false; }
  }
  async function copySMB(file) {
    const profile = { ...defaults, ...(state.status?.profile || {}) };
    let host = profile.smb_host; if (!host) { try { host = new URL(profile.url).hostname; } catch { /* Leave the path unavailable without a configured NAS. */ } }
    if (!file.smb_path && !host) { showToast("请先设置 Windows 共享主机。"); return; }
    const path = typeof file.smb_path === "string" && file.smb_path ? file.smb_path : "\\\\" + host + file.path.replaceAll("/", "\\");
    try { await copyText(path); showToast("已复制文件路径，可粘贴到文件资源管理器。"); } catch { showToast("复制失败，请检查浏览器的剪贴板权限。"); }
  }
  async function copyText(value) {
    try { if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(value); return; } } catch { /* Fall back for local browsers with clipboard permissions disabled. */ }
    const input = element("textarea", "copy-fallback"); input.value = value; input.setAttribute("readonly", ""); document.body.append(input); input.select(); const ok = document.execCommand("copy"); input.remove(); if (!ok) throw new Error("copy failed");
  }
  function previewMessage(text) { $("preview-content").replaceChildren(element("p", "preview-message", text)); }
  function disposePDF() {
    const view = state.pdfView; state.pdfView = null;
    if (!view) return;
    view.closed = true; clearTimeout(view.resizeTimer); view.observer?.disconnect();
    if (view.resizeHandler) window.removeEventListener("resize", view.resizeHandler);
    try { view.renderTask?.cancel(); } catch { /* A completed render has nothing to cancel. */ }
    try { view.page?.cleanup(); } catch { /* Destruction below releases remaining resources. */ }
    if (view.loadingTask) Promise.resolve(view.loadingTask.destroy()).catch(() => {});
    view.canvas.width = 1; view.canvas.height = 1;
  }
  function renderPDFPage(view, number) {
    if (view.closed || !view.document) return;
    const generation = ++view.generation;
    try { view.renderTask?.cancel(); } catch { /* Rendering may already have completed. */ }
    view.previous.disabled = true; view.next.disabled = true; view.status.hidden = false; view.status.textContent = "正在绘制页面…";
    view.sequence = (view.sequence || Promise.resolve()).catch(() => {}).then(async () => {
      if (view.closed || generation !== view.generation) return;
      try {
        view.page?.cleanup(); const page = await view.document.getPage(number);
        if (view.closed || generation !== view.generation) { page.cleanup(); return; }
        view.page = page; view.pageNumber = number;
        const base = page.getViewport({ scale: 1 }); const width = Math.max(120, view.frame.clientWidth - 32);
        const scale = width / base.width; const cssHeight = base.height * scale;
        const ratio = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(8000000 / (width * cssHeight)));
        const viewport = page.getViewport({ scale: scale * ratio });
        view.canvas.width = Math.max(1, Math.floor(viewport.width)); view.canvas.height = Math.max(1, Math.floor(viewport.height));
        view.canvas.setAttribute("aria-label", `PDF 第 ${number} 页，共 ${view.document.numPages} 页`);
        view.label.textContent = `${number} / ${view.document.numPages}`;
        view.renderTask = page.render({ canvasContext: view.canvas.getContext("2d", { alpha: false }), viewport, annotationMode: view.module.AnnotationMode.DISABLE });
        await view.renderTask.promise;
        if (!view.closed && generation === view.generation) { view.status.hidden = true; view.canvas.hidden = false; }
      } catch (error) {
        if (!view.closed && generation === view.generation && error.name !== "RenderingCancelledException") { view.status.textContent = "这一页暂时无法显示，可尝试其他页面或电脑上的预览工具。"; view.canvas.hidden = true; }
      } finally {
        if (!view.closed && generation === view.generation) { view.renderTask = null; view.previous.disabled = view.pageNumber <= 1; view.next.disabled = view.pageNumber >= view.document.numPages; }
      }
    });
  }
  async function showPDF(data, controller) {
    if (!pdfModulePromise) pdfModulePromise = import("/vendor/pdf.mjs").catch((error) => { pdfModulePromise = null; throw error; });
    const pdfjs = await pdfModulePromise; if (controller.signal.aborted) return;
    pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker.mjs";
    const shell = element("div", "pdf-canvas-viewer"); const toolbar = element("div", "pdf-toolbar");
    const previous = element("button", "button page-button", "上一页"); previous.type = "button";
    const next = element("button", "button page-button", "下一页"); next.type = "button";
    const label = element("span", "pdf-page-number", "正在读取页数…"); label.setAttribute("aria-live", "polite");
    const status = element("p", "pdf-render-status", "正在加载 PDF…"); status.setAttribute("role", "status");
    const frame = element("div", "pdf-canvas-frame"); const canvas = document.createElement("canvas"); canvas.className = "pdf-page-canvas"; canvas.hidden = true; canvas.setAttribute("role", "img");
    toolbar.append(previous, label, next); frame.append(status, canvas); shell.append(toolbar, frame); $("preview-content").replaceChildren(shell);
    const view = { module: pdfjs, canvas, frame, status, label, previous, next, pageNumber: 1, generation: 0, closed: false, document: null, renderTask: null };
    state.pdfView = view; previous.disabled = true; next.disabled = true;
    previous.addEventListener("click", () => renderPDFPage(view, Math.max(1, view.pageNumber - 1)));
    next.addEventListener("click", () => renderPDFPage(view, Math.min(view.document.numPages, view.pageNumber + 1)));
    view.loadingTask = pdfjs.getDocument({ data, isEvalSupported: false, enableXfa: false, useSystemFonts: true, disableFontFace: true, disableWorkerRendering: true, useWasm: false, useWorkerFetch: true, cMapUrl: "/vendor/cmaps/", cMapPacked: true, standardFontDataUrl: "/vendor/standard_fonts/", wasmUrl: "/vendor/wasm/", iccUrl: "/vendor/iccs/" });
    try {
      view.document = await view.loadingTask.promise;
      if (view.closed || controller.signal.aborted) return;
      view.width = frame.clientWidth; renderPDFPage(view, 1);
      view.resizeHandler = () => { if (view.closed || Math.abs(frame.clientWidth - view.width) < 1) return; view.width = frame.clientWidth; clearTimeout(view.resizeTimer); view.resizeTimer = setTimeout(() => renderPDFPage(view, view.pageNumber), 100); };
      if (typeof ResizeObserver !== "undefined") { view.observer = new ResizeObserver(view.resizeHandler); view.observer.observe(frame); }
      else window.addEventListener("resize", view.resizeHandler);
    } catch (error) {
      if (!view.closed && !controller.signal.aborted) { status.textContent = "PDF 无法在网页中预览，可在文件库设置中选择 QuickLook 或 Seer。"; previous.disabled = true; next.disabled = true; }
    }
  }
  async function webPreview(file) {
    if (state.stopped || (file.source_kind !== "local" && !state.status?.connected)) return;
    closePreview(); const controller = new AbortController(); state.previewAbort = controller; state.previewFile = file;
    $("preview-title").textContent = file.name || file.path.split("/").pop(); $("preview-path").textContent = file.display_path || file.path;
    $("preview-meta").replaceChildren(); $("preview-reveal-label").textContent = (file.source_kind === "local" || state.status.profile?.local_root) ? "打开所在文件夹" : "设置电脑上的文件夹位置"; previewMessage("正在读取文件信息…"); $("preview-dialog").showModal();
    try {
      const info = await previewInfo("/api/files/info?" + new URLSearchParams({ path: file.path }), { signal: controller.signal });
      if (controller.signal.aborted) return;
      const current = { ...file, ...info }; $("preview-meta").append(element("span", "", current.isdir ? "文件夹" : formatSize(sizeOf(current))), element("span", "", "修改于 " + formatDate(mtimeOf(current))));
      if (current.isdir) { previewMessage("这是一个文件夹。你可以复制它的 Windows 共享路径，在文件资源管理器中打开。"); return; }
      const ext = extension(current), size = sizeOf(current), url = "/api/files/preview?" + new URLSearchParams({ path: file.path });
      if (imageExtensions.has(ext) && size !== null && size <= MAX_IMAGE) {
        const img = document.createElement("img"); img.alt = current.name || "文件图片"; img.addEventListener("error", () => { if (!controller.signal.aborted) previewMessage("图片无法预览，可以打开所在文件夹查看。"); }, { once: true }); img.src = url; $("preview-content").replaceChildren(img);
      } else if (textExtensions.has(ext) && size !== null && size <= MAX_TEXT) {
        previewMessage("正在读取文本…");
        const response = await fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "X-SynoFind-CSRF": state.csrf, Accept: "text/plain" }, signal: controller.signal });
        if (!response.ok) { let detail; try { detail = await response.json(); } catch { /* Error must stay plain text. */ } throw new APIError(detail?.error?.message || "无法读取文本预览，可以打开所在文件夹查看。", response.status, detail?.error?.code, detail?.error?.api, detail?.error?.method); }
        const contentType = (response.headers.get("Content-Type") || "").toLowerCase();
        if (!contentType.startsWith("text/plain")) throw new Error("此文件未以安全文本格式返回，请使用电脑上的预览工具查看。");
        const advertised = Number(response.headers.get("Content-Length")); if (advertised > MAX_TEXT) { controller.abort(); throw new Error("文件超过 2 MB 文本预览上限，请使用电脑上的预览工具查看。"); }
        const reader = response.body.getReader(); const chunks = []; let bytes = 0;
        try { while (true) { const { value, done } = await reader.read(); if (done) break; bytes += value.byteLength; if (bytes > MAX_TEXT) { await reader.cancel(); throw new Error("文件超过 2 MB 文本预览上限，请使用电脑上的预览工具查看。"); } chunks.push(value); } } finally { reader.releaseLock(); }
        if (controller.signal.aborted) return;
        const buffer = new Uint8Array(bytes); let position = 0; for (const chunk of chunks) { buffer.set(chunk, position); position += chunk.byteLength; }
        const pre = element("pre", "", new TextDecoder("utf-8", { fatal: false }).decode(buffer)); $("preview-content").replaceChildren(pre);
      } else if (ext === "pdf" && size !== null && size <= MAX_PDF) {
        previewMessage("正在读取 PDF…");
        const response = await fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "X-SynoFind-CSRF": state.csrf, Accept: "application/pdf" }, signal: controller.signal });
        if (!response.ok) { let detail; try { detail = await response.json(); } catch { /* Keep errors plain. */ } throw new APIError(detail?.error?.message || "无法读取 PDF 预览。", response.status, detail?.error?.code, detail?.error?.api, detail?.error?.method); }
        if (!(response.headers.get("Content-Type") || "").toLowerCase().startsWith("application/pdf")) { await response.body?.cancel(); throw new Error("此文件未返回有效的 PDF 内容。"); }
        const reader = response.body.getReader(); const chunks = []; let bytes = 0;
        try { while (true) { const { value, done } = await reader.read(); if (done) break; bytes += value.byteLength; if (bytes > MAX_PDF) { await reader.cancel(); throw new Error("PDF 超过 20 MB 网页预览上限，请使用电脑上的预览工具查看。"); } chunks.push(value); } } finally { reader.releaseLock(); }
        if (controller.signal.aborted) return;
        const data = new Uint8Array(bytes); let position = 0; for (const chunk of chunks) { data.set(chunk, position); position += chunk.byteLength; }
        if (new TextDecoder().decode(data.subarray(0, 5)) !== "%PDF-") throw new Error("文件内容不是有效的 PDF。");
        await showPDF(data, controller);
      } else if (imageExtensions.has(ext) || textExtensions.has(ext) || ext === "pdf") previewMessage("文件超过网页预览上限，或暂时无法确认大小。文本限 2 MB、图片限 10 MB、PDF 限 20 MB，可使用电脑上的预览工具查看。");
      else previewMessage("当前格式不支持网页预览。可在文件库设置中选择 QuickLook 或 Seer，也可以打开所在文件夹查看。HTML、SVG 不会在网页中执行。");
    } catch (error) { if (error.name !== "AbortError" && state.previewAbort === controller) previewMessage(message(error)); }
  }
  function closePreview() { state.previewAbort?.abort(); state.previewAbort = null; state.previewFile = null; disposePDF(); $("preview-content").replaceChildren(); if ($("preview-dialog").open) $("preview-dialog").close(); }
  async function shutdown() {
    if (state.stopped || !window.confirm("退出 SynoFind 会停止这台电脑上的搜索服务。已记住的账户会保留。确认退出？")) return;
    try { await api("/api/shutdown", { method: "POST", body: {} }); state.stopped = true; clearInterval(state.poll); state.searchAbort?.abort(); closeConnection(); closePreview(); clearResults(); setControls(); $("settings-button").disabled = true; $("shutdown-button").disabled = true; $("connection-label").textContent = "应用已退出"; $("connection-pill").classList.remove("connected"); document.body.classList.add("app-stopped"); showNotice("SynoFind 已退出。你可以关闭此窗口；下次使用时重新打开应用即可。"); }
    catch (error) { showToast(message(error)); }
  }
  async function initialize() {
    try { const session = await api("/api/session"); if (typeof session.csrf_token !== "string" || !session.csrf_token) throw new Error("本机会话无法初始化，请重新打开应用。"); state.csrf = session.csrf_token; renderStatus(session.status); state.poll = setInterval(() => { if (!document.hidden && !state.connecting && !state.searching) refreshStatus(); }, 10000); }
    catch (error) { showNotice(message(error), "重新尝试"); renderEmpty("暂时无法连接本机服务", "请确认 SynoFind 正在运行，然后点击上方的重新尝试。"); setControls(); }
  }

  $("settings-button").addEventListener("click", () => openConnection());
  $("close-connection").addEventListener("click", closeConnection);
  $("connection-dialog").addEventListener("close", leaveConnection);
  $("connection-form").addEventListener("submit", connect);
  $("change-account-button").addEventListener("click", changeAccount);
  $("edit-account-button").addEventListener("click", changeAccount);
  $("change-library-button").addEventListener("click", loadShares);
  $("reload-shares-button").addEventListener("click", loadShares);
  $("connection-root").addEventListener("change", applyShareSettings);
  $("connection-preview-mode").addEventListener("change", () => { $("preview-executable-field").hidden = $("connection-preview-mode").value === "web"; });
  $("connection-smb").addEventListener("input", () => { state.smbEdited = true; });
  $("connection-url").addEventListener("input", () => { if (!state.smbEdited) $("connection-smb").value = ""; });
  $("reconnect-button").addEventListener("click", reconnect);
  $("disconnect-button").addEventListener("click", () => disconnect(false));
  $("forget-button").addEventListener("click", () => disconnect(true));
  $("empty-action").addEventListener("click", () => state.status?.remembered ? reconnect() : openConnection());
  $("notice-action").addEventListener("click", () => !state.csrf ? initialize() : state.status?.remembered ? reconnect() : openConnection());
  $("stop-search").addEventListener("click", stopSearch);
  $("search-input").addEventListener("input", scheduleLiveSearch);
  $("search-input").addEventListener("compositionstart", () => { composing = true; clearTimeout(liveTimer); invalidateSearch(); setControls(); });
  $("search-input").addEventListener("compositionend", () => { composing = false; scheduleLiveSearch(); });
  $("search-form").addEventListener("submit", (event) => { event.preventDefault(); try { search(readQuery()); } catch (error) { $("search-error").textContent = message(error); $("search-error").hidden = false; } });
  $("sort-select").addEventListener("change", () => { if (state.query) { const [sort_by, order] = $("sort-select").value.split(":"); search({ ...state.query, sort_by, descending: order === "desc", offset: 0 }); } });
  $("previous-page").addEventListener("click", () => { if (state.query?.offset > 0) search({ ...state.query, offset: Math.max(0, state.query.offset - state.query.limit) }); });
  $("next-page").addEventListener("click", () => { if (state.query) search({ ...state.query, offset: state.query.offset + state.query.limit }); });
  $("reset-filters").addEventListener("click", () => { ["extension-filter", "min-size-filter", "max-size-filter", "after-filter", "before-filter"].forEach((id) => { $(id).value = ""; }); filterCount(); });
  ["extension-filter", "min-size-filter", "max-size-filter", "after-filter", "before-filter"].forEach((id) => $(id).addEventListener("input", filterCount));
  $("close-preview").addEventListener("click", closePreview);
  $("preview-dialog").addEventListener("cancel", (event) => { event.preventDefault(); closePreview(); });
  $("preview-reveal").addEventListener("click", () => reveal(state.previewFile));
  $("view-details").addEventListener("click", () => setView("details"));
  $("view-paths").addEventListener("click", () => setView("paths"));
  document.addEventListener("keydown", (event) => {
    if (event.isComposing || composing || event.altKey || event.ctrlKey || event.metaKey || event.repeat || $("connection-dialog").open || $("sources-dialog").open || $("preview-dialog").open || state.searching) return;
    if ((event.code === "Space" || event.key === " ") && hoveredFile) { event.preventDefault(); void preview(hoveredFile); return; }
    const target = event.target instanceof Element ? event.target : null;
    if (target?.closest("input,textarea,select,[contenteditable='true']")) return;
    const rows = [...$("file-rows").children];
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && target?.closest("#files-table") && rows.length) {
      event.preventDefault(); const current = rows.findIndex((row) => row.dataset.path === state.selectedPath); const next = Math.max(0, Math.min(rows.length - 1, current + (event.key === "ArrowDown" ? 1 : -1))); selectRow(rows[next].dataset.path, true); return;
    }
    if ((event.code === "Space" || event.key === " ") && state.selectedPath && !target?.closest("button,a,summary")) {
      const file = state.results?.files.find((item) => item.path === state.selectedPath); if (file) { event.preventDefault(); preview(file); }
    }
  });
  $("shutdown-button").addEventListener("click", shutdown);
  window.addEventListener("pagehide", () => { clearTimeout(liveTimer); clearHover(); invalidateSearch(); clearInterval(state.poll); state.previewAbort?.abort(); state.searchAbort?.abort(); disposePDF(); void cancelSelection(state.selectionId, true); clearPasswordFields(); });

  let hoveredFile = null, hoverTimer = null, hoverAbort = null, hoverURL = null, hoverPDF = null;
  let hoverX = 0, hoverY = 0, sourceSnapshot = '';
  function hasSources() { return !!state.status?.connected || !!state.status?.local_sources?.length; }
  function renderSources() {
    const sources = state.status?.local_sources || [];
    const selected = $("source-select").value;
    const stamp = JSON.stringify(sources);
    if (sourceSnapshot !== stamp) {
      sourceSnapshot = stamp;
      $("source-select").replaceChildren();
      for (const [value, name] of [["all", "全部数据源"], ["nas", "NAS"], ["local", "全部本机文件夹"], ...sources.map(s => [s.id, "本机 · " + s.name])]) {
        const option = element("option", "", name); option.value = value; $("source-select").append(option);
      }
      $("source-select").value = [...$("source-select").options].some(o => o.value === selected) ? selected : "all";
      $("source-list").replaceChildren();
      for (const source of sources) {
        const row = element("div", "source-item"), text = element("div", "source-item-info");
        text.append(element("strong", "", source.name), element("p", "", source.root));
        const detail = source.scanning ? `正在收录 · 已检查 ${integerFormatter.format(source.visited || 0)} 项` : `${integerFormatter.format(source.count)} 项` + (source.updated ? ` · 更新于 ${formatDate(source.updated)}` : " · 等待刷新");
        text.append(element("small", "", detail + (source.errors ? ` · ${source.errors} 处无法读取，原记录已保留` : "")));
        const edit = element("button", "text-button", "设置"); edit.type = "button";
        edit.addEventListener("click", () => { $("source-root").value = source.root; $("source-mode").value = source.mode; $("source-executable").value = source.executable; $("source-executable-field").hidden = source.mode === "web"; $("source-root").focus(); });
        const remove = element("button", "text-button danger", "移除"); remove.type = "button";
        remove.addEventListener("click", async () => { remove.disabled = true; try { renderStatus(await api("/api/sources/remove", {method:"POST", body:{id:source.id}})); clearResults(); showToast("已移除数据源，原始文件保留。"); } catch(error) { sourceError(error); } finally { remove.disabled = false; } });
        row.append(text, edit, remove); $("source-list").append(row);
      }
      if (!sources.length) $("source-list").append(element("p", "dialog-intro", "尚未添加本机文件夹。"));
    }
    $("source-nas-label").textContent = state.status?.connected ? state.status.profile.root : "未连接";
    $("scan-local").disabled = !sources.length || sources.some(s => s.scanning);
  }
  function sourceError(error) { $("source-error").textContent = message(error); $("source-error").hidden = false; }
  function initializeExtras() {
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    let preference = "system";
    try { preference = localStorage.getItem("synofind-theme") || "system"; } catch {}
    if (!["light", "dark", "system"].includes(preference)) preference = "system";
    $("theme-select").value = preference;
    const applyTheme = () => { const value = $("theme-select").value; document.documentElement.dataset.theme = value === "system" ? scheme.matches ? "dark" : "light" : value; };
    applyTheme(); scheme.addEventListener("change", applyTheme);
    $("theme-select").addEventListener("change", () => { applyTheme(); try { localStorage.setItem("synofind-theme", $("theme-select").value); } catch {} });
    $("sources-button").addEventListener("click", () => { clearHover(); renderSources(); $("source-error").hidden = true; $("sources-dialog").showModal(); });
    $("close-sources").addEventListener("click", () => $("sources-dialog").close());
    $("source-nas-settings").addEventListener("click", () => { $("sources-dialog").close(); openConnection(); });
    $("source-mode").addEventListener("change", () => { $("source-executable-field").hidden = $("source-mode").value === "web"; });
    $("source-form").addEventListener("submit", async event => {
      event.preventDefault(); $("save-source").disabled = true; $("source-error").hidden = true;
      try {
        renderStatus(await api("/api/sources/add", {method:"POST", body:{root:$("source-root").value.trim(), preview_mode:$("source-mode").value, preview_executable:$("source-mode").value === "web" ? "" : $("source-executable").value.trim()}}));
        $("source-root").value = ""; showToast("已保存，正在后台更新本机索引。"); clearResults();
      } catch(error) { sourceError(error); } finally { $("save-source").disabled = false; }
    });
    $("scan-local").addEventListener("click", async () => { try { await api("/api/sources/scan", {method:"POST", body:{}}); showToast("已请求刷新本机索引。"); await refreshStatus(); } catch(error) { sourceError(error); } });
    $("source-select").addEventListener("change", () => { if (state.query) search(readQuery()); });
    window.addEventListener("blur", clearHover);
    document.addEventListener("scroll", clearHover, true);
  }
  function clearHover() {
    hoveredFile = null; clearTimeout(hoverTimer); hoverTimer = null;
    hoverAbort?.abort(); hoverAbort = null;
    if (hoverURL) URL.revokeObjectURL(hoverURL); hoverURL = null;
    if (hoverPDF) { void hoverPDF.destroy().catch(() => {}); hoverPDF = null; }
    $("hover-card").hidden = true; $("hover-content").replaceChildren();
  }
  function positionHover() {
    const card = $("hover-card"); if (card.hidden) return;
    const width = card.offsetWidth, height = card.offsetHeight;
    const left = hoverX + width + 24 < innerWidth ? hoverX + 20 : hoverX - width - 20;
    card.style.left = Math.max(12, Math.min(left, innerWidth - width - 12)) + "px";
    card.style.top = Math.max(12, Math.min(hoverY + 18, innerHeight - height - 12)) + "px";
  }
  function enterHover(file, event) {
    clearHover();
    if (event.pointerType === "touch" || state.searching || document.querySelector("dialog[open]")) return;
    hoveredFile = file; hoverX = event.clientX; hoverY = event.clientY;
    hoverTimer = setTimeout(() => { void showHover(file); }, 400);
  }
  async function showHover(file) {
    if (hoveredFile !== file) return;
    const controller = new AbortController(); hoverAbort = controller;
    $("hover-name").textContent = file.name;
    $("hover-caption").textContent = (file.isdir ? "文件夹" : formatSize(sizeOf(file))) + " · 按空格预览";
    $("hover-content").replaceChildren(element("p", "", canPreview(file) ? "正在读取缩略图…" : "按空格查看，或打开所在文件夹"));
    $("hover-card").hidden = false; positionHover();
    if (!canPreview(file)) return;
    try {
      const response = await fetch("/api/files/preview?" + new URLSearchParams({path:file.path}), {credentials:"same-origin", signal:controller.signal, cache:"no-store"});
      if (!response.ok) throw new Error("缩略图暂不可用，按空格预览");
      const ext = extension(file);
      if (imageExtensions.has(ext)) {
        const blob = await response.blob(); if (controller.signal.aborted) return;
        if (blob.size > MAX_IMAGE || !blob.type.startsWith("image/")) throw new Error("图片超过预览限制");
        hoverURL = URL.createObjectURL(blob); const img = element("img"); img.alt = file.name; img.onload = positionHover;
        img.onerror = () => { if (!controller.signal.aborted) $("hover-content").replaceChildren(element("p", "", "图片无法预览")); };
        img.src = hoverURL; $("hover-content").replaceChildren(img);
      } else if (textExtensions.has(ext)) {
        const value = await response.text(); if (controller.signal.aborted) return;
        $("hover-content").replaceChildren(element("pre", "", value.slice(0, 1600)));
      } else if (ext === "pdf") {
        const data = await response.arrayBuffer(); if (controller.signal.aborted) return;
        if (data.byteLength > MAX_PDF) throw new Error("PDF 超过预览限制");
        if (!pdfModulePromise) pdfModulePromise = import("/vendor/pdf.mjs");
        const pdf = await pdfModulePromise; if (controller.signal.aborted) return;
        pdf.GlobalWorkerOptions.workerSrc = "/vendor/pdf.worker.mjs";
        const task = pdf.getDocument({data, cMapUrl:"/vendor/cmaps/", cMapPacked:true, standardFontDataUrl:"/vendor/standard_fonts/", wasmUrl:"/vendor/wasm/", iccUrl:"/vendor/iccs/", useWasm:false, disableWorkerRendering:true, disableFontFace:true, enableXfa:false});
        hoverPDF = task;
        const doc = await task.promise; if (controller.signal.aborted) return;
        const page = await doc.getPage(1); if (controller.signal.aborted) return;
        const base = page.getViewport({scale:1}); const viewport = page.getViewport({scale:Math.min(640/base.width, 500/base.height)});
        const canvas = element("canvas"); canvas.width = Math.ceil(viewport.width); canvas.height = Math.ceil(viewport.height);
        await page.render({canvasContext:canvas.getContext("2d"), viewport, annotationMode:pdf.AnnotationMode.DISABLE}).promise;
        if (controller.signal.aborted) return;
        $("hover-content").replaceChildren(canvas);
      }
      positionHover();
    } catch(error) { if (!controller.signal.aborted && hoveredFile === file) { $("hover-content").replaceChildren(element("p", "", "缩略图暂不可用，按空格预览")); positionHover(); } }
  }
  async function previewInfo(url, options) {
    for (let attempt = 0; ; attempt++) {
      try { return await api(url, options); }
      catch(error) { if (error.status !== 409 || attempt >= 10 || options.signal.aborted) throw error; await new Promise(resolve => setTimeout(resolve, 150)); }
    }
  }

  initializeExtras();
  setControls();
  initialize();
})();
