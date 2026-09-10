import { test, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { once } from "node:events";

let server, directory, url;
test.beforeEach(async ({ page }) => {
  directory = await mkdtemp(join(tmpdir(), "cp-trainer-test-"));
  server = spawn(
    process.platform === "win32" ? "python" : "python3",
    [
      "-c",
      "import sys;sys.path.insert(0,'tests');from test_web import serve_fixture;serve_fixture(sys.argv[1])",
      directory,
    ],
    { cwd: resolve(import.meta.dirname, ".."), stdio: ["ignore", "pipe", "pipe"] },
  );
  url = await new Promise((resolveUrl, reject) => {
    let output = "";
    server.stdout.on("data", (chunk) => {
      output += chunk;
      if (output.includes("\n")) resolveUrl(output.trim());
    });
    server.on("error", reject);
    server.on("exit", (code) => reject(new Error(`Server exited: ${code}`)));
  });
  await page.route("**/*", (route) =>
    new URL(route.request().url()).origin === url ? route.continue() : route.abort(),
  );
  await page.goto(url);
  await expect(page.locator("#archive-summary")).toContainText("CF 8 / 8");
});
test.afterEach(async () => {
  if (server && server.exitCode === null) {
    server.kill();
    await once(server, "exit");
  }
  await rm(directory, { recursive: true, force: true });
});

test("create, schedule, start, score, reveal tags only on request, and clear", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await expect(page.locator("#session-count")).toHaveText("0");
  await page.locator("#create-open").click();
  await page.locator('#create-form [name="name"]').fill("专注练习");
  await page.locator('#create-form [name="count"]').fill("3");
  await page.locator('#create-form [type="submit"]').click();
  await expect(page.locator("#timer")).toHaveText("READY");
  await expect(page.getByRole("button", { name: "显示标签", exact: true })).toHaveCount(0);
  const future = new Date(Date.now() + 3600000),
    input = new Date(future.getTime() - future.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  await page.locator("#schedule-form input").fill(input);
  await page.getByRole("button", { name: "保存时间", exact: true }).click();
  await expect(page.locator("#timer-label")).toHaveText("距离预约开始");
  await page.getByRole("button", { name: "立即开始", exact: false }).click();
  await expect(page.locator("#timer-label")).toHaveText("剩余时间");
  await expect(page.locator(".tags")).toHaveText(["", "", ""]);
  await page.locator("[data-record]").first().click();
  await page.locator('#record-form [name="solved"]').selectOption("true");
  await page.locator('#record-form [name="elapsed"]').fill("12");
  await page.getByRole("button", { name: "保存结果", exact: true }).click();
  await expect(page.locator(".task-meta").first()).toContainText("AC");
  await page.getByRole("button", { name: "结束并结算", exact: true }).click();
  await page.locator('#confirm-dialog button[value="ok"]').click();
  await expect(page.locator("#timer")).toHaveText("FINISHED");
  await expect(page.locator(".tags")).toHaveText(["", "", ""]);
  await page.getByRole("button", { name: "显示标签", exact: true }).click();
  await expect(page.locator(".tags").first()).toContainText("graphs");
  await page.reload();
  await page.locator("[data-session]").first().click();
  await expect(page.locator(".tags")).toHaveText(["", "", ""]);
  await page.locator("#clear").click();
  await page.locator('#confirm-dialog button[value="ok"]').click();
  await expect(page.locator("#session-count")).toHaveText("0");
  expect(errors).toEqual([]);
});

for (const platform of ["atcoder", "cf"])
  test(`remote ${platform}: native form, source identity, result and no repeat`, async ({
    page,
    context,
  }, testInfo) => {
    test.setTimeout(75000);
    const errors = [],
      storage = new Map();
    let posts = 0,
      storedSource = "",
      polls = 0,
      signedIn = false;
    const origin = platform === "atcoder" ? "https://atcoder.jp" : "https://codeforces.com";
    const contest = platform === "atcoder" ? "abc350" : "2000";
    const pid = platform === "atcoder" ? "abc350_a" : "2000A";
    const prefix = platform === "atcoder" ? `/contests/${contest}` : `/contest/${contest}`;
    const ownPath = prefix + (platform === "atcoder" ? "/submissions/me" : "/my");
    const detailPath = prefix + (platform === "atcoder" ? "/submissions/" : "/submission/");
    const problemPath = prefix + (platform === "atcoder" ? `/tasks/${pid}` : "/problem/A");
    const taskName = platform === "atcoder" ? "data.TaskScreenName" : "submittedProblemIndex";
    const langName = platform === "atcoder" ? "data.LanguageId" : "programTypeId";
    const sourceName = platform === "atcoder" ? "sourceCode" : "source";
    const escape = (s) =>
      s.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
    const row = (id, verdict) =>
      `<tr><td>time</td><td><a href="${problemPath}">A</a></td><td>me</td><td>Python</td><td>0</td><td>10</td><td class="submissionVerdictWrapper">${verdict}</td><td><a href="${detailPath}${id}">Detail</a></td></tr>`;
    // All original-site traffic is synthetic; this test cannot submit to a real account.
    await context.route("**/*", async (route) => {
      const u = new URL(route.request().url());
      if (u.origin === url) return route.continue();
      if (u.origin !== origin) return route.abort();
      const respond = (body) =>
        route.fulfill({
          contentType: "text/html; charset=utf-8",
          body: `<!doctype html><meta charset="utf-8"><title>Judge check</title>${body}`,
        });
      if (u.pathname === prefix + "/submit") {
        if (!signedIn)
          return route.fulfill({ status: 302, headers: { location: origin + "/login" } });
        if (route.request().method() === "POST") {
          posts++;
          const data = new URLSearchParams(route.request().postData());
          expect(data.get(taskName)).toBe(platform === "atcoder" ? pid : "A");
          expect(data.get(langName)).toBe("4006");
          storedSource = data.get(sourceName);
          return route.fulfill({ status: 303, headers: { location: origin + ownPath } });
        }
        return respond(
          `<form method="post"><select name="${taskName}"><option value="${platform === "atcoder" ? pid : "A"}">A</option></select><div style="display:none"><select name="${langName}"><option value="999">Inactive group</option></select></div><div><select name="${langName}" style="display:none"><option value="4003">C++ 17</option><option value="4006">Python 3 (original)</option></select></div><textarea name="${sourceName}"></textarea><button type="submit">Submit</button></form>`,
        );
      }
      if (u.pathname === "/login") return respond('<input type="password"><p>Sign in manually</p>');
      if (u.pathname === ownPath) {
        if (posts) polls++;
        return respond(
          `<table>${row(100, "WA")}${posts ? row(101, polls > 3 ? (platform === "atcoder" ? "AC" : "Accepted") : "Judging") + row(102, "WA") : ""}</table>`,
        );
      }
      if (u.pathname.startsWith(detailPath))
        return respond(
          `<pre id="${platform === "atcoder" ? "submission-code" : "program-source-text"}">${escape(u.pathname.endsWith("101") ? storedSource : "different submission")}</pre>`,
        );
      return route.abort();
    });
    await context.exposeBinding("cpGM", async ({ page: caller }, method, key, value) => {
      if (method === "get") return storage.get(key);
      if (method === "delete") {
        storage.delete(key);
        return;
      }
      if (method === "set") {
        const old = storage.get(key);
        storage.set(key, value);
        await Promise.all(
          context.pages().map((p) =>
            p
              .evaluate(
                ({ key, old, value }) => {
                  for (const callback of window.cpListeners?.[key] || [])
                    callback(key, old, value, true);
                },
                { key, old, value },
              )
              .catch(() => {}),
          ),
        );
      }
      if (method === "open") {
        const tab = await context.newPage();
        tab.on("pageerror", (e) => errors.push(e.message));
        await tab.goto(key);
      }
      if (method === "xhr") {
        expect(new URL(key.url).origin).toBe(url);
        const response = await context.request.fetch(key.url, {
          method: key.method,
          headers: { ...key.headers, Origin: new URL(caller.url()).origin },
          data: key.data,
        });
        return { status: response.status(), responseText: await response.text() };
      }
    });
    const helper = await readFile(
      resolve(import.meta.dirname, "../cptrainer/static/cp-trainer.user.js"),
      "utf8",
    );
    await context.addInitScript({
      content: `
    window.cpListeners = {};
    window.GM_getValue = (key) => window.cpGM('get', key);
    window.GM_setValue = (key, value) => window.cpGM('set', key, value);
    window.GM_deleteValue = (key) => window.cpGM('delete', key);
    window.GM_addValueChangeListener = (key, callback) => (window.cpListeners[key] ||= []).push(callback);
    window.GM_openInTab = (url) => window.cpGM('open', url);
    window.GM_xmlhttpRequest = (request) => window.cpGM('xhr', {method:request.method, url:request.url, headers:request.headers, data:request.data}).then(request.onload, request.onerror);
    window.unsafeWindow = window;
    document.addEventListener('DOMContentLoaded', () => { ${helper} });
  `,
    });
    page.on("pageerror", (e) => errors.push(e.message));
    await page.reload();
    await page.locator('[data-nav="bank"]').click();
    await page.locator('#search-form [name="q"]').fill(pid);
    await page.getByRole("button", { name: "筛选", exact: true }).click();
    await expect(page.locator("#bank-count")).toContainText("共 1 道");
    await page.locator("[data-select]").check();
    await page.locator("#manual-create").click();
    await page.locator('#create-form [type="submit"]').click();
    await page.getByRole("button", { name: "立即开始", exact: false }).click();
    await page.locator("#session-detail [data-problem]").click();
    await page.locator("#reader-remote").click();
    const opened = context.waitForEvent("page");
    await page.locator("#remote-connect").click();
    const remoteTab = await opened;
    await expect(remoteTab.locator("#cp-trainer-bridge")).toContainText("登录");
    expect(posts).toBe(0);
    signedIn = true;
    await remoteTab.locator("#cp-trainer-bridge a").click();
    await expect(page.locator("#remote-status")).toContainText("已连接");
    await page.locator('#judge-form [name="language"]').selectOption("4006");
    const source = "# 中文 <>&\nprint('a&b')\n";
    await page.locator("#judge-source").fill(source);
    await page.locator('#judge-form [type="submit"]').click();
    await expect(page.locator("#judge-result h3")).toHaveText("AC · 已通过", { timeout: 50000 });
    expect(posts).toBe(1);
    expect(storedSource.replaceAll("\r\n", "\n")).toBe(source);
    await expect(page.locator("#judge-result a")).toHaveAttribute(
      "href",
      origin + detailPath + "101",
    );
    await remoteTab.reload();
    await page.getByRole("button", { name: "关闭评测", exact: true }).click();
    await page.getByRole("button", { name: "关闭题面", exact: true }).click();
    await expect(page.locator(".task-meta")).toContainText("AC", { timeout: 10000 });
    await page.locator('[data-nav="judge"]').click();
    await expect(page.locator("#judge-history tbody tr")).toHaveCount(1);
    await page.locator("[data-judge-run]").click();
    await expect(page.locator("#judge-source")).toHaveValue(source);
    await page.setViewportSize({ width: 320, height: 760 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(
      true,
    );
    await page.screenshot({ path: testInfo.outputPath(`remote-${platform}.png`), fullPage: true });
    expect(posts).toBe(1);
    expect(errors).toEqual([]);
  });

test("offline statements, local math, filtering and manual contest", async ({ page }) => {
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.getByRole("link", { name: "本地题库", exact: true }).click();
  await page.locator('#search-form [name="platform"]').selectOption("atcoder");
  await page.locator('#search-form [name="ready"]').check();
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  await expect(page.locator("#bank-count")).toContainText("共 8 道");
  await page.locator("#problem-list [data-problem]").first().click();
  await expect(page.locator("#statement .katex")).toHaveCount(2);
  await expect(page.locator("#reader-status")).toHaveText("离线就绪");
  await expect(page.locator("#statement pre")).toHaveText("2\n4");
  await page.locator("#reader-remote").click();
  await expect(page.locator("#remote-status")).toContainText("未检测到助手");
  await expect(page.locator("#judge-form [type=submit]")).toBeDisabled();
  await page.getByRole("button", { name: "关闭评测", exact: true }).click();
  await page.getByRole("button", { name: "关闭题面", exact: true }).click();
  await page.locator("[data-select]").nth(0).check();
  await page.locator("[data-select]").nth(1).check();
  await page.locator("#manual-create").click();
  await page.locator('#create-form [type="submit"]').click();
  await expect(page.locator(".task-row")).toHaveCount(2);
  await expect(page.locator(".session-heading .chip")).toHaveText("AtCoder");
  expect(errors).toEqual([]);
});

test("theme persists and 320px layout remains usable", async ({ page }, testInfo) => {
  await page.getByRole("button", { name: "切换至暗色模式", exact: true }).click();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.screenshot({ path: testInfo.outputPath("dark-desktop.png"), fullPage: true });
  await page.setViewportSize({ width: 320, height: 760 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("link", { name: "本地题库", exact: true }).click();
  await expect(page.locator("#problem-list tr")).toHaveCount(16);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("dark-mobile.png"), fullPage: true });
  await page.locator("#theme").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("offline judge: Python and C++ AC, WA, CE, history and contest scoring", async ({
  page,
}, testInfo) => {
  test.skip(!process.env.CP_TRAINER_JUDGE_TEST_IMAGE, "Requires prepared Docker judge image");
  test.setTimeout(120000);
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.getByRole("link", { name: "本地题库", exact: true }).click();
  await page.locator('#search-form [name="q"]').fill("abc350_a");
  await page.locator('#search-form [name="judge"]').check();
  await page.getByRole("button", { name: "筛选", exact: true }).click();
  await expect(page.locator("#bank-count")).toContainText("共 1 道");
  await page.locator("[data-select]").check();
  await page.locator("#manual-create").click();
  await page.locator('#create-form [type="submit"]').click();
  await page.getByRole("button", { name: "立即开始", exact: false }).click();
  await page.locator("#session-detail [data-problem]").click();
  await page.locator("#reader-judge").click();
  await page.locator('#judge-form [name="language"]').selectOption("python3");
  await page.locator("#judge-source").fill("n=int(input())\nprint(n*n)\n");
  await page.getByRole("button", { name: "提交并评测", exact: true }).click();
  await expect(page.locator("#judge-result h3")).toHaveText("AC · 已通过", { timeout: 30000 });
  await expect(page.locator("#judge-result")).toContainText("已通过 2 个");
  await page.getByRole("button", { name: "关闭评测", exact: true }).click();
  await page.getByRole("button", { name: "关闭题面", exact: true }).click();
  await expect(page.locator(".task-meta")).toContainText("AC", { timeout: 10000 });
  await page.locator("#session-detail [data-problem]").click();
  await page.locator("#reader-judge").click();
  await page.locator('#judge-form [name="language"]').selectOption("cpp17");
  await page
    .locator("#judge-source")
    .fill('#include <iostream>\nint main(){long long n;std::cin>>n;std::cout<<n*n<<"\\n";}');
  await page.getByRole("button", { name: "提交并评测", exact: true }).click();
  await expect(page.locator("#judge-result h3")).toHaveText("AC · 已通过", { timeout: 30000 });
  await page.locator('#judge-form [name="language"]').selectOption("python3");
  await page.locator("#judge-source").fill("print(0)");
  await page.getByRole("button", { name: "提交并评测", exact: true }).click();
  await expect(page.locator("#judge-result h3")).toHaveText("WA · 答案不匹配", { timeout: 30000 });
  await page.locator('#judge-form [name="language"]').selectOption("cpp17");
  await page.locator("#judge-source").fill("int main(");
  await page.getByRole("button", { name: "提交并评测", exact: true }).click();
  await expect(page.locator("#judge-result h3")).toHaveText("CE · 编译错误", { timeout: 30000 });
  await page.reload();
  await page.getByRole("link", { name: "评测记录", exact: true }).click();
  await expect(page.locator("#judge-history tbody tr")).toHaveCount(4);
  await page.locator("[data-judge-run]").first().click();
  await expect(page.locator("#judge-source")).toHaveValue("int main(");
  await page.setViewportSize({ width: 320, height: 760 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("judge-mobile.png"), fullPage: true });
  await page.getByRole("button", { name: "关闭评测", exact: true }).click();
  await page.setViewportSize({ width: 1365, height: 900 });
  await page.screenshot({ path: testInfo.outputPath("judge-desktop.png"), fullPage: true });
  expect(errors).toEqual([]);
});
