import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  timeout: 45000,
  use: { viewport: { width: 1365, height: 900 }, trace: "retain-on-failure" },
});
