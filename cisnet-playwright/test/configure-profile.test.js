'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { configureProfile } = require('../configure-profile');

function temporaryProfile(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'cisnet-profile-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  return { root, file: path.join(root, 'Default', 'Preferences') };
}

test('new profile blocks browser prompts without changing website content settings', t => {
  const { root, file } = temporaryProfile(t);
  assert.equal(configureProfile(root), true);
  const prefs = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.equal(prefs.credentials_enable_service, false);
  assert.equal(prefs.credentials_enable_autosignin, false);
  assert.equal(prefs.profile.password_manager_enabled, false);
  assert.equal(prefs.autofill.profile_enabled, false);
  assert.equal(prefs.autofill.credit_card_enabled, false);
  assert.equal(prefs.translate.enabled, false);
  for (const name of ['notifications', 'geolocation', 'media_stream_camera', 'media_stream_mic']) {
    assert.equal(prefs.profile.default_content_setting_values[name], 2);
  }
  assert.equal(prefs.profile.default_content_setting_values.javascript, undefined);
  assert.equal(configureProfile(root), false);
});

test('existing profile is updated while unrelated preferences are retained', t => {
  const { root, file } = temporaryProfile(t);
  fs.mkdirSync(path.dirname(file));
  const existing = {
    credentials_enable_service: true,
    autofill: { profile_enabled: true, unrelated: 'keep' },
    profile: { default_content_setting_values: { javascript: 1 } },
    browser: { check_default_browser: false },
  };
  fs.writeFileSync(file, JSON.stringify(existing), { mode: 0o600 });
  assert.equal(configureProfile(root), true);
  const prefs = JSON.parse(fs.readFileSync(file, 'utf8'));
  assert.equal(prefs.credentials_enable_service, false);
  assert.equal(prefs.autofill.unrelated, 'keep');
  assert.equal(prefs.profile.default_content_setting_values.javascript, 1);
  assert.deepEqual(prefs.browser, existing.browser);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
});

test('invalid JSON is rejected without overwriting the profile or revealing its text', t => {
  const { root, file } = temporaryProfile(t);
  fs.mkdirSync(path.dirname(file));
  fs.writeFileSync(file, '{"secret":"do-not-print"');
  assert.throws(() => configureProfile(root), /^Error: Chromium Preferences is invalid JSON$/);
  assert.equal(fs.readFileSync(file, 'utf8'), '{"secret":"do-not-print"');
});

test('symlinked Preferences is rejected', t => {
  const { root, file } = temporaryProfile(t);
  fs.mkdirSync(path.dirname(file));
  const other = path.join(root, 'other');
  fs.writeFileSync(other, '{}');
  fs.symlinkSync(other, file);
  assert.throws(() => configureProfile(root), /must not be a symlink/);
  assert.equal(fs.readFileSync(other, 'utf8'), '{}');
});
