/* CampusClaw 前端：原生 JS，同源 /api，身份以 /api/me 为准 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const els = {
  loginView: $("#login-view"), appView: $("#app-view"),
  loginForm: $("#login-form"), loginError: $("#login-error"),
  meClass: $("#me-class"), meUser: $("#me-user"),
  search: $("#search-input"), materials: $("#materials"),
  emptyHint: $("#empty-hint"), listCount: $("#list-count"),
  btnUpload: $("#btn-upload"),
  uploadModal: $("#upload-modal"), uploadFile: $("#upload-file"),
  uploadSubmit: $("#upload-submit"), progressWrap: $(".progress-wrap"),
  progressBar: $("#progress-bar"), progressText: $("#progress-text"),
  detailModal: $("#detail-modal"), detailTitle: $("#detail-title"),
  detailMeta: $("#detail-meta"), detailContent: $("#detail-content"),
  detailDownload: $("#detail-download"),
  cmdPalette: $("#cmd-palette"), cmdInput: $("#cmd-input"), cmdList: $("#cmd-list"),
  toastStack: $("#toast-stack"),
};

const state = {
  me: null, materials: [], view: "list", cmdActive: 0, cmdItems: [],
  detailId: null, searchMode: "hybrid", askMessages: [],
};
const TOKEN_KEY = "cc_token";

/* ---------------- API ---------------- */

function authHeaders() {
  const token = localStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: "Bearer " + token } : {};
}

async function api(method, url, body) {
  const opts = { method, headers: authHeaders() };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  if (res.status === 204) return { ok: true, status: 204, data: null };
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON */ }
  if (res.status === 401 && url !== "/api/login") {
    localStorage.removeItem(TOKEN_KEY);
    showLogin();
    return { ok: false, status: 401, data };
  }
  return { ok: res.ok, status: res.status, data };
}

/* ---------------- 视图切换 ---------------- */

function showLogin() {
  state.me = null;
  els.appView.hidden = true;
  els.loginView.hidden = false;
  $("#login-username").focus();
}

function showApp(me) {
  state.me = me;
  els.loginView.hidden = true;
  els.appView.hidden = false;
  const role = me.role === "teacher" ? "教师" : "学生";
  els.meClass.textContent = `${me.class_name} · ${role}`;
  els.meUser.innerHTML = `${escapeHtml(me.username)}`;
  $("#knowledge-title").textContent = `${me.class_name}知识问答`;
  $("#knowledge-me").textContent = `${me.username} · ${role}`;
  els.btnUpload.hidden = me.role !== "teacher";
  showMaterials();
  loadMaterials();
}

/* ---------------- 登录 / 登出 ---------------- */

els.loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  els.loginError.hidden = true;
  const username = $("#login-username").value.trim();
  const password = $("#login-password").value;
  const r = await api("POST", "/api/login", { username, password });
  if (r.ok && r.data && r.data.token) {
    localStorage.setItem(TOKEN_KEY, r.data.token);
    $("#login-password").value = "";
    showApp(r.data);
  } else {
    els.loginError.textContent = (r.data && r.data.error) || "登录失败";
    els.loginError.hidden = false;
  }
});

async function logout() {
  await api("POST", "/api/logout");
  localStorage.removeItem(TOKEN_KEY);
  showLogin();
}
$("#btn-logout").addEventListener("click", logout);
$("#btn-logout-knowledge").addEventListener("click", logout);

/* ---------------- 材料列表 ---------------- */

let searchTimer = null;
els.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadMaterials(els.search.value.trim()), 220);
});

async function loadMaterials(q = "") {
  const url = "/api/materials" + (q ? "?q=" + encodeURIComponent(q) : "");
  const r = await api("GET", url);
  if (!r.ok) return;
  state.materials = r.data || [];
  renderMaterials();
}

function fmtSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}
function fmtDate(s) {
  try { return new Date(s).toLocaleString("zh-CN", { hour12: false }); }
  catch (_) { return s; }
}

