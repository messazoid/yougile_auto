#!/usr/bin/env node

const { chromium } = require('/opt/cisnet-playwright/node_modules/playwright');

const CDP_PORT = 9222;

async function main() {
  const browser = await chromium.launch({
    headless: false,
    args: [
      `--remote-debugging-address=127.0.0.1`,
      `--remote-debugging-port=${CDP_PORT}`,
    ],
  });
  const context = await browser.newContext({ viewport: null, locale: 'en-US' });
  const page = await context.newPage();
  await page.goto('https://cisnet.cisac.org/', { waitUntil: 'domcontentloaded' });

  console.log(`CIS-Net Chromium is ready; CDP is bound to 127.0.0.1:${CDP_PORT}`);
  const close = async () => {
    await browser.close();
    process.exit(0);
  };
  process.on('SIGINT', close);
  process.on('SIGTERM', close);
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
