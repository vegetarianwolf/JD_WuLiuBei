// Artifact-only capture of the single authorized report served on localhost.
// It uses a fresh headless context and never accesses the user's browser profile.
const { chromium } = require('/Users/vegetarianwolf/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const output = __dirname;
  const context = await browser.newContext({ viewport: { width: 1200, height: 900 } });
  const page = await context.newPage();
  for (const width of [1200, 360, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('http://127.0.0.1:8768/', { waitUntil: 'load' });
    await page.screenshot({ path: path.join(output, `report-${width}.png`), fullPage: false });
  }
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.locator('#stages-heading').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, 'report-three-trials.png'), fullPage: false });
  await browser.close();
  console.log(JSON.stringify({ saved: ['report-1200.png', 'report-360.png', 'report-320.png', 'report-three-trials.png'].map(f => path.join(output, f)) }));
})().catch(error => { console.error(error); process.exitCode = 1; });