function renderMaterials() {
  const list = state.materials;
  els.listCount.textContent = list.length ? `共 ${list.length} 份` : "";
  els.emptyHint.hidden = list.length > 0;
  els.materials.className = state.view === "grid" ? "grid-view" : "list-view";
  els.materials.innerHTML = list.map((m) => `
    <div class="card" data-id="${m.id}" tabindex="0">
      <div class="card-title"><span class="ext">${extOf(m.original_name)}</span>${escapeHtml(m.title)}</div>
      <div class="card-meta">
        <span>📄 ${escapeHtml(m.original_name)}</span>
        <span>👤 ${escapeHtml(m.uploader || "")}</span>
        <span>🗂 ${fmtSize(m.size_bytes)}</span>
        <span>🕒 ${fmtDate(m.created_at)}</span>
      </div>
    </div>`).join("");
  els.materials.querySelectorAll(".card").forEach((card) => {
    const open = () => openDetail(Number(card.dataset.id));
    card.addEventListener("click", open);
    card.addEventListener("keydown", (e) => { if (e.key === "Enter") open(); });
  });
}

function extOf(name) {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toUpperCase() : "TXT";
}

/* ---------------- 详情 + Markdown ---------------- */

async function openDetail(id) {
  const r = await api("GET", `/api/materials/${id}`);
  if (!r.ok) {
    if (r.status === 404) toast("未找到该材料（可能属于其他班级）", "err");
    return;
  }
  const m = r.data;
  state.detailId = m.id;
  $("#reindex-bar").hidden = !(state.me && state.me.role === "teacher");
  els.detailTitle.textContent = m.title;
  els.detailMeta.textContent =
    `${m.original_name} · ${fmtSize(m.size_bytes)} · 上传者 ${m.uploader} · ${fmtDate(m.created_at)}`;
  els.detailContent.innerHTML = renderMarkdown(m.content);
  els.detailDownload.dataset.url = `/api/materials/${m.id}/file`;
  els.detailDownload.dataset.name = m.original_name;
  els.detailDownload.href = "#";
  els.detailModal.hidden = false;
}

els.detailDownload.addEventListener("click", async (e) => {
  e.preventDefault();
  const url = els.detailDownload.dataset.url;
  const name = els.detailDownload.dataset.name || "download";
  if (!url) return;
  const res = await fetch(url, { headers: authHeaders() });
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    showLogin();
    return;
  }
  if (!res.ok) { toast("下载失败", "err"); return; }
  const blob = await res.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
});

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function safeHref(url) {
  return /^(https?:|mailto:|\/)/i.test(url) ? url : "#";
}

function renderInline(text) {
  // 先抽出行内代码，避免其内部被二次转换
  const codes = [];
  text = text.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return "\u0000" + (codes.length - 1) + "\u0000";
  });
  text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g,
    (_, alt, src) => `<img alt="${alt}" src="${escapeHtml(safeHref(src))}" style="max-width:100%">`);
  text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,
    (_, t, href) => `<a href="${escapeHtml(safeHref(href))}" target="_blank" rel="noopener noreferrer">${t}</a>`);
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
             .replace(/__([^_]+)__/g, "<strong>$1</strong>")
             .replace(/~~([^~]+)~~/g, "<del>$1</del>")
             .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  text = text.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
  return text;
}

function renderMarkdown(src) {
  const escaped = escapeHtml(src);
  const fences = [];
  // 抽出围栏代码块
  const body = escaped.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    fences.push(`<pre><code data-lang="${escapeHtml(lang)}">${code.replace(/\n$/, "")}</code></pre>`);
    return "\u0001" + (fences.length - 1) + "\u0001";
  });

  const lines = body.split("\n");
  const html = [];
  let para = [];
  let listType = null;
  const flushPara = () => {
    if (para.length) { html.push("<p>" + para.map(renderInline).join("<br>") + "</p>"); para = []; }
  };
  const flushList = () => {
    if (listType) { html.push(`<${listType}>` + listItems.map((li) => `<li>${renderInline(li)}</li>`).join("") + `</${listType}>`); listType = null; listItems = []; }
  };
  let listItems = [];

  for (const line of lines) {
    const t = line.trim();
    let m;
    if (t.startsWith("\u0001")) { flushPara(); flushList(); html.push(t); continue; }
    if (!t) { flushPara(); flushList(); continue; }
    if ((m = t.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara(); flushList();
      const lv = m[1].length;
      html.push(`<h${lv}>${renderInline(m[2])}</h${lv}>`);
    } else if (/^(-{3,}|\*{3,}|_{3,})$/.test(t)) {
      flushPara(); flushList(); html.push("<hr>");
    } else if ((m = t.match(/^&gt;\s?(.*)$/))) {
      flushPara(); flushList();
      html.push(`<blockquote>${renderInline(m[1])}</blockquote>`);
    } else if ((m = t.match(/^[-*+]\s+(.*)$/))) {
      flushPara();
      if (listType && listType !== "ul") flushList();
      listType = "ul"; listItems.push(m[1]);
    } else if ((m = t.match(/^\d+[.)]\s+(.*)$/))) {
      flushPara();
      if (listType && listType !== "ol") flushList();
      listType = "ol"; listItems.push(m[1]);
    } else {
      flushList();
      para.push(t);
    }
  }
  flushPara(); flushList();
  return html.join("\n").replace(/\u0001(\d+)\u0001/g, (_, i) => fences[Number(i)]);
}

