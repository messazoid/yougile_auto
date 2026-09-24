'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const SETTINGS = [
  [['credentials_enable_service'], false],
  [['credentials_enable_autosignin'], false],
  [['profile', 'password_manager_enabled'], false],
  [['autofill', 'profile_enabled'], false],
  [['autofill', 'credit_card_enabled'], false],
  [['translate', 'enabled'], false],
  [['profile', 'default_content_setting_values', 'notifications'], 2],
  [['profile', 'default_content_setting_values', 'geolocation'], 2],
  [['profile', 'default_content_setting_values', 'media_stream_camera'], 2],
  [['profile', 'default_content_setting_values', 'media_stream_mic'], 2],
];

function setPreference(preferences, keys, value) {
  let section = preferences;
  for (const key of keys.slice(0, -1)) {
    if (section[key] === undefined) section[key] = {};
    if (section[key] === null || typeof section[key] !== 'object' || Array.isArray(section[key])) {
      throw new Error('Chromium Preferences has an invalid settings section');
    }
    section = section[key];
  }
  const key = keys[keys.length - 1];
  if (section[key] === value) return false;
  section[key] = value;
  return true;
}

function configureProfile(userDataDir) {
  const profileDir = path.join(userDataDir, 'Default');
  const preferencesPath = path.join(profileDir, 'Preferences');
  fs.mkdirSync(profileDir, { recursive: true, mode: 0o700 });
  if (fs.lstatSync(profileDir).isSymbolicLink()) {
    throw new Error('Chromium profile directory must not be a symlink');
  }

  let preferences = {};
  try {
    if (fs.lstatSync(preferencesPath).isSymbolicLink()) {
      throw new Error('Chromium Preferences must not be a symlink');
    }
    try {
      preferences = JSON.parse(fs.readFileSync(preferencesPath, 'utf8'));
    } catch (error) {
      if (error instanceof SyntaxError) {
        throw new Error('Chromium Preferences is invalid JSON');
      }
      throw error;
    }
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  if (preferences === null || typeof preferences !== 'object' || Array.isArray(preferences)) {
    throw new Error('Chromium Preferences must be a JSON object');
  }

  let changed = !fs.existsSync(preferencesPath);
  for (const [keys, value] of SETTINGS) {
    changed = setPreference(preferences, keys, value) || changed;
  }
  if (!changed) return false;

  const temporaryPath = `${preferencesPath}.tmp-${process.pid}-${randomUUID()}`;
  try {
    fs.writeFileSync(temporaryPath, JSON.stringify(preferences), { flag: 'wx', mode: 0o600 });
    fs.renameSync(temporaryPath, preferencesPath);
  } finally {
    if (fs.existsSync(temporaryPath)) fs.unlinkSync(temporaryPath);
  }
  return true;
}

module.exports = { configureProfile };
