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
    if (location.hash === "#judge") await loadJudgePage();
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
    action(() =>
      openProblem(
        button.dataset.platform,
        button.dataset.problem,
        currentSession?.status === "running" && !currentSession.ended ? currentSession.id : null,
      ),
    );
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
            `<tr><td><input type="checkbox" data-select="${escapeHtml(p.platform + ":" + p.problem_id)}" aria-label="选择 ${escapeHtml(p.problem_id)}" ${p.offline !== "ready" ? "disabled" : ""} ${selection.has(p.platform + ":" + p.problem_id) ? "checked" : ""} /></td><td><button class="task-title" data-platform="${p.platform}" data-problem="${escapeHtml(p.problem_id)}">${escapeHtml(p.title)}</button><span class="problem-id">${escapeHtml(p.problem_id)}</span>${p.judge_cases ? `<span class="chip">可评测 · ${p.judge_cases} 个测试点</span>` : ""}</td><td>${platformName(p.platform)}</td><td>${p.difficulty == null ? "暂无估分" : Math.round(p.difficulty)}</td><td><span class="${p.offline === "ready" ? "chip" : "subtle"}">${offlineName(p.offline)}</span></td><td>${p.published_at ? new Date(p.published_at * 1000).toISOString().slice(0, 10) : "—"}</td></tr>`,
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

let readerProblem = null,
  readerSession = null;
