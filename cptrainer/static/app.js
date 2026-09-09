"use strict";
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
const platformName = (platform) => (platform === "cf" ? "Codeforces" : "AtCoder");
const offlineName = (status) =>
  ({ ready: "离线就绪", partial: "配图 / 附件缺失", failed: "下载待重试", pending: "等待下载" })[
    status
  ] || "等待下载";
const dateText = (seconds) =>
  seconds
    ? new Date(seconds * 1000).toLocaleString("zh-CN", {
        hour12: false,
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "手动开始";
const dateInput = (seconds) => {
  if (!seconds) return "";
  const d = new Date(seconds * 1000);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
const phase = (s) =>
  s.status === "finished"
    ? "已结束"
    : s.ended
      ? "已到时 · 待结算"
      : s.status === "running"
        ? "进行中"
        : "未开始";
let state,
  selectedId = null,
  currentSession = null,
  sessionSignature = "",
  currentTags = null,
  timeOffset = 0;
let bankPage = 1,
  bankResult = { total: 0, items: [] },
  manualMode = false,
  refreshing = false;
const selection = new Map();

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#theme").textContent = theme === "light" ? "☾ 暗色" : "☀ 明色";
  $("#theme").setAttribute("aria-label", theme === "light" ? "切换至暗色模式" : "切换至明色模式");
}
try {
  applyTheme(
    localStorage.getItem("cp-trainer-theme") ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"),
  );
} catch {
  applyTheme("light");
}
$("#theme").addEventListener("click", () => {
  const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(theme);
  try {
    localStorage.setItem("cp-trainer-theme", theme);
  } catch {
    /* Preference storage may be disabled. */
  }
});

function notice(message) {
  $("#notice").textContent = message;
  $("#notice").hidden = !message;
}
async function api(path, data) {
  const response = await fetch(
    path,
    data === undefined
      ? { cache: "no-store" }
      : {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CP-Trainer": state?.token || "" },
          body: JSON.stringify(data),
        },
  );
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失败，请重试。");
  return result;
}
async function action(work) {
  try {
    await work();
  } catch (error) {
    notice(error.message || "无法连接本机服务，请检查服务是否运行。");
  }
}
async function confirmAction(title, body) {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-body").textContent = body;
  dialog.returnValue = "cancel";
  dialog.showModal();
  await new Promise((resolve) => dialog.addEventListener("close", resolve, { once: true }));
  return dialog.returnValue === "ok";
}
document.addEventListener("click", (event) => {
  const close = event.target.closest("[data-close]");
  if (close) close.closest("dialog").close();
});

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api("/api/state");
    timeOffset = state.now * 1000 - Date.now();
    const cf = state.archive.cf,
      atc = state.archive.atcoder;
    $("#archive-summary").textContent =
      `CF ${cf.ready.toLocaleString()} / ${cf.total.toLocaleString()} 题　·　AtCoder ${atc.ready.toLocaleString()} / ${atc.total.toLocaleString()} 题离线就绪`;
    const missing = cf.total + atc.total - cf.ready - atc.ready;
    $("#sync").disabled = state.sync.running;
    $("#sync").textContent = state.sync.running ? "同步中…" : "同步 / 续传";
    $("#sync-message").textContent = state.sync.running
      ? state.sync.message
      : state.sync.errors.length
        ? state.sync.errors.join("；")
        : missing
          ? `还有 ${missing.toLocaleString()} 题未完成下载；同步可安全续传。`
          : "所有已入库题目均已离线就绪。";
    $("#sync-message").hidden = !$("#sync-message").textContent;
    $("#session-count").textContent = state.sessions.length;
    $("#clear").disabled = !state.sessions.length;
    if (selectedId && !state.sessions.some((s) => s.id === selectedId)) {
      selectedId = null;
      currentSession = null;
    }
    renderSessionList();
    if (selectedId) await loadSession();
  } finally {
    refreshing = false;
  }
}
function renderSessionList() {
  $("#session-list").innerHTML = state.sessions.length
    ? state.sessions
        .map(
          (s) =>
            `<button class="session-card ${s.id === selectedId ? "active" : ""}" data-session="${s.id}" aria-pressed="${s.id === selectedId}"><span class="chip">${platformName(s.platform)}</span><span class="phase">${phase(s)}</span><strong>${escapeHtml(s.name)}</strong><span class="card-meta">${s.problem_count} 题 · ${s.duration_minutes} 分钟<br />${dateText(s.started_at || s.scheduled_at)}</span></button>`,
        )
        .join("")
    : '<p class="list-empty">还没有比赛<br />点击“创建比赛”来组一套题。</p>';
}
$("#session-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-session]");
  if (button)
    action(async () => {
      selectedId = Number(button.dataset.session);
      currentTags = null;
      sessionSignature = "";
      renderSessionList();
      await loadSession();
    });
});
async function loadSession() {
  const id = selectedId;
  const session = await api(`/api/sessions/${id}`);
  if (selectedId !== id) return;
  currentSession = session;
  const signature = JSON.stringify(session);
  if (signature !== sessionSignature) {
    sessionSignature = signature;
    renderSession();
  }
  renderTimer();
}
function renderSession() {
  const s = currentSession;
  let controls = "";
  if (s.status === "scheduled")
    controls = `<button class="primary" data-action="start">▶ 立即开始</button><form id="schedule-form" class="schedule-form"><label>预约开始时间<input type="datetime-local" name="scheduled_at" value="${dateInput(s.scheduled_at)}" /></label><button>保存时间</button></form>`;
  if (s.status === "running")
    controls = `<button class="primary" data-action="finish">结束并结算</button><span class="subtle small">${s.ended ? "已到时，录入结果后结算。" : "可随时录入结果；结算后停止计时。"}</span>`;
  if (s.ended)
    controls += `<button data-action="tags" aria-expanded="${Boolean(currentTags)}">${currentTags ? "隐藏标签" : "显示标签"}</button>`;
  const result =
    s.status === "finished"
      ? `<dl class="results"><div><dt>比赛得分</dt><dd>${s.score?.toFixed(1) || "0"}</dd></div><div><dt>罚时（分钟）</dt><dd>${s.penalty_minutes?.toFixed(1) || "0"}</dd></div>${s.new_rating == null ? "" : `<div><dt>本地训练分</dt><dd>${s.old_rating.toFixed(0)} → ${s.new_rating.toFixed(0)}</dd></div>`}</dl>`
      : "";
  $("#session-detail").innerHTML =
    `<div class="session-heading"><div><span class="chip">${platformName(s.platform)}</span><h2>${escapeHtml(s.name)}</h2><p>${s.items.length} 道题 · ${s.duration_minutes} 分钟 · ${s.rated ? "本地计分" : "不计分"} · ${phase(s)}</p></div><div><div id="timer" class="timer"></div><div id="timer-label" class="timer-label"></div></div></div><div class="session-actions">${controls}</div>${result}<div>${s.items.map((item) => `<div class="task-row"><span class="task-slot">${String.fromCharCode(64 + item.ordinal)}</span><div><button class="task-title" data-problem="${escapeHtml(item.problem_id)}" data-platform="${item.platform}">${escapeHtml(item.title)}</button><div class="task-meta"><span>${escapeHtml(item.problem_id)}</span><span>${offlineName(item.offline)}</span><span>${item.max_points} 分</span>${item.solved ? `<span>✓ AC · ${(item.elapsed_seconds / 60).toFixed(1)} 分钟</span>` : ""}${s.ended ? `<span>难度 ${item.difficulty ?? "暂无估分"}</span>` : ""}</div><div class="tags" data-tags="${item.ordinal}"></div></div><div class="task-links"><a href="${escapeHtml(item.submit_url)}" target="_blank" rel="noreferrer">交题 ↗</a>${s.status === "running" ? `<button data-record="${item.ordinal}">录入结果</button>` : ""}</div></div>`).join("")}</div><p class="footnote">${s.ended ? "标签默认隐藏，点击“显示标签”后才会显示。" : "比赛结束后可以选择显示题目标签。"} 在线交题需要连接原评测平台。</p>`;
  renderTags();
  $("#schedule-form")?.addEventListener("submit", (event) => {
    event.preventDefault();
    const date = new FormData(event.target).get("scheduled_at");
    action(async () => {
      await api(`/api/sessions/${selectedId}/schedule`, {
        scheduled_at: date ? Math.floor(new Date(date).getTime() / 1000) : null,
      });
      notice("开始时间已保存。");
      await refresh();
    });
  });
}
function renderTimer() {
  if (!currentSession || !$("#timer")) return;
  const s = currentSession,
    now = Date.now() + timeOffset;
  let seconds, label;
  if (s.status === "finished") {
    $("#timer").textContent = "FINISHED";
    $("#timer-label").textContent = "比赛已结束";
    return;
  }
  if (s.status === "scheduled" && !s.scheduled_at) {
    $("#timer").textContent = "READY";
    $("#timer-label").textContent = "准备好后，立即开始";
    return;
  }
  if (s.status === "scheduled") {
    seconds = Math.max(0, Math.ceil(s.scheduled_at - now / 1000));
    label = "距离预约开始";
  } else {
    seconds = Math.max(0, Math.ceil(s.started_at + s.duration_minutes * 60 - now / 1000));
    label = seconds ? "剩余时间" : "已到时，请录入结果并结算";
  }
  $("#timer").textContent = [
    Math.floor(seconds / 3600),
    Math.floor((seconds % 3600) / 60),
    seconds % 60,
  ]
    .map((x) => String(x).padStart(2, "0"))
    .join(":");
  $("#timer-label").textContent = label;
}
function renderTags() {
  document.querySelectorAll("[data-tags]").forEach((node) => {
    const tags = currentTags?.[node.dataset.tags];
    node.innerHTML = tags
      ? tags.length
        ? tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join("")
        : "<span>暂无标签</span>"
      : "";
  });
}
$("#session-detail").addEventListener("click", (event) => {
  const button = event.target.closest("[data-action], [data-record], [data-problem]");
  if (!button) return;
  if (button.dataset.problem) {
    action(() => openProblem(button.dataset.platform, button.dataset.problem));
    return;
  }
  if (button.dataset.record) {
    openRecord(Number(button.dataset.record));
    return;
  }
  action(async () => {
    if (button.dataset.action === "create") openCreate(false);
    if (button.dataset.action === "start") {
      await api(`/api/sessions/${selectedId}/start`, {});
      await refresh();
    }
    if (
      button.dataset.action === "finish" &&
      (await confirmAction(
        "结束并结算比赛？",
        "请先录入本场解题结果。结算将更新本地训练分，结束后结果不可修改。",
      ))
    ) {
      await api(`/api/sessions/${selectedId}/finish`, {});
      await refresh();
    }
    if (button.dataset.action === "tags") {
      currentTags = currentTags ? null : await api(`/api/sessions/${selectedId}/tags`);
      renderSession();
      renderTimer();
    }
  });
});
$("#clear").addEventListener("click", () =>
  action(async () => {
    if (
      !(await confirmAction(
        "清空比赛列表？",
        "将清空所有比赛和比赛内的解题记录。操作前自动保存一份可恢复的数据库备份；题库与训练分保留。",
      ))
    )
      return;
    const result = await api("/api/sessions/clear", {});
    selectedId = null;
    currentSession = null;
    currentTags = null;
    $("#session-detail").innerHTML =
      '<div class="empty"><h2>比赛列表已清空</h2><p>准备好后，开始一场新的训练。</p><button class="primary" data-action="create">创建比赛</button></div>';
    notice(`比赛已清空。备份：${result.backup}`);
    await refresh();
  }),
);
$("#sync").addEventListener("click", () =>
  action(async () => {
    await api("/api/sync", {});
    notice("同步已开始；已有题目仍可离线阅读和组题。");
    await refresh();
  }),
);

