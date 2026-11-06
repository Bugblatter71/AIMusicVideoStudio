// @ts-check
const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  testDir: './tests',
  testMatch: /.*\.spec\.js/,
  timeout: 45000,
  expect: {
    timeout: 10000,
  },
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: 'http://localhost:8765',
    trace: 'off',
    video: 'off',
    channel: 'chrome',
    headless: true,
  },
  webServer: {
    command: 'python work/ui/server.py --port 8765',
    port: 8765,
    timeout: 15000,
    reuseExistingServer: true,
  },
});
