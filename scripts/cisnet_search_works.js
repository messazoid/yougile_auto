#!/usr/bin/env node

/*
 * Browser-only CIS-Net adapter.  It attaches to the visible browser started
 * separately by cisnet-cdp-browser.service and emits one JSON object to stdout.
 */

const fs = require('fs');
const browserAutomationModule = process.env.CISNET_PLAYWRIGHT_MODULE
  || '/opt/cisnet-playwright/node_modules/patchright';
const { chromium } = require(browserAutomationModule);

const CDP_ENDPOINT = process.env.CISNET_CDP_ENDPOINT || 'http://127.0.0.1:9223';
const NO_RESULTS = 'No results were found for this request.';
const LOGIN_TIMEOUT_MS = 60000;

class AdapterError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

let currentStep = 'adapter.init';

function trace(step, event, code) {
  const record = { step, event };
  if (code) record.code = code;
  fs.writeSync(2, `[CISNET-PLAYWRIGHT] ${JSON.stringify(record)}\n`);
}

function begin(step) {
  currentStep = step;
  trace(step, 'begin');
}

function done() {
  trace(currentStep, 'ok');
}

function failureCode(error) {
  if (error instanceof AdapterError) return error.code;
  if (error?.name === 'TimeoutError') return 'TIMEOUT';
  if (currentStep === 'browser.attach') return 'CDP_ATTACH_FAILED';
  if (error instanceof SyntaxError) return 'INVALID_REQUEST_JSON';
  return 'UNEXPECTED';
}

function argument(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 ? process.argv[index + 1] : null;
}

function clean(value) {
  return (value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
}

async function single(locator, description) {
  const count = await locator.count();
  if (count !== 1) throw new AdapterError('ELEMENT_COUNT', `${description}: expected one element, found ${count}`);
  return locator;
}

async function waitForResults(watcher) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    // Keep evaluation in the handle's execution context. Patchright may use
    // different isolated contexts for evaluate() and waitForFunction().
    if (await watcher.evaluate(state => state.ready())) return;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  const error = new Error('CIS-Net results did not settle within 30000 ms');
  error.name = 'TimeoutError';
  throw error;
}

async function pageInSharedBrowser() {
  const browser = await chromium.connectOverCDP(CDP_ENDPOINT);
  const page = browser.contexts().flatMap((context) => context.pages())
    .find((item) => item.url().startsWith('https://cisnet.cisac.org/'));
  if (!page) throw new AdapterError('TAB_MISSING', 'CIS-Net tab is not open in the shared browser');
  return page;
}

async function signedIn(page) {
  return (await page.locator('span.v-menubar-menuitem-caption:visible').filter({ hasText: /^MWI$/ }).count()) === 1;
}

async function login(page) {
  begin('login.credentials');
  const email = process.env.CISNET_EMAIL;
  const password = process.env.CISNET_PASSWORD;
  if (!email || !password) throw new AdapterError('CREDENTIALS_MISSING', 'CIS-Net credentials are not configured');
  done();
  begin('login.form');
  const emailInput = page.locator('input[type="text"]:visible').first();
  const passwordInput = page.locator('input[type="password"]:visible').first();
  if (!(await passwordInput.isVisible())) throw new AdapterError('LOGIN_FORM_MISSING', 'CIS-Net login page is not open');
  done();
  begin('login.fill');
  await emailInput.fill(email);
  await passwordInput.fill(password);
  done();
  begin('login.submit');
  await single(page.getByRole('button', { name: /Sign in/i }), 'Sign in button').then((button) => button.click());
  done();
  begin('login.wait');
  const deadline = Date.now() + LOGIN_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const busy = page.locator('.v-window:visible').filter({ hasText: /another active session/i });
    if (await busy.count()) {
      begin('login.busy');
      const no = await single(busy.getByRole('button', { name: 'No', exact: true }), 'busy-session No button');
      await no.click();
      throw new AdapterError('BUSY_SESSION', 'CIS-Net session is already in use; selected No');
    }
    if (await signedIn(page)) {
      done();
      return;
    }
    await page.waitForTimeout(250);
  }
  throw new AdapterError('LOGIN_TIMEOUT', 'CIS-Net login did not reach the main menu');
}

