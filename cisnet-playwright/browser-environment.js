'use strict';

function readBrowserEnvironment(env = process.env) {
  const locale = env.CISNET_LOCALE || 'en-US';
  const timezoneId = env.CISNET_TIMEZONE || 'UTC';
  try {
    if (!Intl.DateTimeFormat.supportedLocalesOf([locale]).length) {
      throw new RangeError('unsupported locale');
    }
    new Intl.DateTimeFormat(locale, { timeZone: timezoneId });
  } catch {
    throw new Error('CISNET_LOCALE or CISNET_TIMEZONE is invalid');
  }

  function dimension(name, fallback, minimum, maximum) {
    const value = env[name] || String(fallback);
    if (!/^[1-9][0-9]{2,3}$/.test(value)) throw new Error(`${name} is invalid`);
    const number = Number(value);
    if (number < minimum || number > maximum) throw new Error(`${name} is invalid`);
    return number;
  }

  return {
    locale,
    timezoneId,
    screenWidth: dimension('CISNET_SCREEN_WIDTH', 1300, 800, 3840),
    screenHeight: dimension('CISNET_SCREEN_HEIGHT', 1080, 600, 2160),
  };
}

module.exports = { readBrowserEnvironment };
