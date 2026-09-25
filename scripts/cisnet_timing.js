'use strict';

function readTiming(env = process.env) {
  function milliseconds(name, fallback, limit) {
    const raw = env[name] ?? String(fallback);
    if (!/^(0|[1-9][0-9]*)$/.test(raw)) throw new Error(`${name} must be an integer from 0 to ${limit}`);
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value > limit) {
      throw new Error(`${name} must be an integer from 0 to ${limit}`);
    }
    return value;
  }

  const keyDelayMs = milliseconds('CISNET_KEY_DELAY_MS', 40, 250);
  const actionDelayMinMs = milliseconds('CISNET_ACTION_DELAY_MIN_MS', 120, 2000);
  const actionDelayMaxMs = milliseconds('CISNET_ACTION_DELAY_MAX_MS', 280, 2000);
  if (actionDelayMaxMs < actionDelayMinMs) {
    throw new Error('CISNET_ACTION_DELAY_MAX_MS must be at least CISNET_ACTION_DELAY_MIN_MS');
  }
  return { keyDelayMs, actionDelayMinMs, actionDelayMaxMs };
}

function createTiming(config = readTiming()) {
  async function pause() {
    const { actionDelayMinMs: min, actionDelayMaxMs: max } = config;
    const delay = min + Math.floor(Math.random() * (max - min + 1));
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
  }

  async function act(action) {
    await pause();
    return action();
  }

  async function enterText(locator, value) {
    await pause();
    await locator.fill('');
    if (value) await locator.pressSequentially(value, { delay: config.keyDelayMs });
  }

  return { act, enterText };
}

module.exports = { readTiming, createTiming };
