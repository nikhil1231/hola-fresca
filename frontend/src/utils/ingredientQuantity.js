// How an ingredient line is written out at a given scale. Shared, because the
// recipe page's ingredient table and the cooking page's step tooltips have to
// agree: a cook who reads "200g" in the list and then taps "rice" mid-step
// should not be handed two different numbers.

const METRIC_UNITS = ['grams', 'milliliter(s)']
// A bare count: the source is counting the ingredient itself, not portioning it
// into containers, so the unit word carries nothing the name does not.
const COUNT_UNITS = ['unit(s)', 'unit', 'units']
// Spoon measures are how these are actually measured at the hob, so they show
// natively rather than as the gram conversion the mapping layer needs.
const SPOON_UNITS = ['tbsp', 'tsp', 'pinch']
const FRACTIONS = { 0.25: '¼', 0.5: '½', 0.75: '¾' }
// Past a handful of spoons nobody counts them out, so fall back to the weight.
const MAX_SPOONS = 8

// Round a scaled quantity to a sensible precision for display.
function roundNice(v) {
  if (v >= 20) return Math.round(v / 5) * 5
  if (v >= 1) return Math.round(v)
  return Math.round(v * 4) / 4
}

function roundCount(v) {
  return Math.round(v * 4) / 4
}

function formatCount(v) {
  const whole = Math.floor(v)
  const frac = FRACTIONS[v - whole]
  if (!frac) return String(v)
  return whole ? `${whole}${frac}` : frac
}

// Source units carry their own plural suffix, e.g. "bunch(es)" or "unit(s)".
function unitLabel(unit, n) {
  if (unit === 'pinch') return n > 1 ? 'pinches' : 'pinch'
  return unit.replace(/\((e?s)\)$/, n > 1 ? '$1' : '')
}

export function hasDisplayQuantity(ing) {
  if (ing.amount != null && ing.amount <= 0) return false
  if (ing.amount_g != null && ing.amount_g <= 0) return false
  return ing.amount != null || ing.amount_g != null
}

// Worth warning about only where both halves are true: the quantity is our
// estimate, and getting it wrong would actually hurt the dish. An estimated
// weight of onion is not worth a badge; an estimated weight of ground cloves is.
export function estimatedPotent(ing) {
  return ing.amount_g_estimated && ing.potency === 'high'
}

// "1-2" for a potent spice whose range is worth showing, else null. Collapses
// when both ends round to the same figure, so a narrow range never renders as
// the nonsense "1-1".
function potentSpan(ing, factor) {
  if (!estimatedPotent(ing) || !ing.spoon_range) return null
  const lo = roundCount(ing.spoon_range[0] * factor)
  const hi = roundCount(ing.spoon_range[1] * factor)
  if (!(lo > 0) || hi <= lo || hi > MAX_SPOONS) return null
  return `${formatCount(lo)}–${formatCount(hi)}`
}

function gramsLabel(ing, factor) {
  if (ing.amount_g == null) return null
  const metricAmount = roundNice(ing.amount_g * factor)
  return metricAmount > 0 ? `${metricAmount}${ing.canonical_unit || 'g'}` : null
}

// Format one ingredient at the chosen scale.
//
// The source's own unit leads. Where a line says "1 sachet", the weight beside
// it is our estimate from a reference table, not something HelloFresh published
// - so leading with it would put the least trustworthy number first and, worse,
// hand the cook a figure to measure against that we cannot stand behind. Native
// unit first, then the actionable translation: teaspoons for a pre-portioned
// container, the approximate weight for anything else. A line that states grams
// or millilitres outright keeps them as the headline, because there it is the
// source speaking.
export function scaledQuantity(ing, factor) {
  // Already a spoon measure: nothing to translate.
  if (ing.amount && SPOON_UNITS.includes(ing.unit)) {
    const n = Math.max(roundCount(ing.amount * factor), 0.25)
    if (n <= MAX_SPOONS) return `${formatCount(n)} ${unitLabel(ing.unit, n)}`
  }
  // The source stated a metric amount, so it leads.
  if (!ing.amount_g_estimated) {
    const grams = gramsLabel(ing, factor)
    if (grams) return grams
  }

  const nativeIsCount =
    ing.unit && !METRIC_UNITS.includes(ing.unit) && !SPOON_UNITS.includes(ing.unit)
  if (ing.amount != null && nativeIsCount) {
    const n = roundCount(ing.amount * factor)
    if (n > 0) {
      // A bare count needs no unit word - the ingredient name is already the
      // noun being counted, so "2 Sea Bass Fillets" beats "2 units Sea Bass
      // Fillets", and the gram estimate would only get in the way of an
      // instruction that is already complete.
      if (COUNT_UNITS.includes(ing.unit)) return formatCount(n)

      const native = `${formatCount(n)} ${unitLabel(ing.unit, n)}`
      // Spoons are what a cook holding a supermarket jar can act on; fall back
      // to the weight for things nobody spoons out (a bunch, a tin).
      if (ing.spoons != null) {
        const spoons = roundCount(ing.spoons * factor)
        if (spoons > 0 && spoons <= MAX_SPOONS) {
          // For a spice potent enough to ruin the dish, the span is the honest
          // answer: the container mass behind that midpoint is our estimate, and
          // the cook is better served by the range than by false precision.
          const span = potentSpan(ing, factor)
          return span
            ? `${native} (${span} tsp)`
            : `${native} (≈${formatCount(spoons)} tsp)`
        }
      }
      const grams = gramsLabel(ing, factor)
      return grams ? `${native} (≈${grams})` : native
    }
  }

  const grams = gramsLabel(ing, factor)
  if (grams) return grams
  if (ing.amount != null) {
    const amount = Math.round(ing.amount * factor * 100) / 100
    if (amount > 0) return String(amount)
  }
  return ''
}

export function splitQuantityLabel(label) {
  const match = label.match(/^(.*) \((.+)\)$/)
  if (!match) return { quantity: label, estimate: null }
  return { quantity: match[1], estimate: match[2] }
}