async function logout(page) {
  if (!(await signedIn(page))) return;
  const confirmationLocator = page.locator('.v-window[role="dialog"]:visible')
    .filter({ hasText: /Please confirm/ })
    .filter({ hasText: /Are you really sure\s*\?/ });
  if (!(await confirmationLocator.count())) {
    const account = await single(page.locator('span.v-menubar-menuitem:visible').filter({ hasText: /\(RAO\)/ }), 'account menu');
    await account.hover();
    const logoutItem = await single(page.locator('span.v-menubar-menuitem:visible').filter({ hasText: /Logout\s*$/ }), 'Logout menu item');
    await logoutItem.click();
  }
  await confirmationLocator.waitFor({ state: 'visible', timeout: 10000 });
  const confirmation = await single(confirmationLocator, 'Logout confirmation');
  await (await single(confirmation.getByRole('button', { name: 'Yes', exact: true }), 'Logout Yes button')).click();
  await page.locator('input[type="password"]:visible').first().waitFor({ state: 'visible', timeout: LOGIN_TIMEOUT_MS });
}

async function startSession(page) {
  begin('session.inspect');
  if (await signedIn(page)) throw new AdapterError('ALREADY_SIGNED_IN', 'CIS-Net is already signed in; manual session check is required');
  done();
  let loginAttempted = false;
  try {
    loginAttempted = true;
    await login(page);
    begin('mwi.open');
    const mwiCaption = await single(page.locator('span.v-menubar-menuitem-caption:visible').filter({ hasText: /^MWI$/ }), 'MWI menu');
    const mwi = mwiCaption.locator('..');
    await mwi.click();
    await page.getByText('MWI > Search works > Search', { exact: true })
      .waitFor({ state: 'visible', timeout: LOGIN_TIMEOUT_MS });
    done();
  } catch (error) {
    if (loginAttempted && await signedIn(page)) {
      try { await logout(page); } catch (logoutError) {
        throw new Error(`${error.message}; Logout failed: ${logoutError.message}`);
      }
    }
    throw error;
  }
}

async function selectByText(select, text) {
  const choice = await select.locator('option').evaluateAll((options, expected) => {
    const option = options.find((item) => item.textContent.trim() === expected);
    return option ? { value: option.value, selected: option.selected } : null;
  }, text);
  if (choice === null) throw new Error(`CIS-Net select option is unavailable: ${text}`);
  if (!choice.selected) await select.selectOption(choice.value);
}

async function capture(page) {
  return page.evaluate((noResults) => {
    const cleanText = (value) => (value || '').replace(/\u00a0/g, ' ').replace(/\s+/g, ' ').trim();
    const tableData = (table) => {
      const rows = [...table.querySelectorAll('tr')];
      const columns = rows.length
        ? [...rows[0].querySelectorAll('th,td')].map((cell) => cleanText(cell.innerText))
        : [];
      return rows.slice(1).map((row) => {
        const cells = [...row.querySelectorAll('td')].map((cell) => cleanText(cell.innerText));
        return Object.fromEntries(cells.map((cell, index) => [columns[index] || `column_${index}`, cell]).filter(([, cell]) => cell));
      }).filter((row) => Object.keys(row).length);
    };
    const allDb = [...document.querySelectorAll('*')].find((element) =>
      element.children.length === 0 && /^All DB\s*:\s*\d+$/.test(cleanText(element.textContent)));
    const message = [...document.querySelectorAll('*')].find((element) =>
      element.children.length === 0 && cleanText(element.textContent) === noResults);
    const base = {
      schema_version: 'cisnet-search-result/v1',
      captured_at_utc: new Date().toISOString(),
    };
    if (allDb) base.all_db_summary = cleanText(allDb.textContent);
    if (message) return { ...base, result_message: noResults, works: [] };
    const root = document.querySelector('#mwiResultLayoutPrint');
    if (!root) throw new Error('CIS-Net results were not rendered');
    const slots = [...root.children];
    const starts = slots.map((slot, index) => slot.querySelector('.v-slot-workTitleLayout') ? index : -1).filter((index) => index >= 0);
    const works = starts.map((start, index) => {
      const end = index + 1 < starts.length ? starts[index + 1] : slots.length;
      const group = slots.slice(start, end);
      const text = cleanText(group.map((slot) => slot.innerText).join('\n'));
      const titleText = cleanText(group[0].querySelector('.v-slot-workTitleLayout')?.innerText)
        .replace(/^\d+\s+/, '').replace(/[\uF000-\uF8FF]/g, '').trim();
      const labels = [...group.flatMap((slot) => [...slot.querySelectorAll('.v-label')])]
        .map((element) => cleanText(element.innerText)).filter((value, position, all) => value && all.indexOf(value) === position);
      const performerStart = labels.indexOf('Performer(s)');
      const performerNames = [];
      if (performerStart >= 0) {
        for (const label of labels.slice(performerStart + 1)) {
          if (label.startsWith('Domestic View:') || label.startsWith('Territory:')) break;
          performerNames.push(label);
        }
      }
      const tables = group.flatMap((slot) => [...slot.querySelectorAll('table.resultSimpleTable')]);
      const iswc = (text.match(/ISWC:\s*([A-Z]-\d{3}\.\d{3}\.\d{3}-\d)/) || [])[1] || null;
      const workCode = (text.match(/Work Code:\s*(\d+)/) || [])[1] || null;
      const lastUpdate = (text.match(/Last Update:\s*(\d{4}\/\d{2}\/\d{2})/) || [])[1] || null;
      const domestic = labels.find((label) => label.startsWith('Domestic View:'));
      const work = { position: index + 1, title: titleText, performers: performerNames };
      if (iswc) work.iswc = iswc;
      if (workCode) work.work_code = workCode;
      if (lastUpdate) work.last_update = lastUpdate;
      if (domestic) work.domestic_view = domestic.replace('Domestic View:', '').trim();
      if (tables.length) work.interested_parties = tableData(tables[0]);
      return work;
    });
    return { ...base, works };
  }, NO_RESULTS);
}

