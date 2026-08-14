import assert from 'node:assert/strict'
import test from 'node:test'

import {
  canonicalUnitPriceBasis,
  comparisonPalette,
  formatRating,
  ratingQualityScore,
  relativeQualityScores,
} from './mappingComparison.js'

function scoresFor(items, options = {}) {
  return relativeQualityScores(items, {
    keyOf: (item) => item.id,
    valueOf: (item) => item.value,
    selectedOf: (item) => item.selected !== false,
    ...options,
  })
}

test('a small relative spread remains close to neutral', () => {
  const scores = scoresFor([
    { id: 'lower', value: 4.8 },
    { id: 'higher', value: 4.9 },
  ])

  assert.ok(scores.get('lower') > 0.4)
  assert.ok(scores.get('higher') < 0.6)
})

test('a large unit-price spread reaches the endpoints with lower prices better', () => {
  const scores = scoresFor(
    [
      { id: 'cheap', value: 2 },
      { id: 'middle', value: 3 },
      { id: 'expensive', value: 4 },
    ],
    { higherIsBetter: false },
  )

  assert.equal(scores.get('cheap'), 1)
  assert.equal(scores.get('middle'), 0.5)
  assert.equal(scores.get('expensive'), 0)
})

test('single, equal, missing, and unselected values remain unscored', () => {
  const scores = scoresFor([
    { id: 'same-a', value: 3 },
    { id: 'same-b', value: 3 },
    { id: 'missing', value: null },
    { id: 'unselected', value: 8, selected: false },
  ])

  assert.equal(scores.size, 0)
})

test('unit prices compare only within canonical like-for-like groups', () => {
  const items = [
    { id: 'retailer-kg', value: 8, basis: 'kg' },
    { id: 'manual-kg', value: 10, basis: 'per kg' },
    { id: 'litre', value: 2, basis: 'l' },
  ]
  const scores = scoresFor(items, {
    groupOf: (item) => canonicalUnitPriceBasis(item.basis),
    higherIsBetter: false,
  })

  assert.ok(scores.get('retailer-kg') > scores.get('manual-kg'))
  assert.equal(scores.has('litre'), false)
  assert.equal(canonicalUnitPriceBasis('per litre'), 'l')
})

test('ratings display at one decimal place', () => {
  assert.equal(formatRating(4.86, 127), '4.9★ (127)')
  assert.equal(formatRating(4, null), '4.0★')
  assert.equal(formatRating(null, 0), '—')
})

test('rating colour uses the fixed linear 1–5 scale and clamps outliers', () => {
  assert.ok(ratingQualityScore(1, 100) < ratingQualityScore(3, 100))
  assert.ok(ratingQualityScore(3, 100) < ratingQualityScore(5, 100))
  assert.equal(ratingQualityScore(0.5, 100), ratingQualityScore(1, 100))
  assert.equal(ratingQualityScore(5.5, 100), ratingQualityScore(5, 100))
})

test('low review counts pull rating colour toward neutral', () => {
  const sparsePerfectRating = ratingQualityScore(5, 2)
  const provenHighRating = ratingQualityScore(4.7, 500)
  const credibleNearPerfectRating = ratingQualityScore(4.9, 20)

  assert.ok(sparsePerfectRating > 0.5)
  assert.ok(sparsePerfectRating < 0.7)
  assert.ok(sparsePerfectRating < provenHighRating)
  assert.ok(provenHighRating > 0.9)
  assert.ok(credibleNearPerfectRating > 0.85)
  assert.equal(ratingQualityScore(5, 0), 0.5)
})

test('the palette provides distinct light and dark theme colours', () => {
  const palette = comparisonPalette(1)

  assert.notEqual(palette['--metric-pill-light-bg'], palette['--metric-pill-dark-bg'])
  assert.notEqual(palette['--metric-pill-light-color'], palette['--metric-pill-dark-color'])
})
