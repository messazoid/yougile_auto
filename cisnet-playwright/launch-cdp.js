#!/usr/bin/env node

const { chromium } = require('patchright');
const { configureProfile } = require('./configure-profile');

const CDP_PORT = Number.parseInt(process.env.CISNET_CDP_PORT || '9223', 10);
const CDP_ADDRESS = process.env.CISNET_CDP_BIND_ADDRESS || '127.0.0.1';
const USER_DATA_DIR = '/var/lib/cisnet-playwright/cdp-profile';

if (!Number.isInteger(CDP_PORT) || CDP_PORT < 1 || CDP_PORT > 65535) {
  throw new Error('CISNET_CDP_PORT must be a valid TCP port');
}

async function main() {
  let closing = false;
  configureProfile(USER_DATA_DIR);
  const context = await chromium.launchPersistentContext(
    USER_DATA_DIR,
    {
    headless: false,
    viewport: null,
    locale: 'en-US',
    args: [
      `--remote-debugging-address=${CDP_ADDRESS}`,
      `--remote-debugging-port=${CDP_PORT}`,
      '--hide-crash-restore-bubble',
      '--disable-save-password-bubble',
      '--no-default-browser-check',
      '--no-first-run',
    ],
    },
  );
  const page = context.pages()[0] || await context.newPage();
  await page.goto('https://cisnet.cisac.org/', { waitUntil: 'domcontentloaded' });

  console.log(`CIS-Net Chromium is ready; CDP is bound to ${CDP_ADDRESS}:${CDP_PORT}`);
  context.on('close', () => {
    if (!closing) process.exit(1);
  });
  const close = async () => {
    closing = true;
    await context.close();
    await new Promise((resolve) => setTimeout(resolve, 1000));
    process.exit(0);
  };
  process.on('SIGINT', close);
  process.on('SIGTERM', close);
  setInterval(() => {}, 60_000);
}

main().catch((error) => {
  console.error(error.message);
  process.exit(1);
});
