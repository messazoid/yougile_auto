#!/usr/bin/env node
'use strict';

const { configureProfile } = require('./configure-profile');
const { readBrowserEnvironment } = require('./browser-environment');
const { execFile } = require('node:child_process');
const fs = require('node:fs/promises');
const http = require('node:http');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { promisify } = require('node:util');
const exec = promisify(execFile);

// Served as the page's own script. Using page.evaluate() to collect this data
// would instead inspect Patchright's isolated world, not the website's world.
async function collectInPage() {
  const output = document.getElementById('result');
  output.removeAttribute('data-ready');
  try {
    const canvas = document.createElement('canvas');
    canvas.width = 240;
    canvas.height = 60;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#f4f4f4';
    ctx.fillRect(0, 0, 240, 60);
    ctx.font = '18px sans-serif';
    ctx.fillStyle = '#246';
    ctx.fillText('CIS-Net diagnostic Aa 0123', 8, 32);
    const bytes = ctx.getImageData(0, 0, 240, 60).data;
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const canvasHash = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
    const gl = document.createElement('canvas').getContext('webgl');
    const debug = gl?.getExtension('WEBGL_debug_renderer_info');
    const webgl = gl ? {
      vendor: debug ? gl.getParameter(debug.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR),
      renderer: debug ? gl.getParameter(debug.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
      version: gl.getParameter(gl.VERSION),
    } : null;
    gl?.getExtension('WEBGL_lose_context')?.loseContext();
    const iframe = document.createElement('iframe');
    iframe.hidden = true;
    document.body.append(iframe);
    const frameNavigator = iframe.contentWindow.navigator;
    const frame = {
      webdriver: frameNavigator.webdriver,
      userAgent: frameNavigator.userAgent,
      platform: frameNavigator.platform,
      languages: Array.from(frameNavigator.languages),
    };
    iframe.remove();
    const video = document.createElement('video');
    const headers = await fetch('/headers', { cache: 'no-store' }).then(r => r.json());
    const descriptor = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
    const snapshot = {
      navigator: {
        webdriver: navigator.webdriver,
        webdriverGetter: descriptor?.get?.toString() ?? null,
        userAgent: navigator.userAgent,
        clientHints: navigator.userAgentData ? await navigator.userAgentData.getHighEntropyValues([
          'architecture', 'bitness', 'platformVersion', 'fullVersionList', 'wow64',
        ]) : null,
        platform: navigator.platform,
        languages: Array.from(navigator.languages),
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory ?? null,
        maxTouchPoints: navigator.maxTouchPoints,
        plugins: Array.from(navigator.plugins, p => p.name),
      },
      headers,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      screen: { width: screen.width, height: screen.height, colorDepth: screen.colorDepth },
      window: { innerWidth, innerHeight, outerWidth, outerHeight, devicePixelRatio },
      webgl,
      canvasHash,
      codecs: {
        h264: video.canPlayType('video/mp4; codecs="avc1.42E01E"'),
        vp9: video.canPlayType('video/webm; codecs="vp9"'),
        aac: video.canPlayType('audio/mp4; codecs="mp4a.40.2"'),
      },
      frame,
      markers: Object.getOwnPropertyNames(window).filter(name =>
        /^__cisnet|playwright|puppeteer|webdriver|^cdc_/i.test(name)).sort(),
    };
    output.textContent = JSON.stringify(snapshot);
    output.setAttribute('data-ready', 'true');
  } catch (error) {
    output.textContent = JSON.stringify({ error: String(error) });
    output.setAttribute('data-ready', 'true');
  }
}

const pageHtml = '<!doctype html><title>Local browser diagnostics</title>'
  + '<button id="collect">Collect</button><pre id="result"></pre>'
  + '<script>const collect = ' + collectInPage.toString() + ';'
  + 'document.getElementById("collect").onclick = collect; collect();</script>';

function differences(left, right, prefix = '') {
  if (JSON.stringify(left) === JSON.stringify(right)) return [];
  if (left && right && typeof left === 'object' && typeof right === 'object'
    && !Array.isArray(left) && !Array.isArray(right)) {
    return [...new Set([...Object.keys(left), ...Object.keys(right)])].sort().flatMap(key =>
      differences(left[key], right[key], prefix ? `${prefix}.${key}` : key));
  }
  return [{ field: prefix, left: left ?? null, right: right ?? null }];
}

async function readSnapshot(page) {
  const result = page.locator('#result[data-ready="true"]');
  await result.waitFor({ state: 'attached', timeout: 30000 });
  const snapshot = JSON.parse(await result.textContent());
  if (snapshot.error) throw new Error(snapshot.error);
  return snapshot;
}

async function freshSnapshot(page) {
  await page.locator('#collect').click();
  return readSnapshot(page);
}

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return server.address().port;
}