/* ---------------- 弹窗通用关闭 ---------------- */

document.addEventListener("click", (e) => {
  const closeId = e.target.dataset && e.target.dataset.close;
  if (closeId) document.getElementById(closeId).hidden = true;
  if (e.target.classList && e.target.classList.contains("modal-mask")) e.target.hidden = true;
});

/* ---------------- 上传（教师，服务端强制 403） ---------------- */

$("#btn-upload")?.addEventListener("click", () => {
  els.uploadFile.value = "";
  els.progressWrap.hidden = true;
  els.progressBar.style.width = "0%";
  els.progressText.textContent = "0%";
  els.uploadModal.hidden = false;
});

els.uploadSubmit.addEventListener("click", () => {
  const file = els.uploadFile.files[0];
  if (!file) { toast("请先选择 .txt / .md 文件", "err"); return; }
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/materials");
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) xhr.setRequestHeader("Authorization", "Bearer " + token);
  els.progressWrap.hidden = false;
  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      els.progressBar.style.width = pct + "%";
      els.progressText.textContent = pct + "%";
    }
  };
  xhr.onload = () => {
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      els.uploadModal.hidden = true;
      showLogin();
      return;
    }
    if (xhr.status === 201) {
      toast(`《${data.title}》上传成功，已入知识库`, "ok");
      els.uploadModal.hidden = true;
      loadMaterials(els.search.value.trim());
    } else {
      toast(data.error || `上传失败（${xhr.status}）`, "err");
    }
  };
  xhr.onerror = () => toast("网络错误，上传失败", "err");
  const fd = new FormData();
  fd.append("file", file);
  const strategy = $("#upload-strategy").value;
  fd.append("strategy", strategy);
  if (strategy === "custom") {
    fd.append("max_len", $("#upload-maxlen").value);
    fd.append("overlap", $("#upload-overlap").value);
    fd.append("sep", $("#upload-sep").value);
    if ($("#upload-remove-urls").checked) fd.append("remove_urls", "true");
    if ($("#upload-collapse").checked) fd.append("collapse_ws", "true");
  }
  xhr.send(fd);
});

$("#upload-strategy").addEventListener("change", () => {
  $("#custom-fields").hidden = $("#upload-strategy").value !== "custom";
});

$("#btn-reindex").addEventListener("click", async () => {
  if (!state.detailId) return;
  const r = await api("POST", `/api/materials/${state.detailId}/reindex`, {
    strategy: $("#reindex-strategy").value,
  });
  if (r.ok) {
    toast(`索引已重建，切片 ${r.data.chunks_total}，失败 ${r.data.chunks_failed}`, "ok");
  } else {
    toast((r.data && r.data.error) || "重建失败", "err");
  }
});

/* ---------------- 主题 / 视图 ---------------- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#btn-theme").textContent = theme === "dark" ? "☀️" : "🌙";
  localStorage.setItem("cc-theme", theme);
}
$("#btn-theme").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});
$("#btn-view").addEventListener("click", () => {
  state.view = state.view === "grid" ? "list" : "grid";
  $("#btn-view").textContent = state.view === "grid" ? "☰" : "▤";
  localStorage.setItem("cc-view", state.view);
  renderMaterials();
});

/* ---------------- 命令面板 ---------------- */

