#!/usr/bin/env node
'use strict';

const { readBrowserEnvironment } = require('./browser-environment');
const { createProbeServer, closeProbeServer, listen, readSnapshot } = require('./diagnose-browsers');

function consistencyChecks(snapshot, environment) {
  return {
    locale: snapshot.navigator.languages[0] === environment.locale,
    acceptLanguage: snapshot.headers['accept-language']?.startsWith(environment.locale) === true,
    timezone: snapshot.timezone === environment.timezoneId,
    screen: snapshot.screen.width === environment.screenWidth
      && snapshot.screen.height === environment.screenHeight,
    userAgentHeader: snapshot.headers['user-agent'] === snapshot.navigator.userAgent,
    iframeNavigator: snapshot.frame.webdriver === snapshot.navigator.webdriver
      && snapshot.frame.userAgent === snapshot.navigator.userAgent
      && snapshot.frame.platform === snapshot.navigator.platform
      && JSON.stringify(snapshot.frame.languages) === JSON.stringify(snapshot.navigator.languages),
  };
}

async function diagnose() {
  const { chromium } = require('patchright');
  const environment = readBrowserEnvironment();
  const server = createProbeServer();
  let page;
  try {
    const port = await listen(server);
    const browser = await chromium.connectOverCDP(process.env.CISNET_CDP_ENDPOINT || 'http://127.0.0.1:9223');
    const context = browser.contexts()[0];
    if (!context) throw new Error('The shared browser has no default context');
    page = await context.newPage();
    await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'load' });
    const snapshot = await readSnapshot(page);
    const report = {
      schema: 'cisnet-browser-live-diagnostics/v1',
      capturedAt: new Date().toISOString(),
      browserVersion: browser.version(),
      driver: { name: 'patchright', version: require('patchright/package.json').version },
      expectedEnvironment: environment,
      checks: consistencyChecks(snapshot, environment),
      snapshot,
      limitations: [
        'Collected from a temporary loopback page in the running browser, not from the CIS-Net page.',
        'Local JavaScript and selected HTTP headers only; no TLS/IP reputation or site detection test.',
        'These checks verify configuration consistency and do not assign a stealth score.',
      ],
    };
    await new Promise((resolve, reject) => process.stdout.write(
      `${JSON.stringify(report, null, 2)}\n`, error => error ? reject(error) : resolve(),
    ));
  } finally {
    if (page) await page.close().catch(() => {});
    await closeProbeServer(server);
  }
}

module.exports = { consistencyChecks, diagnose };

if (require.main === module) diagnose().then(() => {
  // The CDP client keeps a socket open; exit without closing the shared browser.
  process.exit(0);
}).catch(error => {
  console.error(error);
  process.exit(1);
});
