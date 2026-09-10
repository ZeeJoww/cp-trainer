// ==UserScript==
// @name         CP Trainer 原站提交助手
// @namespace    cp-trainer-local
// @version      0.4.0
// @description  从本机 CP Trainer 提交到自己的 AtCoder / Codeforces 账号并回传结果。
// @match        http://127.0.0.1/*
// @match        http://localhost/*
// @match        https://atcoder.jp/*
// @match        https://codeforces.com/*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        GM_addValueChangeListener
// @grant        GM_openInTab
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-end
// @noframes
// ==/UserScript==

(async () => {
  "use strict";
  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const local = ["127.0.0.1", "localhost"].includes(location.hostname);
  const reply = (data) => window.postMessage({ type: "CP_REMOTE_REPLY", ...data }, location.origin);
  const keyFor = (platform) => `cp-trainer-active-${platform}`;
  function valid(job) {
    if (
      !job ||
      !["prepare", "submit"].includes(job.kind) ||
      !["cf", "atcoder"].includes(job.platform)
    )
      return false;
    const url = new URL(job.url),
      origin = new URL(job.origin);
    const host = job.platform === "atcoder" ? "atcoder.jp" : "codeforces.com";
    const path =
      job.platform === "atcoder"
        ? `/contests/${job.contest_id}/submit`
        : `/contest/${job.contest_id}/submit`;
    return (
      url.protocol === "https:" &&
      url.host === host &&
      url.pathname === path &&
      origin.protocol === "http:" &&
      ["127.0.0.1", "localhost"].includes(origin.hostname) &&
      origin.origin === job.origin &&
      /^[\w-]+$/.test(job.contest_id) &&
      /^[\w-]+$/.test(job.problem_id) &&
      /^[\w-]+$/.test(job.problem_index) &&
      job.deadline > Date.now() / 1000 &&
      job.deadline <= Date.now() / 1000 + 901
    );
  }
  if (local) {
    GM_addValueChangeListener("cp-trainer-reply", (_key, _old, value) => {
      if (value?.origin === location.origin) reply(value);
    });
    window.addEventListener("message", async (event) => {
      if (event.source !== window || event.origin !== location.origin) return;
      const data = event.data;
      if (data?.type === "CP_REMOTE_HELLO") reply({ ready: true, version: "0.4.0" });
      if (data?.type === "CP_REMOTE_CANCEL") {
        const key = keyFor(data.platform),
          job = await GM_getValue(key);
        if (job?.id === data.id && job.origin === location.origin) await GM_deleteValue(key);
      }
      if (data?.type !== "CP_REMOTE_OPEN") return;
      try {
        const job = { ...data.job, origin: location.origin, phase: "ready" };
        if (!valid(job)) throw new Error("提交目标或有效期不合法。");
        const key = keyFor(job.platform),
          active = await GM_getValue(key);
        if (active?.kind === "submit" && active.deadline > Date.now() / 1000)
          throw new Error("此平台仍有提交在处理，请先等待或停止等待。");
        await GM_setValue(key, job);
        GM_openInTab(job.url, { active: true, insert: true });
      } catch (error) {
        reply({ connection: data.job?.connection, error: error.message });
      }
    });
    reply({ ready: true, version: "0.4.0" });
    return;
  }

  const platform = location.hostname === "atcoder.jp" ? "atcoder" : "cf",
    key = keyFor(platform);
  // ponytail: per-tab read polling; elect one poller if many open tabs hit rate limits.
  let working = false,
    lastFetch = 0;
  async function clear(job) {
    const active = await GM_getValue(key);
    if (active?.id === job.id && active?.connection === job.connection) await GM_deleteValue(key);
  }
  function banner(message, job) {
    let box = document.getElementById("cp-trainer-bridge");
    if (!box) {
      box = document.createElement("aside");
      box.id = "cp-trainer-bridge";
      box.style.cssText =
        "position:fixed;bottom:16px;right:16px;z-index:2147483647;max-width:460px;padding:16px;background:#173f30;color:white;border:1px solid #7aca9d;border-radius:8px;font:14px/1.6 sans-serif;box-shadow:0 3px 20px #0005";
      box.setAttribute("role", "status");
      document.body.append(box);
    }
    box.replaceChildren(document.createTextNode(`CP Trainer · ${job.problem_id}：${message} `));
    const link = document.createElement("a");
    link.href = job.url;
    link.textContent = "打开交题页";
    link.style.color = "#bbf3d1";
    box.append(link);
  }
  async function report(job, data) {
    return new Promise((resolve, reject) =>
      GM_xmlhttpRequest({
        method: "POST",
        url: `${job.origin}/api/remote/runs/${job.id}/event`,
        headers: { "Content-Type": "application/json", "X-CP-Remote": job.request_key },
        data: JSON.stringify(data),
        timeout: 15000,
        onload: (r) => {
          try {
            const value = JSON.parse(r.responseText);
            if (r.status !== 200) throw new Error(value.error || "本地记录失败");
            resolve(value);
          } catch (error) {
            reject(error);
          }
        },
        onerror: () => reject(new Error("无法连接本机 CP Trainer，请保持本地服务运行。")),
        ontimeout: () => reject(new Error("本机服务响应超时。")),
      }),
    );
  }
  async function fetchPage(url) {
    if (new URL(url).origin !== location.origin) throw new Error("原站链接无效。");
    await pause(Math.max(0, 2200 - (Date.now() - lastFetch)));
    lastFetch = Date.now();
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("原站需要登录或验证，请在浏览器中完成后继续。");
    const doc = new DOMParser().parseFromString(await response.text(), "text/html");
    if (
      /just a moment|sign in|log in/i.test(doc.title) ||
      doc.querySelector('input[type="password"]')
    )
      throw new Error("请先在原站完成登录或验证。");
    return doc;
  }
  function ownUrl(job) {
    return platform === "atcoder"
      ? `${location.origin}/contests/${job.contest_id}/submissions/me?lang=en`
      : `${location.origin}/contest/${job.contest_id}/my?locale=en`;
  }
  function submissions(doc, job) {
    const pattern =
      platform === "atcoder"
        ? new RegExp(`^/contests/${job.contest_id}/submissions/(\\d+)$`)
        : new RegExp(`^/contest/${job.contest_id}/submission/(\\d+)$`);
    const rows = [];
    for (const tr of doc.querySelectorAll("tr")) {
      const links = [...tr.querySelectorAll("a[href]")].map(
        (a) => new URL(a.getAttribute("href"), location.origin),
      );
      const problem = links.some((u) =>
        platform === "atcoder"
          ? u.pathname === `/contests/${job.contest_id}/tasks/${job.problem_id}`
          : [
              `/contest/${job.contest_id}/problem/${job.problem_index}`,
              `/problemset/problem/${job.contest_id}/${job.problem_index}`,
            ].includes(u.pathname),
      );
      if (!problem) continue;
      const link = links.find((u) => u.origin === location.origin && pattern.test(u.pathname));
      if (!link) continue;
      const status =
        platform === "atcoder"
          ? tr.querySelectorAll("td")[6]?.textContent
          : tr.querySelector(".submissionVerdictWrapper")?.textContent;
      rows.push({
        id: Number(pattern.exec(link.pathname)[1]),
        url: link.origin + link.pathname,
        status: status?.trim() || "",
      });
    }
    return rows.sort((a, b) => b.id - a.id);
  }
  function verdict(status) {
    for (const [pattern, code] of [
      [/^AC$|^Accepted$/i, "AC"],
      [/^WA$|wrong answer/i, "WA"],
      [/^TLE$|time limit/i, "TLE"],
      [/^MLE$|memory limit/i, "MLE"],
      [/^RE$|runtime error/i, "RE"],
      [/^CE$|compilation error/i, "CE"],
      [/^OLE$|output limit/i, "OLE"],
      [/^PE$|presentation error/i, "PE"],
    ])
      if (pattern.test(status)) return code;
    return null;
  }
  async function hash(source) {
    const bytes = new TextEncoder().encode(source.replace(/\r\n/g, "\n").trim());
    return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))]
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
  }
  async function form(job) {
    const task = document.querySelector(
      platform === "atcoder" ? '[name="data.TaskScreenName"]' : '[name="submittedProblemIndex"]',
    );
    if (!task) return null;
    const expected = platform === "atcoder" ? job.problem_id : job.problem_index;
    if (task.tagName === "SELECT" && ![...task.options].some((o) => o.value === expected))
      throw new Error("交题页面没有对应题目，已停止。");
    task.value = expected;
    task.dispatchEvent(new Event("change", { bubbles: true }));
    await pause(150);
    const language = [
      ...document.querySelectorAll(
        platform === "atcoder" ? 'select[name="data.LanguageId"]' : 'select[name="programTypeId"]',
      ),
    ].find((n) => {
      if (n.disabled) return false;
      // Select2 hides the native select; only hidden ancestor groups are inactive.
      for (let p = n.parentElement; p && p !== task.form; p = p.parentElement)
        if (getComputedStyle(p).display === "none") return false;
      return true;
    });
    if (!language) return null;
    return { task, language, element: task.form };
  }
  async function tick() {
    if (working) return;
    working = true;
    let job;
    try {
      job = await GM_getValue(key);
      if (job && job.deadline <= Date.now() / 1000) {
        await clear(job);
        banner("等待已超时，请到原站核对结果；不会自动重发。", job);
        return;
      }
      if (!job || !valid(job)) return;
      if (job.phase === "ready") {
        if (location.pathname !== new URL(job.url).pathname) {
          banner("请完成登录，再打开交题页继续。", job);
          return;
        }
        const controls = await form(job);
        if (!controls) {
          banner("请先完成原站登录或验证。", job);
          return;
        }
        if (job.kind === "prepare") {
          const languages = [...controls.language.options]
            .filter((o) => /^\d+$/.test(o.value) && !o.disabled)
            .map((o) => ({ id: o.value, name: o.textContent.trim() }));
          await GM_setValue("cp-trainer-reply", {
            origin: job.origin,
            connection: job.connection,
            platform,
            problem_id: job.problem_id,
            languages,
          });
          await clear(job);
          banner("连接完成，返回本地页面选择语言并提交。", job);
          return;
        }
        if (![...controls.language.options].some((o) => o.value === job.language && !o.disabled))
          throw new Error("原站语言列表已变化，请返回本地重新连接并选择语言。");
        const old = await fetchPage(ownUrl(job));
        if (!old.querySelector("table"))
          throw new Error("无法核对自己的提交列表，已停止发送，请检查原站登录或验证。");
        job.before = Math.max(0, ...submissions(old, job).map((r) => r.id));
        controls.language.value = job.language;
        controls.language.dispatchEvent(new Event("change", { bubbles: true }));
        for (const select of controls.element.querySelectorAll("select")) {
          if (select.name === controls.language.name)
            select.disabled = select !== controls.language;
        }
        const source = controls.element?.querySelector(
          'textarea[name="sourceCode"],textarea[name="source"],#sourceCodeTextarea',
        );
        if (!source) throw new Error("无法识别原站代码输入框，请使用原站表单提交。");
        // Use the site's editor API when present, then its ordinary submit button.
        const editors = unsafeWindow.document.querySelectorAll(".CodeMirror");
        for (const editor of editors) if (editor.CodeMirror) editor.CodeMirror.setValue(job.source);
        if (unsafeWindow.ace && document.querySelector(".ace_editor"))
          unsafeWindow.ace.edit(document.querySelector(".ace_editor").id).setValue(job.source, -1);
        source.value = job.source;
        source.dispatchEvent(new Event("input", { bubbles: true }));
        source.dispatchEvent(new Event("change", { bubbles: true }));
        const button = controls.element.querySelector(
          'button[type="submit"],input[type="submit"],button:not([type])',
        );
        if (!button || button.disabled || !controls.element.checkValidity())
          throw new Error("原站表单尚未就绪，请检查登录、验证码及必填项。");
        const claimed = await report(job, { phase: "claim" });
        if (!claimed.allowed) {
          banner("此提交已处理或已取消，不会重复发送。", job);
          return;
        }
        job.phase = "sending";
        await GM_setValue(key, job);
        banner("正在提交，之后自动回传结果；请保持此标签页打开。", job);
        button.click();
        return;
      }
      const doc = await fetchPage(ownUrl(job));
      const rows = submissions(doc, job).filter((r) => r.id > job.before);
      let selected = job.remote_url ? rows.find((r) => r.url === job.remote_url) : null;
      if (!job.remote_url) {
        for (const row of rows.slice(0, 5)) {
          const detail = await fetchPage(
            row.url + (platform === "atcoder" ? "?lang=en" : "?locale=en"),
          );
          const code = detail.querySelector("#submission-code,#program-source-text");
          if (code && (await hash(code.textContent)) === (await hash(job.source))) {
            selected = row;
            break;
          }
        }
        if (!selected) {
          banner("等待原站确认收件。若页面要求验证码，请手动完成；不会自动重复提交。", job);
          return;
        }
        job.remote_url = selected.url;
        job.source_hash = await hash(job.source);
        job.phase = "sent";
        await GM_setValue(key, job);
      }
      const result = selected && verdict(selected.status);
      const saved = await report(job, {
        phase: result ? "result" : "sent",
        url: job.remote_url,
        source_hash: job.source_hash,
        verdict: result,
        message: result ? `原站结果：${selected.status}` : undefined,
      });
      if (result || saved.allowed === false) {
        await clear(job);
        banner(
          result ? `原站结果 ${result} 已保存到本地。` : "本地已停止等待，请在原站查看结果。",
          job,
        );
      } else banner(`原站已收到，${selected?.status || "正在评测"}；请保持标签页打开。`, job);
    } catch (error) {
      if (job) banner(error.message, job);
      // Login/network interruptions keep the tab recoverable; never retry the submit POST.
    } finally {
      working = false;
    }
  }
  await tick();
  setInterval(tick, 3000);
})();