function buildCommands() {
  const cmds = [
    { name: "🔍 聚焦搜索", hint: "/", run: () => els.search.focus() },
    { name: "🌓 切换深浅色", hint: "theme", run: () => $("#btn-theme").click() },
    { name: "▤ 切换列表/网格", hint: "view", run: () => $("#btn-view").click() },
    { name: "💬 知识问答", hint: "ask", run: () => showKnowledge("ask") },
    { name: "🚪 登出", hint: "logout", run: () => $("#btn-logout").click() },
  ];
  if (state.me && state.me.role === "teacher") {
    cmds.splice(2, 0, { name: "⬆️ 上传材料", hint: "upload", run: () => $("#btn-upload").click() });
  }
  return cmds;
}

function openPalette() {
  state.cmdItems = buildCommands();
  state.cmdActive = 0;
  els.cmdInput.value = "";
  renderCmd();
  els.cmdPalette.hidden = false;
  els.cmdInput.focus();
}
function closePalette() { els.cmdPalette.hidden = true; }
function renderCmd() {
  const q = els.cmdInput.value.trim().toLowerCase();
  const shown = state.cmdItems
    .map((c, i) => ({ c, i }))
    .filter(({ c }) => !q || c.name.toLowerCase().includes(q) || c.hint.includes(q));
  state.cmdActive = 0;
  els.cmdList.innerHTML = shown.map(({ c, i }, k) =>
    `<li data-i="${i}" class="${k === 0 ? "active" : ""}">
       <span>${c.name}</span><span class="cmd-hint">${c.hint}</span></li>`).join("");
  els.cmdList.querySelectorAll("li").forEach((li) => {
    li.addEventListener("click", () => { state.cmdItems[Number(li.dataset.i)].run(); closePalette(); });
  });
  els.cmdList._shown = shown.map(({ i }) => i);
}
els.cmdInput.addEventListener("input", renderCmd);
els.cmdPalette.addEventListener("click", (e) => { if (e.target === els.cmdPalette) closePalette(); });
$("#btn-cmd").addEventListener("click", openPalette);
els.cmdInput.addEventListener("keydown", (e) => {
  const ids = els.cmdList._shown || [];
  if (e.key === "Escape") closePalette();
  else if (e.key === "Enter" && ids.length) { state.cmdItems[ids[state.cmdActive] ?? ids[0]].run(); closePalette(); }
  else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const n = ids.length;
    state.cmdActive = (state.cmdActive + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    els.cmdList.querySelectorAll("li").forEach((li, k) => li.classList.toggle("active", k === state.cmdActive));
  }
});

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (state.me) els.cmdPalette.hidden ? openPalette() : closePalette();
  }
  if (e.key === "Escape") {
    els.cmdPalette.hidden = true;
    els.uploadModal.hidden = true;
    els.detailModal.hidden = true;
  }
});

/* ---------------- Toast ---------------- */

function toast(msg, kind = "") {
  const div = document.createElement("div");
  div.className = "toast " + kind;
  div.textContent = msg;
  els.toastStack.appendChild(div);
  setTimeout(() => { div.style.opacity = "0"; setTimeout(() => div.remove(), 250); }, 3200);
}

/* ---------------- 知识检索 / 问答 ---------------- */

function showMaterials() {
  $("#materials-topbar").hidden = false;
  $("#knowledge-topbar").hidden = true;
  $("#materials-panel").hidden = false;
  $("#knowledge-panel").hidden = true;
}

function showKnowledge(tab) {
  if (!state.me) return;
  $("#materials-topbar").hidden = true;
  $("#knowledge-topbar").hidden = false;
  $("#materials-panel").hidden = true;
  $("#knowledge-panel").hidden = false;
  setKnowledgeTab(tab || "ask");
}

function setKnowledgeTab(tab) {
  const ask = tab !== "search";
  $("#tab-ask").classList.toggle("active", ask);
  $("#tab-search").classList.toggle("active", !ask);
  $("#ask-pane").hidden = !ask;
  $("#search-pane").hidden = ask;
}