function openCreate(manual) {
  manualMode = manual;
  const form = $("#create-form");
  form.elements.platform.disabled = manual;
  form.elements.count.disabled = manual;
  if (manual) {
    form.elements.platform.value = [...selection.values()][0].platform;
    form.elements.count.value = selection.size;
  }
  $("#create-help").textContent = manual
    ? `使用已选 ${selection.size} 道真实题目。含未估分题目时，请关闭本地计分。`
    : "从离线就绪的真实题目中，按当前训练分自动组题。";
  $("#create-error").textContent = "";
  $("#create-dialog").showModal();
}
$("#create-open").addEventListener("click", () => openCreate(false));
$("#manual-create").addEventListener("click", () => openCreate(true));
$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target,
    button = form.querySelector('[type="submit"]');
  button.disabled = true;
  try {
    const date = form.elements.scheduled_at.value;
    const data = {
      name: form.elements.name.value,
      platform: form.elements.platform.value,
      count: Number(form.elements.count.value),
      duration: Number(form.elements.duration.value),
      scheduled_at: date ? Math.floor(new Date(date).getTime() / 1000) : null,
      rated: form.elements.rated.checked,
    };
    if (manualMode) data.problems = [...selection.values()].map((p) => p.problem_id);
    const result = await api("/api/sessions", data);
    selectedId = result.id;
    currentTags = null;
    sessionSignature = "";
    $("#create-dialog").close();
    selection.clear();
    updateSelection();
    location.hash = "contests";
    await refresh();
  } catch (error) {
    $("#create-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

async function loadBank() {
  const params = new URLSearchParams(new FormData($("#search-form")));
  params.set("page", bankPage);
  bankResult = await api("/api/problems?" + params);
  $("#bank-count").textContent = `共 ${bankResult.total.toLocaleString()} 道题目　·　每页 40 题`;
  $("#problem-list").innerHTML = bankResult.items.length
    ? bankResult.items
        .map(
          (p) =>
            `<tr><td><input type="checkbox" data-select="${escapeHtml(p.platform + ":" + p.problem_id)}" aria-label="选择 ${escapeHtml(p.problem_id)}" ${p.offline !== "ready" ? "disabled" : ""} ${selection.has(p.platform + ":" + p.problem_id) ? "checked" : ""} /></td><td><button class="task-title" data-platform="${p.platform}" data-problem="${escapeHtml(p.problem_id)}">${escapeHtml(p.title)}</button><span class="problem-id">${escapeHtml(p.problem_id)}</span></td><td>${platformName(p.platform)}</td><td>${p.difficulty == null ? "暂无估分" : Math.round(p.difficulty)}</td><td><span class="${p.offline === "ready" ? "chip" : "subtle"}">${offlineName(p.offline)}</span></td><td>${p.published_at ? new Date(p.published_at * 1000).toISOString().slice(0, 10) : "—"}</td></tr>`,
        )
        .join("")
    : '<tr><td colspan="6" class="subtle">没有匹配的题目。调整筛选，或点击“同步 / 续传”下载真实题库。</td></tr>';
  $("#page-label").textContent = `${bankPage} / ${Math.max(1, Math.ceil(bankResult.total / 40))}`;
  $("#page-prev").disabled = bankPage === 1;
  $("#page-next").disabled = bankPage * 40 >= bankResult.total;
}
$("#search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  bankPage = 1;
  action(loadBank);
});
$("#page-prev").addEventListener("click", () => {
  bankPage--;
  action(loadBank);
});
$("#page-next").addEventListener("click", () => {
  bankPage++;
  action(loadBank);
});
$("#problem-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-problem]");
  if (button) action(() => openProblem(button.dataset.platform, button.dataset.problem));
});
$("#problem-list").addEventListener("change", (event) => {
  const key = event.target.dataset.select;
  if (!key) return;
  if (event.target.checked) {
    const p = bankResult.items.find((item) => `${item.platform}:${item.problem_id}` === key);
    if (
      selection.size >= 26 ||
      (selection.size && [...selection.values()][0].platform !== p.platform)
    ) {
      event.target.checked = false;
      notice("一场比赛请选择同一平台的 1–26 道题目。");
      return;
    }
    selection.set(key, p);
  } else selection.delete(key);
  updateSelection();
});
function updateSelection() {
  $("#manual-create").textContent = `用已选 ${selection.size} 题组比赛`;
  $("#manual-create").disabled = !selection.size;
  $("#selection-clear").hidden = !selection.size;
}
$("#selection-clear").addEventListener("click", () => {
  selection.clear();
  updateSelection();
  action(loadBank);
});

