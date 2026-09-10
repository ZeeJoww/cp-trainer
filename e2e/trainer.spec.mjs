import { test, expect } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
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
  await page.getByRole("link", { name: "离线评测", exact: true }).click();
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
