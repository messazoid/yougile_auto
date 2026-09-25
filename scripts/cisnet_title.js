'use strict';

function titleForSearch(title) {
  const original = title.trim();
  const marker = /\s+(?:\(\s*)?feat\.?(?=\s|$)/iu.exec(original);
  if (!marker) return original;
  const prefix = original.slice(0, marker.index).trim();
  return prefix || original;
}

module.exports = { titleForSearch };