async function pagerState(page) {
  return page.evaluate(() => {
    const labels = [...document.querySelectorAll('.paging-layout .v-label')]
      .map((element) => element.innerText.trim());
    const match = labels.map((label) => label.match(/^(\d+)\s*\/\s*(\d+)$/)).find(Boolean);
    return match ? { current: Number(match[1]), total: Number(match[2]) } : null;
  });
}

async function captureAllPages(page) {
  const first = await capture(page);
  if (first.result_message) return first;
  const paging = await pagerState(page);
  if (!paging) return first;
  if (paging.current !== 1 || paging.total < 1) throw new Error('CIS-Net results did not start on page 1');
  for (let expectedPage = 2; expectedPage <= paging.total; expectedPage += 1) {
    const previousResults = await page.locator('#mwiResultLayoutPrint').innerText();
    const next = await single(
      page.locator('.paging-layout .v-button:visible').filter({ hasText: /^\s*\uF105\s*$/ }),
      'next-page button',
    );
    await next.click();
    await page.waitForFunction(({ expected, previous, noResults }) => {
      if (document.body.innerText.includes(noResults)) return true;
      const labels = [...document.querySelectorAll('.paging-layout .v-label')]
        .map((element) => element.innerText.trim());
      const results = document.querySelector('#mwiResultLayoutPrint');
      return labels.some((label) => new RegExp(`^${expected}\\s*\\/\\s*\\d+$`).test(label))
        && Boolean(results) && results.innerText !== previous;
    }, { expected: expectedPage, previous: previousResults, noResults: NO_RESULTS }, { timeout: 30000 });
    if (await page.getByText(NO_RESULTS, { exact: true }).count()) {
      return capture(page);
    }
    await page.waitForTimeout(150);
    const pageResult = await capture(page);
    if (pageResult.result_message || !pageResult.works.length) {
      throw new Error(`CIS-Net page ${expectedPage} has no rendered works`);
    }
    for (const work of pageResult.works) {
      first.works.push({ ...work, position: first.works.length + 1 });
    }
  }
  return first;
}

