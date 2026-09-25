'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { readTiming, createTiming } = require('../scripts/cisnet_timing');

test('timing defaults and explicit zero delays', async () => {
  assert.deepEqual(readTiming({}), {
    keyDelayMs: 40, actionDelayMinMs: 120, actionDelayMaxMs: 280,
  });
  const timing = createTiming(readTiming({
    CISNET_KEY_DELAY_MS: '0',
    CISNET_ACTION_DELAY_MIN_MS: '0',
    CISNET_ACTION_DELAY_MAX_MS: '0',
  }));
  const calls = [];
  await timing.enterText({
    fill: async (value) => calls.push(['fill', value]),
    pressSequentially: async (value, options) => calls.push(['type', value, options.delay]),
  }, 'Title');
  assert.deepEqual(calls, [['fill', ''], ['type', 'Title', 0]]);
  assert.equal(await timing.act(() => 'done'), 'done');
});

test('invalid timing values fail before browser interaction', () => {
  for (const env of [
    { CISNET_KEY_DELAY_MS: '-1' },
    { CISNET_KEY_DELAY_MS: '1.5' },
    { CISNET_KEY_DELAY_MS: '251' },
    { CISNET_ACTION_DELAY_MIN_MS: '2001' },
    { CISNET_ACTION_DELAY_MAX_MS: 'abc' },
    { CISNET_ACTION_DELAY_MIN_MS: '300', CISNET_ACTION_DELAY_MAX_MS: '299' },
  ]) assert.throws(() => readTiming(env));
});
