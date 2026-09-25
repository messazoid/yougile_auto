'use strict';

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const test = require('node:test');
const { readBrowserEnvironment } = require('../browser-environment');
const screenScript = path.join(__dirname, '../screen-size.sh');

function shellGeometry(env) {
  return execFileSync('bash', ['-c', 'source "$1"; cisnet_screen_geometry', '_', screenScript], {
    env: { ...process.env, ...env },
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
}

test('defaults and custom locale, timezone, screen size agree across Node and Xvfb', () => {
  const defaults = readBrowserEnvironment({});
  assert.deepEqual(defaults, {
    locale: 'en-US', timezoneId: 'UTC', screenWidth: 1300, screenHeight: 1080,
  });
  assert.equal(shellGeometry({ CISNET_SCREEN_WIDTH: '1300', CISNET_SCREEN_HEIGHT: '1080' }), '1300x1080x24');
  const custom = {
    CISNET_LOCALE: 'ru-RU', CISNET_TIMEZONE: 'Europe/Moscow',
    CISNET_SCREEN_WIDTH: '1600', CISNET_SCREEN_HEIGHT: '900',
  };
  assert.deepEqual(readBrowserEnvironment(custom), {
    locale: 'ru-RU', timezoneId: 'Europe/Moscow', screenWidth: 1600, screenHeight: 900,
  });
  assert.equal(shellGeometry(custom), '1600x900x24');
});

test('bad locale, timezone, and screen dimensions are rejected before browser launch', () => {
  for (const env of [
    { CISNET_LOCALE: 'not_a_locale' },
    { CISNET_TIMEZONE: 'Mars/Olympus' },
    { CISNET_SCREEN_WIDTH: '0' },
    { CISNET_SCREEN_WIDTH: '9999' },
    { CISNET_SCREEN_HEIGHT: '500' },
    { CISNET_SCREEN_HEIGHT: '1080x24' },
  ]) {
    assert.throws(() => readBrowserEnvironment(env), /invalid/);
    if ('CISNET_SCREEN_WIDTH' in env || 'CISNET_SCREEN_HEIGHT' in env) {
      assert.throws(() => shellGeometry(env));
    }
  }
});
