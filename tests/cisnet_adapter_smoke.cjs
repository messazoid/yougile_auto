'use strict';

// Run in the browser image with Xvfb and a temporary profile. Every request is
// intercepted locally: this test never logs in to the real CIS-Net service.
const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { promisify } = require('node:util');
const test = require('node:test');
const driver = process.env.CISNET_PLAYWRIGHT_MODULE
  || '/opt/cisnet-playwright/node_modules/patchright';
const { chromium } = require(driver);
const exec = promisify(execFile);
const browserChannel = process.env.CISNET_TEST_BROWSER_CHANNEL || 'chromium';
if (!['chromium', 'chrome'].includes(browserChannel)) {
  throw new Error('CISNET_TEST_BROWSER_CHANNEL must be chromium or chrome');
}

const fixture = String.raw`<!doctype html><html><body>
<main id="app"></main>
<script>
const app = document.getElementById('app');
const noResults = 'No results were found for this request.';
document.addEventListener('keydown', () => {
  document.body.dataset.keydowns = String(Number(document.body.dataset.keydowns || 0) + 1);
});
function login() {
  app.innerHTML = '<input type="text"><input type="password"><button id="login">Sign in</button>';
  document.getElementById('login').onclick = menu;
}
function menu() {
  app.innerHTML = '<span class="v-menubar-menuitem"><span class="v-menubar-menuitem-caption" id="mwi">MWI</span></span>'
    + '<span class="v-menubar-menuitem">Test (RAO)</span>'
    + '<span class="v-menubar-menuitem" id="logout">Logout</span><section id="search"></section>';
  document.getElementById('mwi').onclick = searchForm;
  document.getElementById('logout').onclick = () => {
    const dialog = document.createElement('div');
    dialog.className = 'v-window';
    dialog.setAttribute('role', 'dialog');
    dialog.innerHTML = 'Please confirm. Are you really sure?<button>Yes</button>';
    dialog.querySelector('button').onclick = login;
    app.append(dialog);
  };
}
function searchForm() {
  const choices = ['Title', 'ISWC', 'Contains', 'Performer', 'Choose', '10', '20'];
  const select = '<select>' + choices.map(x => '<option>' + x + '</option>').join('') + '</select>';
  document.getElementById('search').innerHTML = '<p>MWI &gt; Search works &gt; Search</p>'
    + select.repeat(6) + '<input type="text"><input type="text">'
    + '<button class="v-button-primary">Search</button><section id="results"></section>';
  document.querySelector('.v-button-primary').onclick = () => {
    // This executes in the site's main world, independently of evaluate().
    document.body.dataset.ownMarkers = JSON.stringify(
      Object.getOwnPropertyNames(window).filter(x => x.startsWith('__cisnet')));
    document.body.dataset.webdriver = String(navigator.webdriver);
    if (document.querySelector('input').value === 'EMPTY') {
      document.getElementById('results').innerHTML = '<p>' + noResults + '</p>';
    } else {
      renderPage(1);
    }
  };
}
function renderPage(number) {
  document.getElementById('results').innerHTML = '<div id="mwiResultLayoutPrint">'
    + '<div><div class="v-slot-workTitleLayout">' + number + ' Work ' + number + '</div></div>'
    + '<div><span class="v-label">Performer(s)</span><span class="v-label">Test Artist</span></div>'
    + '</div><div class="paging-layout"><span class="v-label">' + number + ' / 2</span>'
    + '<button class="v-button">\uF105</button></div>';
  document.querySelector('.paging-layout button').onclick = () => renderPage(2);
}
login();
</script></body></html>`;

test('Patchright CDP adapter: login, repeated search, pagination, empty results, logout',
  { timeout: 240000 }, async () => {
    const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'cisnet-adapter-'));
    let context;
    try {
      context = await chromium.launchPersistentContext(path.join(temporary, 'profile'), {
        ...(browserChannel === 'chrome' ? { channel: 'chrome' } : {}),
        headless: false,
        viewport: null,
        locale: 'en-US',
        args: ['--remote-debugging-address=127.0.0.1', '--remote-debugging-port=19223'],
      });
      await context.route('**/*', route => route.fulfill({ contentType: 'text/html', body: fixture }));
      const page = context.pages()[0];
      await page.goto('https://cisnet.cisac.org/');
      const adapter = path.resolve(__dirname, '../scripts/cisnet_search_works.js');
      const run = async (...args) => {
        console.log(`Fixture adapter: ${args[0]}`);
        try {
          const result = await exec(process.execPath, [adapter, ...args], {
            timeout: 45000,
            env: {
              ...process.env,
              CISNET_PLAYWRIGHT_MODULE: driver,
              CISNET_CDP_ENDPOINT: 'http://127.0.0.1:19223',
              CISNET_EMAIL: 'fixture@example.test',
              CISNET_PASSWORD: 'fixture-only',
              CISNET_KEY_DELAY_MS: '1',
              CISNET_ACTION_DELAY_MIN_MS: '0',
              CISNET_ACTION_DELAY_MAX_MS: '0',
            },
          });
          return result.stdout.trim() ? JSON.parse(result.stdout) : null;
        } catch (error) {
          throw new Error(`Adapter failed: ${error.stderr || error.message}`);
        }
      };
      const requestPath = path.join(temporary, 'request.json');
      await run('--start');
      await fs.writeFile(requestPath, JSON.stringify({ title: 'TEST', performer: 'Test Artist' }));
      for (let attempt = 0; attempt < 2; attempt++) {
        const result = await run('--request', requestPath);
        assert.deepEqual(result.works.map(work => work.title), ['Work 1', 'Work 2']);
        assert.deepEqual(result.works.map(work => work.position), [1, 2]);
      }
      await fs.writeFile(requestPath, JSON.stringify({ title: 'TEST', performer: 'Test Artist', iswc: 'T-123.456.789-0' }));
      assert.equal((await run('--request', requestPath)).works.length, 2);
      await fs.writeFile(requestPath, JSON.stringify({ title: 'EMPTY', performer: 'Test Artist' }));
      const empty = await run('--request', requestPath);
      assert.deepEqual(empty.works, []);
      assert.equal(empty.result_message, 'No results were found for this request.');
      assert.equal(await page.locator('body').getAttribute('data-webdriver'), 'false');
      assert.equal(await page.locator('body').getAttribute('data-own-markers'), '[]');
      assert.ok(Number(await page.locator('body').getAttribute('data-keydowns')) > 20);
      await run('--finish');
      assert.equal(await page.locator('input[type="password"]').count(), 1);
    } finally {
      if (context) await context.close();
      await fs.rm(temporary, { recursive: true, force: true });
    }
  });
