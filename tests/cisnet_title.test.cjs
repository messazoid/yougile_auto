'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { titleForSearch } = require('../scripts/cisnet_title');

test('removes feat and all following text from a search title', () => {
  assert.equal(
    titleForSearch("i smoked away my brain (i'm god x demons mashup) feat imogen heap & clams casino"),
    "i smoked away my brain (i'm god x demons mashup)",
  );
  assert.equal(titleForSearch('Song (Feat. Guest)'), 'Song');
  assert.equal(titleForSearch('Song FEAT Guest & Another'), 'Song');
});

test('keeps titles without a separate feat marker', () => {
  assert.equal(titleForSearch('Song featuring Guest'), 'Song featuring Guest');
  assert.equal(titleForSearch('Defeated'), 'Defeated');
  assert.equal(titleForSearch('Feat. Guest'), 'Feat. Guest');
});