async function main() {
  const mode = process.argv.includes('--start') ? 'start'
    : process.argv.includes('--finish') ? 'finish' : 'search';
  const requestPath = argument('--request');
  if (mode === 'search' && !requestPath) throw new Error('missing --request');
  begin('browser.attach');
  const page = await pageInSharedBrowser();
  done();
  if (mode === 'start') return startSession(page);
  if (mode === 'finish') {
    begin('session.finish');
    await logout(page);
    done();
    return;
  }
  begin('search.request');
  const request = JSON.parse(fs.readFileSync(requestPath, 'utf8'));
  if (!request.title || !request.performer) throw new Error('request must contain title and performer');
  if (!(await signedIn(page))) throw new Error('CIS-Net is not signed in');
  if (await page.locator('.v-window:visible').count()) throw new Error('CIS-Net has a visible modal window; employee action is required');
  done();
  begin('search.fields');
  const selects = page.locator('select');
  const inputs = page.locator('input[type="text"]');
  if (await selects.count() < 6 || await inputs.count() < 2) throw new Error('CIS-Net MWI Standard Search is not open');
  const pageSizes = await selects.nth(5).locator('option').evaluateAll((options) =>
    options.map((option) => option.textContent.trim()).filter((value) => /^\d+$/.test(value)).map(Number));
  if (!pageSizes.length) throw new Error('CIS-Net Works/page selector is unavailable');
  await selectByText(selects.nth(5), String(Math.max(...pageSizes)));
  if (request.iswc) {
    if (!/^T-\d{3}\.\d{3}\.\d{3}-\d$/.test(request.iswc)) throw new Error('request has an invalid ISWC');
    await selectByText(selects.nth(0), 'ISWC');
    if (await selects.nth(3).isEnabled()) await selectByText(selects.nth(3), 'Choose');
    await page.waitForTimeout(300);
    await page.waitForLoadState('networkidle', { timeout: 15000 });
    await inputs.nth(0).fill(request.iswc);
    if (await inputs.nth(1).isEnabled()) await inputs.nth(1).fill('');
    await inputs.nth(0).press('Tab');
  } else {
    await selectByText(selects.nth(0), 'Title');
    await selectByText(selects.nth(1), 'Contains');
    await selectByText(selects.nth(3), 'Performer');
    await selectByText(selects.nth(4), 'Contains');
    await page.waitForTimeout(300);
    await page.waitForLoadState('networkidle', { timeout: 15000 });
    await inputs.nth(0).fill(request.title);
    await inputs.nth(1).fill(request.performer);
    await inputs.nth(1).press('Tab');
  }
  await page.waitForTimeout(300);
  await page.waitForLoadState('networkidle', { timeout: 15000 });
  const entered = await page.evaluate(() => [...document.querySelectorAll('input[type="text"]')]
    .slice(0, 2).map((input) => input.value.trim().toUpperCase()));
  if (entered[0] !== (request.iswc || request.title).trim().toUpperCase()
    || (!request.iswc && entered[1] !== request.performer.trim().toUpperCase())) {
    throw new Error('CIS-Net search fields changed before submission');
  }
  done();
  begin('search.submit');
  const search = page.locator('.v-button-primary:visible').filter({ hasText: /^Search$/ });
  if (await search.count() !== 1) throw new Error('CIS-Net Search button is ambiguous or unavailable');
  const resultsWatcher = await page.evaluateHandle((message) => {
    let mutationAt = 0;
    const observer = new MutationObserver((mutations) => {
      const touchesResults = (node) => {
        if (node.nodeType !== Node.ELEMENT_NODE) return false;
        return node.id === 'mwiResultLayoutPrint' || node.matches?.('#mwiResultLayoutPrint *')
          || Boolean(node.querySelector?.('#mwiResultLayoutPrint'))
          || node.textContent?.includes(message);
      };
      if (mutations.some((mutation) => [mutation.target, ...mutation.addedNodes, ...mutation.removedNodes].some(touchesResults))) {
        mutationAt = Date.now();
      }
    });
    observer.observe(document.body, { childList: true, characterData: true, subtree: true });
    return {
      ready: () => mutationAt > 0 && Date.now() - mutationAt >= 1000
        && Boolean(document.body.innerText.includes(message) || document.querySelector('#mwiResultLayoutPrint')),
      disconnect: () => observer.disconnect(),
    };
  }, NO_RESULTS);
  try {
    await search.click();
    done();
    begin('search.wait_results');
    await waitForResults(resultsWatcher);
    await page.waitForLoadState('networkidle', { timeout: 15000 });
    done();
    begin('search.capture');
    let result = await captureAllPages(page);
    if (await page.getByText(NO_RESULTS, { exact: true }).count()) {
      result = await capture(page);
    }
    done();
    fs.writeSync(1, `${JSON.stringify(result)}\n`);
  } finally {
    await resultsWatcher.evaluate(state => state.disconnect()).catch(() => {});
    await resultsWatcher.dispose().catch(() => {});
  }
}

main().then(() => process.exit(0)).catch((error) => {
  trace(currentStep, 'failed', failureCode(error));
  process.exit(2);
});