async function openProblem(platform, id) {
  $("#reader-title").textContent = "正在读取题面…";
  $("#reader-id").textContent = platformName(platform) + " / " + id;
  $("#statement").textContent = "";
  $("#reader-error").textContent = "";
  $("#reader-status").textContent = "";
  $("#reader-source").removeAttribute("href");
  $("#reader-submit").removeAttribute("href");
  $("#problem-dialog").showModal();
  try {
    const p = await api(
      `/api/problem?platform=${encodeURIComponent(platform)}&id=${encodeURIComponent(id)}`,
    );
    $("#reader-title").textContent = p.title;
    $("#reader-status").textContent = offlineName(p.offline);
    $("#reader-source").href = p.url;
    $("#reader-submit").href = p.submit_url;
    $("#reader-error").textContent =
      p.offline === "ready"
        ? ""
        : p.error?.includes("404")
          ? "原站返回 404，暂时无法保存题面。元数据已保留，后续可再次同步重试。"
          : p.html
            ? "正文已保存，部分配图或附件未下载。请同步续传。"
            : "题面尚未下载，请联网时同步；目前可以通过原题链接查看。";
    $("#statement").innerHTML = p.html || "";
    const hasEnglish = Boolean($("#statement .lang-en")),
      hasJapanese = Boolean($("#statement .lang-ja"));
    $("#language-label").hidden = !(hasEnglish && hasJapanese);
    $("#language").value = hasEnglish ? "en" : "ja";
    $("#statement").dataset.language = $("#language").value;
    $("#statement")
      .querySelectorAll("var")
      .forEach((node) => {
        katex.render(node.textContent, node, {
          throwOnError: false,
          trust: false,
          strict: "ignore",
        });
      });
    renderMathInElement($("#statement"), {
      delimiters: [
        { left: "$$$$$$", right: "$$$$$$", display: true },
        { left: "$$$", right: "$$$", display: false },
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      throwOnError: false,
      trust: false,
      strict: "ignore",
      ignoredClasses: ["katex"],
    });
  } catch (error) {
    $("#reader-error").textContent = error.message;
  }
}
$("#language").addEventListener("change", () => {
  $("#statement").dataset.language = $("#language").value;
});
function openRecord(ordinal) {
  const item = currentSession.items.find((p) => p.ordinal === ordinal),
    form = $("#record-form");
  form.elements.ordinal.value = ordinal;
  form.elements.solved.value = item.solved ? "true" : "false";
  form.elements.elapsed.value =
    item.elapsed_seconds == null ? "" : (item.elapsed_seconds / 60).toFixed(2);
  form.elements.elapsed.max = currentSession.duration_minutes;
  form.elements.wrong.value = item.wrong_attempts;
  $("#record-error").textContent = "";
  $("#record-dialog").showModal();
}
$("#record-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  try {
    await api(`/api/sessions/${selectedId}/record`, {
      ordinal: Number(form.elements.ordinal.value),
      solved: form.elements.solved.value === "true",
      elapsed_seconds:
        form.elements.elapsed.value === ""
          ? null
          : Math.round(Number(form.elements.elapsed.value) * 60),
      wrong_attempts: Number(form.elements.wrong.value),
    });
    $("#record-dialog").close();
    await refresh();
  } catch (error) {
    $("#record-error").textContent = error.message;
  }
});
function profileValue() {
  const form = $("#profile-form");
  form.elements.rating.value = Math.round(
    state.profiles.find((p) => p.platform === form.elements.platform.value).local_rating,
  );
}
$("#profile-open").addEventListener("click", () => {
  if (!state) return;
  profileValue();
  $("#profile-error").textContent = "";
  $("#profile-dialog").showModal();
});
$("#profile-form select").addEventListener("change", profileValue);
$("#profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  try {
    await api("/api/profiles", {
      platform: form.elements.platform.value,
      rating: Number(form.elements.rating.value),
    });
    $("#profile-dialog").close();
    await refresh();
  } catch (error) {
    $("#profile-error").textContent = error.message;
  }
});
function navigate() {
  const bank = location.hash === "#bank";
  $("#bank-view").hidden = !bank;
  $("#contests-view").hidden = bank;
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === (bank ? "bank" : "contests")) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (bank) action(loadBank);
}
window.addEventListener("hashchange", navigate);
navigate();
action(refresh);
setInterval(() => action(refresh), 5000);
setInterval(renderTimer, 1000);