async function openProblem(platform, id, sessionId = null) {
  readerProblem = null;
  readerSession = sessionId;
  $("#reader-judge").disabled = true;
  $("#reader-remote").disabled = true;
  $("#reader-judge-info").textContent = "";
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
    readerProblem = p;
    $("#reader-remote").disabled = false;
    $("#reader-judge").disabled = !p.judge || p.judge.checker === "unsupported";
    $("#reader-judge-info").textContent = p.judge
      ? `${p.judge.case_count} 个存档测试点 · ${p.judge.time_limit_ms / 1000} 秒 / ${p.judge.memory_limit_mb} MiB · ${p.judge.reason}`
      : "此题暂未保存测试数据。可在“评测记录”页下载存档，或提交到原站。";
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
const verdictName = (verdict) =>
  ({
    QUEUED: "等待评测",
    JUDGING: "正在评测",
    AC: "AC · 已通过",
    WA: "WA · 答案不匹配",
    TLE: "TLE · 超时",
    MLE: "MLE · 内存超限",
    RE: "RE · 运行错误",
    CE: "CE · 编译错误",
    OLE: "OLE · 输出超限",
    SE: "评测环境 / 数据错误",
    CANCELLED: "已取消",
    INTERRUPTED: "服务重启，评测中断",
    FAILED: "未能提交",
    UNKNOWN: "结果待原站核对",
    PE: "PE · 输出格式错误",
  })[verdict] || verdict;
let judgeProblem = null,
  judgeSession = null,
  judgeId = null,
  judgeSignature = "",
  judgePolling = false,
  judgeMode = "local",
  judgeBusy = false,
  remoteReady = false,
  remoteConnection = null,
  remoteRequestKey = null;
const remoteLanguages = new Map();
const languageName = (run) =>
  run.result.language_name || { cpp17: "C++17", python3: "Python 3" }[run.language] || run.language;
function judgeEnabled() {
  return judgeMode === "remote"
    ? remoteReady &&
        Boolean(remoteLanguages.get(`${judgeProblem.platform}:${judgeProblem.problem_id}`)?.length)
    : judgeProblem?.judge && judgeProblem.judge.checker !== "unsupported";
}
function setJudgeLanguages() {
  const select = $("#judge-form").elements.language,
    previous = select.value;
  const languages =
    judgeMode === "remote"
      ? remoteLanguages.get(`${judgeProblem.platform}:${judgeProblem.problem_id}`) || []
      : [
          { id: "cpp17", name: "C++17" },
          { id: "python3", name: "Python 3" },
        ];
  select.innerHTML = languages.length
    ? languages
        .map((l) => `<option value="${escapeHtml(l.id)}">${escapeHtml(l.name)}</option>`)
        .join("")
    : '<option value="">请先连接原站</option>';
  if (languages.some((l) => l.id === previous)) select.value = previous;
  $("#judge-form [type=submit]").disabled = judgeBusy || !judgeEnabled();
}
window.addEventListener("message", (event) => {
  if (
    event.source !== window ||
    event.origin !== location.origin ||
    event.data?.type !== "CP_REMOTE_REPLY"
  )
    return;
  const data = event.data;
  if (data.ready) {
    remoteReady = true;
    $("#remote-connect").disabled = judgeBusy;
    if (!remoteConnection)
      $("#remote-status").textContent =
        judgeProblem && remoteLanguages.has(`${judgeProblem.platform}:${judgeProblem.problem_id}`)
          ? "助手已连接，可选择语言并提交。"
          : "助手已就绪。点击连接，读取此题当前支持的语言。";
  }
  if (data.connection && data.connection === remoteConnection) {
    if (data.error) $("#remote-status").textContent = data.error;
    else if (
      data.platform === judgeProblem?.platform &&
      data.problem_id === judgeProblem.problem_id &&
      Array.isArray(data.languages)
    ) {
      const languages = data.languages
        .filter((l) => /^\d{1,8}$/.test(l.id) && typeof l.name === "string")
        .slice(0, 500);
      remoteLanguages.set(`${data.platform}:${data.problem_id}`, languages);
      $("#remote-status").textContent = languages.length
        ? `已连接 ${platformName(data.platform)}，请选择编程语言。`
        : "原站未返回可用语言，请检查原站页面后重试。";
      setJudgeLanguages();
    }
  }
});
$("#remote-connect").addEventListener("click", () =>
  action(async () => {
    remoteConnection = crypto.randomUUID();
    const job = await api("/api/remote/prepare", {
      platform: judgeProblem.platform,
      problem_id: judgeProblem.problem_id,
    });
    $("#remote-status").textContent =
      "已打开原站。完成登录或验证后，点击原站右下角的“打开交题页”。";
    window.postMessage(
      {
        type: "CP_REMOTE_OPEN",
        job: {
          ...job,
          kind: "prepare",
          connection: remoteConnection,
          deadline: Math.floor(Date.now() / 1000) + 900,
        },
      },
      location.origin,
    );
  }),
);

async function loadJudgePage() {
  const [data, runs] = await Promise.all([api("/api/judge/status"), api("/api/judge/runs")]);
  const dates = data.contests.map((c) => c.started_at);
  const range = dates.length
    ? `${new Date(Math.min(...dates) * 1000).toISOString().slice(0, 10)} 至 ${new Date(Math.max(...dates) * 1000).toISOString().slice(0, 10)}`
    : "尚未下载";
  $("#testdata-summary").textContent =
    `${data.ready} / ${data.total || 100} 场已下载 · ${data.problems} 题 · ${data.cases.toLocaleString()} 个测试点 · ${(data.bytes / 1024 ** 3).toFixed(2)} GiB`;
  $("#testdata-progress").textContent = `${range}。${
    data.running
      ? data.sync.message || "下载进行中，关闭网页后可继续。"
      : data.sync.error ||
        `可直接评测 ${Object.entries(data.checkers)
          .filter(([key]) => key !== "unsupported")
          .reduce(
            (sum, [, count]) => sum + count,
            0,
          )} 题；${data.checkers.unsupported || 0} 题因特殊规则或数据缺失暂不可评测。`
  }`;
  $("#testdata-sync").disabled = data.running;
  $("#testdata-sync").textContent = data.running ? "测试数据下载中…" : "下载最近 100 场 / 续传";
  $("#judge-environment").textContent = data.runtime.available
    ? `运行环境已就绪：${data.runtime.versions.replaceAll("\n", " · ")}`
    : data.runtime.message;
  $("#testdata-contests").innerHTML = data.contests.length
    ? data.contests
        .map(
          (c) =>
            `<tr><td><a href="#bank" data-contest-filter="${escapeHtml(c.contest_id)}">${escapeHtml(c.contest_id)}</a></td><td>${new Date(c.started_at * 1000).toISOString().slice(0, 10)}</td><td>${c.problem_count} / ${c.case_count}</td><td>${escapeHtml({ ready: "已下载", partial: "已下载，原存档有缺失", downloading: "下载中", pending: "等待下载", failed: "失败，可重试" }[c.status])}${c.error ? `<br /><span class="form-error">${escapeHtml(c.error)}</span>` : ""}</td></tr>`,
        )
        .join("")
    : '<tr><td colspan="4">尚未下载测试数据，点击上方下载按钮开始。</td></tr>';
  $("#judge-history").innerHTML = runs.items.length
    ? `<table><thead><tr><th>提交</th><th>题目</th><th>语言</th><th>结果</th><th>评测方式</th><th>时间</th></tr></thead><tbody>${runs.items.map((r) => `<tr><td><button data-judge-run="${r.id}">#${r.id} 查看</button></td><td>${escapeHtml(r.problem_id)}</td><td>${escapeHtml(languageName(r))}</td><td>${verdictName(r.verdict)}</td><td>${r.engine === "remote" ? platformName(r.platform) : `离线 · ${r.passed} / ${r.total}`}</td><td>${dateText(r.submitted_at)}</td></tr>`).join("")}</tbody></table>`
    : '<p class="subtle">还没有提交。打开本地题目，选择“提交到原站”或“离线评测”。</p>';
}
$("#testdata-sync").addEventListener("click", () =>
  action(async () => {
    await api("/api/testdata/sync", {});
    await loadJudgePage();
  }),
);
$("#judge-environment-check").addEventListener("click", () =>
  action(async () => {
    await api("/api/judge/environment", {});
    await loadJudgePage();
  }),
);
$("#testdata-contests").addEventListener("click", (event) => {
  const link = event.target.closest("[data-contest-filter]");
  if (link) {
    $("#search-form").elements.q.value = link.dataset.contestFilter;
    $("#search-form").elements.platform.value = "atcoder";
    bankPage = 1;
  }
});
function showJudge(problem, sessionId = null, mode = "local") {
  judgeProblem = problem;
  judgeSession = sessionId;
  judgeId = null;
  judgeSignature = "";
  judgeMode = mode;
  judgeBusy = false;
  remoteConnection = null;
  remoteRequestKey = crypto.randomUUID();
  $("#judge-title").textContent =
    `${mode === "remote" ? "提交到 " + platformName(problem.platform) : "离线评测"} · ${problem.problem_id}`;
  $("#judge-error").textContent = "";
  $("#judge-result").textContent = "";
  $("#judge-cancel").hidden = true;
  $("#remote-setup").hidden = mode !== "remote";
  $("#remote-instructions").open = !remoteReady;
  $("#remote-connect").disabled = !remoteReady;
  $("#remote-status").textContent = remoteReady
    ? "助手已就绪。点击连接，读取此题当前支持的语言。"
    : "未检测到助手。安装并启用后刷新本页。";
  $("#judge-form [type=submit]").textContent = mode === "remote" ? "提交到原站" : "提交并评测";
  $("#judge-cancel").textContent = mode === "remote" ? "停止等待" : "取消评测";
  setJudgeLanguages();
  window.postMessage({ type: "CP_REMOTE_HELLO" }, location.origin);
  $("#judge-session-label").hidden = !sessionId;
  $("#judge-form").elements.session.checked = Boolean(sessionId);
  $("#judge-rules").textContent =
    mode === "remote"
      ? "代码发送到你在原站登录的账号，结果自动保存到本地。登录失效或验证码需手动完成；停止等待不能撤回原站提交。"
      : problem.judge
        ? `${problem.judge.case_count} 个测试点；每点 ${problem.judge.time_limit_ms / 1000} 秒 / ${problem.judge.memory_limit_mb} MiB。${problem.judge.reason} 首个失败点后停止。`
        : "此题没有可用测试数据。";
  if (!$("#judge-dialog").open) $("#judge-dialog").showModal();
}
$("#reader-judge").addEventListener("click", () => {
  if (readerProblem) showJudge(readerProblem, readerSession);
});
$("#reader-remote").addEventListener("click", () => {
  if (readerProblem) showJudge(readerProblem, readerSession, "remote");
});
$("#judge-file").addEventListener("change", () =>
  action(async () => {
    const file = $("#judge-file").files[0];
    if (!file) return;
    if (file.size > 128 * 1024) throw new Error("代码文件超过 128 KiB。");
    $("#judge-source").value = await file.text();
    if (judgeMode === "local")
      $("#judge-form").elements.language.value = /\.py$/i.test(file.name) ? "python3" : "cpp17";
  }),
);
$("#judge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target,
    button = form.querySelector("[type=submit]");
  button.disabled = true;
  $("#judge-error").textContent = "";
  try {
    const run = await api(judgeMode === "remote" ? "/api/remote/runs" : "/api/judge/runs", {
      platform: judgeProblem.platform,
      problem_id: judgeProblem.problem_id,
      language: form.elements.language.value,
      source: form.elements.source.value,
      session_id: form.elements.session.checked ? judgeSession : null,
      ...(judgeMode === "remote"
        ? {
            request_key: remoteRequestKey || (remoteRequestKey = crypto.randomUUID()),
            language_name: form.elements.language.selectedOptions[0].textContent,
          }
        : {}),
    });
    judgeId = run.id;
    judgeSignature = "";
    if (judgeMode === "remote")
      window.postMessage(
        { type: "CP_REMOTE_OPEN", job: { ...run, kind: "submit", connection: remoteConnection } },
        location.origin,
      );
    await pollJudge();
  } catch (error) {
    $("#judge-error").textContent = error.message;
    button.disabled = false;
  }
});
$("#judge-cancel").addEventListener("click", () =>
  action(async () => {
    if (judgeId) await api(`/api/judge/runs/${judgeId}/cancel`, {});
    if (judgeMode === "remote")
      window.postMessage(
        { type: "CP_REMOTE_CANCEL", id: judgeId, platform: judgeProblem.platform },
        location.origin,
      );
    await pollJudge();
  }),
);
function renderJudge(run) {
  const signature = JSON.stringify(run);
  if (signature === judgeSignature) return;
  judgeSignature = signature;
  const busy = ["QUEUED", "JUDGING"].includes(run.verdict),
    result = run.result;
  judgeBusy = busy;
  if (!busy) remoteRequestKey = null;
  $("#judge-form [type=submit]").disabled = busy || !judgeEnabled();
  $("#remote-connect").disabled = busy || !remoteReady;
  $("#judge-cancel").hidden = !busy;
  $("#judge-result").innerHTML =
    `<h3>${escapeHtml(verdictName(run.verdict))}</h3><p>提交 #${run.id} · ${run.engine === "remote" ? `${platformName(run.platform)} · ${escapeHtml(languageName(run))}` : `已执行 ${result.cases?.length || 0} / ${run.total} 个测试点，已通过 ${run.passed} 个。`}</p>${result.remote_url ? `<p><a href="${escapeHtml(result.remote_url)}" target="_blank" rel="noreferrer">查看原站提交与结果 ↗</a></p>` : ""}${result.message ? `<pre class="judge-message">${escapeHtml(result.message)}</pre>` : ""}${(
      result.cases || []
    )
      .map(
        (c) =>
          `<details><summary>${escapeHtml(c.name)} · ${verdictName(c.verdict)} · ${c.elapsed_ms} ms</summary>${[
            ["input", "输入（前 2,000 字节）"],
            ["expected", "标准输出（前 2,000 字节）"],
            ["actual", "程序输出（前 2,000 字节）"],
            ["stderr", "错误信息"],
          ]
            .filter(([key]) => c[key] !== undefined && c[key] !== "")
            .map(([key, label]) => `<p>${label}</p><pre>${escapeHtml(c[key])}</pre>`)
            .join("")}</details>`,
      )
      .join("")}`;
}
async function pollJudge() {
  if (!judgeId || judgePolling || !$("#judge-dialog").open) return;
  judgePolling = true;
  const id = judgeId;
  try {
    const run = await api(`/api/judge/runs/${id}`);
    if (judgeId === id) renderJudge(run);
  } finally {
    judgePolling = false;
  }
}
$("#judge-history").addEventListener("click", (event) => {
  const button = event.target.closest("[data-judge-run]");
  if (button)
    action(async () => {
      const run = await api(`/api/judge/runs/${button.dataset.judgeRun}`);
      const problem = await api(
        `/api/problem?platform=${run.platform}&id=${encodeURIComponent(run.problem_id)}`,
      );
      showJudge(problem, null, run.engine);
      $("#judge-source").value = run.source;
      if (
        run.engine === "remote" &&
        ![...$("#judge-form").elements.language.options].some((l) => l.value === run.language)
      )
        $("#judge-form").elements.language.add(new Option(languageName(run), run.language));
      $("#judge-form").elements.language.value = run.language;
      judgeId = run.id;
      renderJudge(run);
    });
});
setInterval(() => action(pollJudge), 1000);
window.postMessage({ type: "CP_REMOTE_HELLO" }, location.origin);

function navigate() {
  const view = ["bank", "judge"].includes(location.hash.slice(1))
    ? location.hash.slice(1)
    : "contests";
  for (const name of ["bank", "judge", "contests"]) $(`#${name}-view`).hidden = view !== name;
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  if (view === "bank") action(loadBank);
  if (view === "judge") action(loadJudgePage);
}
window.addEventListener("hashchange", navigate);
navigate();
action(refresh);
setInterval(() => action(refresh), 5000);
setInterval(renderTimer, 1000);