$("#btn-knowledge").addEventListener("click", () => showKnowledge("ask"));
$("#btn-back-materials").addEventListener("click", showMaterials);
$("#tab-ask").addEventListener("click", () => setKnowledgeTab("ask"));
$("#tab-search").addEventListener("click", () => setKnowledgeTab("search"));
$("#btn-clear-ask").addEventListener("click", () => {
  state.askMessages = [];
  $("#ask-thread").innerHTML = "";
});

document.querySelectorAll(".mode-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.searchMode = btn.dataset.mode;
    document.querySelectorAll(".mode-btn").forEach((el) => {
      el.classList.toggle("active", el === btn);
    });
  });
});

function renderMarked(text) {
  return escapeHtml(text).replace(/\[(\d+)\]/g, '<span class="cite-mark">[$1]</span>');
}

function citationHtml(item, order) {
  const n = order + 1;
  return `
    <li>
      <button type="button" class="cite-link" data-id="${item.material_id}">
        ${n}. [${n}] ${escapeHtml(item.material_title)} 切片 ${item.chunk_index} · 偏移 ${item.char_start}–${item.char_end}
      </button>
      <p class="cite-excerpt">${escapeHtml(item.excerpt || "")}</p>
    </li>`;
}

function bindMaterialLinks(root) {
  root.querySelectorAll("[data-id]").forEach((node) => {
    node.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      openDetail(Number(node.dataset.id));
    });
  });
}

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#retrieval-q").value.trim();
  if (!q) return;
  const r = await api("GET", "/api/search?q=" + encodeURIComponent(q) + "&mode=" + encodeURIComponent(state.searchMode));
  const box = $("#retrieval-results");
  if (!r.ok) {
    box.innerHTML = `<p class="ask-hint">${escapeHtml((r.data && r.data.error) || "检索失败")}</p>`;
    return;
  }
  const hits = (r.data && r.data.hits) || [];
  if (!hits.length) {
    box.innerHTML = `<p class="ask-hint">没有命中本班切片。</p>`;
    return;
  }
  box.innerHTML = hits.map((hit) => `
    <article class="hit-card" data-id="${hit.material_id}">
      <div class="hit-title">${escapeHtml(hit.material_title)}</div>
      <div class="hit-meta">切片 ${hit.chunk_index} · 偏移 ${hit.char_start}–${hit.char_end}</div>
      <p class="hit-excerpt">${escapeHtml(hit.excerpt || "")}</p>
    </article>`).join("");
  bindMaterialLinks(box);
});

$("#ask-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#ask-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  const thread = $("#ask-thread");
  const pending = document.createElement("div");
  pending.className = "msg-user";
  pending.innerHTML = `<div class="msg-label">你</div><div class="bubble">${escapeHtml(question)}</div>`;
  thread.appendChild(pending);
  const r = await api("POST", "/api/ask", { question, messages: state.askMessages });
  const card = document.createElement("div");
  card.className = "answer-card";
  if (!r.ok) {
    card.innerHTML = `<div class="answer-label">暂时无法回答</div><div class="answer-body">${escapeHtml((r.data && r.data.error) || "服务暂时不可用")}</div>`;
  } else {
    const citations = r.data.citations || [];
    card.innerHTML = `
      <div class="answer-label">依据回答</div>
      <div class="answer-body">${renderMarked(r.data.answer || "")}</div>
      ${citations.length ? `<ol class="citations">${citations.map(citationHtml).join("")}</ol>` : ""}`;
    state.askMessages.push({ role: "user", content: question });
    state.askMessages.push({ role: "assistant", content: r.data.answer || "" });
    if (state.askMessages.length > 6) state.askMessages = state.askMessages.slice(-6);
  }
  thread.appendChild(card);
  bindMaterialLinks(card);
  card.scrollIntoView({ block: "nearest" });
});

$("#ask-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#ask-form").requestSubmit();
  }
});

/* ---------------- 启动：以 /api/me 为准 ---------------- */

(function init() {
  applyTheme(localStorage.getItem("cc-theme") || "light");
  state.view = localStorage.getItem("cc-view") === "grid" ? "grid" : "list";
  $("#btn-view").textContent = state.view === "grid" ? "☰" : "▤";
  api("GET", "/api/me").then((r) => r.ok ? showApp(r.data) : showLogin());
})();
