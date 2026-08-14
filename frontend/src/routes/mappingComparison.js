const NEUTRAL_PALETTE = {
  '--metric-pill-light-bg': '#f1f3f5',
  '--metric-pill-light-color': '#495057',
  '--metric-pill-dark-bg': '#343a40',
  '--metric-pill-dark-color': '#ced4da',
}

const PALETTE_STOPS = [
  {
    at: 0,
    lightBackground: [254, 226, 226],
    lightForeground: [153, 27, 27],
    darkBackground: [108, 31, 31],
    darkForeground: [255, 142, 142],
  },
  {
    at: 0.5,
    lightBackground: [254, 243, 199],
    lightForeground: [120, 53, 15],
    darkBackground: [54, 68, 16],
    darkForeground: [212, 226, 172],
  },
  {
    at: 1,
    lightBackground: [220, 252, 231],
    lightForeground: [22, 101, 52],
    darkBackground: [5, 86, 48],
    darkForeground: [139, 222, 174],
  },
]

// Five neutral (3-star) reviews keep tiny samples from looking conclusive while
// allowing the retailer's rating to dominate quickly as real reviews accrue.
const RATING_PRIOR_COUNT = 5

function finiteNumber(value) {
  return typeof value === 'number' && Number.isFinite(value)
}

function interpolateChannel(from, to, amount) {
  return Math.round(from + (to - from) * amount)
}

function interpolateColor(from, to, amount) {
  return `rgb(${from.map((channel, index) => interpolateChannel(channel, to[index], amount)).join(', ')})`
}

/**
 * Collapse spelling variants that describe the same unit-price basis. Unknown
 * retailer-specific bases remain separate rather than being compared blindly.
 */
export function canonicalUnitPriceBasis(basis) {
  if (typeof basis !== 'string' || !basis.trim()) return null

  const normalized = basis.trim().toLowerCase().replace(/^per\s+/, '')
  if (['kg', 'kilogram', 'kilograms'].includes(normalized)) return 'kg'
  if (['l', 'litre', 'litres', 'liter', 'liters'].includes(normalized)) return 'l'
  if (['each', 'item', 'items'].includes(normalized)) return 'each'
  return normalized
}

/**
 * The unit price to compare on: the shelf price, with any promotion stripped
 * back off. Mirrors `app.mapping.ordering.sort_unit_price`, which computes the
 * order these pills are colouring — an order that gets stored and, once
 * approved, outlives the fortnight a promotion runs for.
 */
export function sortUnitPrice(candidate) {
  if (candidate?.base_unit_price != null) return candidate.base_unit_price
  return candidate?.unit_price ?? null
}

/**
 * Return a quality score (0 = worse/red, 1 = better/green) for comparable,
 * selected values. The selected range determines position, while its size
 * relative to the minimum controls how strongly endpoints leave neutral.
 */
export function relativeQualityScores(
  items,
  {
    keyOf,
    valueOf,
    selectedOf = () => true,
    groupOf = () => 'all',
    higherIsBetter = true,
  },
) {
  const groups = new Map()

  for (const item of items) {
    if (!selectedOf(item)) continue
    const value = valueOf(item)
    const group = groupOf(item)
    if (!finiteNumber(value) || group == null) continue
    const entries = groups.get(group) ?? []
    entries.push({ key: keyOf(item), value })
    groups.set(group, entries)
  }

  const scores = new Map()
  for (const entries of groups.values()) {
    if (entries.length < 2) continue

    const values = entries.map(({ value }) => value)
    const minimum = Math.min(...values)
    const maximum = Math.max(...values)
    const range = maximum - minimum
    if (range <= 0) continue

    // Unit prices and ratings are non-negative. If a zero ever arrives, any
    // non-zero range is maximally meaningful and avoids division by zero.
    const relativeSpread = minimum > 0 ? range / minimum : 1
    const intensity = Math.min(1, Math.sqrt(relativeSpread))

    for (const { key, value } of entries) {
      const rangePosition = (value - minimum) / range
      const qualityPosition = higherIsBetter ? rangePosition : 1 - rangePosition
      scores.set(key, 0.5 + (qualityPosition - 0.5) * intensity)
    }
  }

  return scores
}

export function comparisonPalette(score) {
  if (!finiteNumber(score)) return NEUTRAL_PALETTE

  const clamped = Math.max(0, Math.min(1, score))
  const [from, to] = clamped <= 0.5 ? PALETTE_STOPS : PALETTE_STOPS.slice(1)
  const amount = (clamped - from.at) / (to.at - from.at)
  return {
    '--metric-pill-light-bg': interpolateColor(
      from.lightBackground,
      to.lightBackground,
      amount,
    ),
    '--metric-pill-light-color': interpolateColor(
      from.lightForeground,
      to.lightForeground,
      amount,
    ),
    '--metric-pill-dark-bg': interpolateColor(
      from.darkBackground,
      to.darkBackground,
      amount,
    ),
    '--metric-pill-dark-color': interpolateColor(
      from.darkForeground,
      to.darkForeground,
      amount,
    ),
  }
}

export function comparisonDescription(score) {
  if (!finiteNumber(score)) return 'not compared'
  if (score < 0.4) return 'worse among selected products'
  if (score > 0.6) return 'better among selected products'
  return 'similar among selected products'
}

/**
 * Map the standard 1–5 star scale linearly onto the colour scale, then pull
 * sparse ratings toward the neutral midpoint. This is equivalent to adding five
 * neutral reviews before calculating the colour, without changing the value we
 * display to the reviewer.
 */
export function ratingQualityScore(rating, ratingsCount) {
  if (!finiteNumber(rating)) return null

  const clampedRating = Math.max(1, Math.min(5, rating))
  const linearScore = (clampedRating - 1) / 4
  const reviewCount = finiteNumber(ratingsCount) ? Math.max(0, ratingsCount) : 0
  const confidence = reviewCount / (reviewCount + RATING_PRIOR_COUNT)
  return 0.5 + (linearScore - 0.5) * confidence
}

export function ratingDescription(score) {
  if (!finiteNumber(score)) return 'not compared'
  if (score < 0.4) return 'low confidence-adjusted rating'
  if (score > 0.6) return 'high confidence-adjusted rating'
  return 'neutral confidence-adjusted rating'
}

export function formatRating(value, ratingsCount) {
  if (!finiteNumber(value)) return '—'
  const count = ratingsCount == null ? '' : ` (${ratingsCount})`
  return `${value.toFixed(1)}★${count}`
}