function createProbeServer() {
  return http.createServer((request, response) => {
    response.setHeader('Cache-Control', 'no-store');
    if (request.url === '/headers') {
      const allowed = ['user-agent', 'accept-language', 'sec-ch-ua', 'sec-ch-ua-mobile', 'sec-ch-ua-platform'];
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify(Object.fromEntries(allowed.map(key => [key, request.headers[key] ?? null]))));
    } else {
      response.setHeader('Content-Type', 'text/html; charset=utf-8');
      response.end(pageHtml);
    }
  });
}

async function closeProbeServer(server) {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}

async function freePort() {
  const server = net.createServer();
  const port = await listen(server);
  await new Promise(resolve => server.close(resolve));
  return port;
}

async function collectWithRunner(endpoint, url) {
  const { chromium } = require('patchright');
  // A separate process models the production CDP runner and disconnects by
  // exiting, without closing the browser owned by the launcher.
  const browser = await chromium.connectOverCDP(endpoint);
  const page = browser.contexts().flatMap(c => c.pages()).find(p => p.url() === url);
  if (!page) throw new Error('Local diagnostic page missing');
  const snapshot = await freshSnapshot(page);
  await new Promise((resolve, reject) => process.stdout.write(JSON.stringify(snapshot), error => error ? reject(error) : resolve()));
  process.exit(0);
}

async function compare() {
  const { chromium } = require('patchright');
  const environment = readBrowserEnvironment();
  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'cisnet-diagnostics-'));
  const server = createProbeServer();
  const report = {
    schema: 'cisnet-browser-diagnostics/v1',
    capturedAt: new Date().toISOString(),
    driver: { name: 'patchright', version: require('patchright/package.json').version },
    conditions: { headless: false, viewport: null, ...environment, profiles: 'fresh and separate' },
    limitations: [
      'Both browsers are automated; there is no manual-browser control group.',
      'Local JavaScript and selected HTTP headers only; no TLS/IP reputation or CIS-Net detection test.',
      'Different browser versions can explain differences; this report does not assign a stealth score.',
    ],
    browsers: {},
  };
  try {
    const url = `http://127.0.0.1:${await listen(server)}/`;
    for (const kind of ['chromium', 'chrome']) {
      const port = await freePort();
      const profile = path.join(temporary, kind);
      configureProfile(profile);
      const context = await chromium.launchPersistentContext(profile, {
        ...(kind === 'chrome' ? { channel: 'chrome' } : {}),
        headless: false,
        viewport: null,
        locale: environment.locale,
        timezoneId: environment.timezoneId,
        args: [
          '--remote-debugging-address=127.0.0.1', `--remote-debugging-port=${port}`,
          '--hide-crash-restore-bubble', '--disable-save-password-bubble',
          '--no-default-browser-check', '--no-first-run',
        ],
      });
      try {
        const page = context.pages()[0];
        await page.goto(url, { waitUntil: 'load' });
        const first = await readSnapshot(page);
        const repeated = await freshSnapshot(page);
        const { stdout } = await exec(process.execPath, [__filename, '--runner', `http://127.0.0.1:${port}`, url], {
          timeout: 60000,
        });
        const afterRunner = JSON.parse(stdout);
        report.browsers[kind] = {
          version: context.browser().version(),
          beforeRunner: first,
          afterRunner,
          repeatDifferences: differences(first, repeated),
          runnerDifferences: differences(repeated, afterRunner),
        };
      } finally {
        await context.close();
      }
    }
    report.browserDifferences = differences(report.browsers.chromium.afterRunner, report.browsers.chrome.afterRunner);
    process.stdout.write(JSON.stringify(report, null, 2) + '\n');
  } finally {
    await closeProbeServer(server);
    await fs.rm(temporary, { recursive: true, force: true });
  }
}

module.exports = { createProbeServer, closeProbeServer, listen, readSnapshot, differences };

if (require.main === module) {
  (process.argv[2] === '--runner'
    ? collectWithRunner(process.argv[3], process.argv[4]) : compare()
  ).catch(error => {
    console.error(error);
    if (process.argv[2] === '--runner') process.exit(1);
    process.exitCode = 1;
  });
}
