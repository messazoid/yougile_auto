'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { consistencyChecks } = require('../diagnose-live');

test('live report flags inconsistent browser and request values', () => {
  const environment = { locale: 'en-US', timezoneId: 'UTC', screenWidth: 1300, screenHeight: 1080 };
  const snapshot = {
    navigator: { webdriver: false, userAgent: 'Browser/1', platform: 'Linux', languages: ['en-US'] },
    headers: { 'accept-language': 'en-US,en;q=0.9', 'user-agent': 'Browser/1' },
    timezone: 'UTC',
    screen: { width: 1300, height: 1080 },
    frame: { webdriver: false, userAgent: 'Browser/1', platform: 'Linux', languages: ['en-US'] },
  };
  assert.ok(Object.values(consistencyChecks(snapshot, environment)).every(Boolean));
  snapshot.headers['user-agent'] = 'Browser/2';
  snapshot.frame.languages = ['fr-FR'];
  assert.deepEqual(Object.entries(consistencyChecks(snapshot, environment))
    .filter(([, matched]) => !matched).map(([name]) => name), ['userAgentHeader', 'iframeNavigator']);
});
