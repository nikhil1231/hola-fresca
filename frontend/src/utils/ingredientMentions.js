// Finding the ingredients inside a method step.
//
// The steps are prose, not a data field: they say "cook the onion and garlic",
// "stir in the rice", "add the stock" - never "Vegetable Stock Pot". So an
// ingredient is looked for under its full name and under every contiguous run
// of words inside it, longest first, which is what lets "the rice" reach
// "Risotto Rice" and "stock pot" reach "Vegetable Stock Pot".
//
// The cost of guessing loosely is a wrong amount shown with a confident green
// underline, so anything ambiguous is simply not marked: where two ingredients
// in the same recipe would both answer to "butter", the bare word is left as
// plain text and only the full names stay tappable.

// Articles, prepositions and conjunctions: fine inside a phrase, never at
// either end of one.
const FUNCTION_WORDS = new Set([
  'a', 'an', 'and', 'for', 'in', 'of', 'on', 'or', 'the', 'to', 'with', '&',
])
// How the ingredient was prepared or graded before it reached the box. These
// never identify it on their own - a step saying "diced" is not pointing at
// "Pre-Cooked Diced Potato".
const MODIFIER_WORDS = new Set([
  'baby', 'black', 'british', 'brown', 'chopped', 'cooked', 'diced', 'dried',
  'extra', 'fat', 'fine', 'finely', 'free', 'fresh', 'frozen', 'golden',
  'grated', 'green', 'ground', 'large', 'light', 'low', 'medium', 'mini',
  'minced', 'mixed', 'organic', 'pre-cooked', 'purple', 'raw', 'range', 'red',
  'reduced', 'ripe', 'sliced', 'small', 'style', 'white', 'whole', 'yellow',
])
// The packaging, not the food. "Garlic Clove" is referred to as garlic and
// "Mint Leaves" as mint, so these words are dropped as single-word handles -
// though they stay usable inside a phrase, because a step really does say
// "put the stock pot into a pan".
const CONTAINER_WORDS = new Set([
  'bag', 'block', 'bunch', 'can', 'cans', 'clove', 'cloves', 'jar', 'leaf',
  'leaves', 'pack', 'packet', 'packs', 'piece', 'pieces', 'pinch', 'pot',
  'pots', 'punnet', 'sachet', 'sachets', 'tin', 'tins', 'tub',
])
// Words that read as the instruction or the finished dish rather than the
// ingredient. Some are verbs - "mix in the cheese" is not a reference to
// "Fiesta Mix". The rest name what is in the pan by the time the step mentions
// it: "the curry" is the dish, not the Massaman Curry Paste that went into it,
// and "the sauce" has usually thickened out of the bottle it came from. "Water"
// is here because the steps boil far more of it than the box ships - a pan
// filled for the potatoes should not be labelled with the 150ml measured out
// for the sauce. The longer phrases survive: "curry paste" and "soy sauce"
// still match, because those name the jar.
const WEAK_SINGLE_WORDS = new Set([
  'blend', 'cover', 'curry', 'cut', 'dice', 'mix', 'mixture', 'pizza', 'risotto',
  'rub', 'sauce', 'slice', 'spread', 'toss', 'top', 'water',
])
// What the box calls it against what the step calls it. A cook reads "fry the
// fish" and the label says "Basa Fillets"; the same gap opens between "the
// pasta" and "Linguine". Only worth a table where the step's word is a whole
// category and the box always names a member of it.
const CATEGORY_WORDS = new Map([
  [
    'fish',
    new Set([
      'basa', 'bass', 'bream', 'cod', 'coley', 'haddock', 'hake', 'halibut',
      'mackerel', 'plaice', 'pollock', 'salmon', 'sardine', 'stargazer',
      'tilapia', 'trout', 'tuna', 'whiting',
    ]),
  ],
  [
    'pasta',
    new Set([
      'bucatini', 'conchiglie', 'ditali', 'farfalle', 'fettuccine', 'fusilli',
      'lasagne', 'linguine', 'macaroni', 'orecchiette', 'orzo', 'penne',
      'puntalette', 'ravioli', 'rigatoni', 'spaghetti', 'tagliatelle',
      'tortelloni', 'tortiglioni',
    ]),
  ],
])
// The dissolved, bottled or watered-down form of something else in the box.
// When both answer to the same word, the step means the food: "the chicken" is
// the breasts and not the Chicken Stock Pot, "the fish" is the fillets and not
// the Fish Sauce, "the pasta" is the linguine and not the Reserved Pasta Water.
const DILUTED_WORDS = new Set([
  'bouillon', 'cube', 'cubes', 'granules', 'paste', 'pot', 'pots', 'powder',
  'sauce', 'seasoning', 'stock', 'water',
])

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

// Names carry trailing asides the steps never repeat: "(V)", "(x2)", "(100ml)".
function normalizeName(name) {
  return name
    .toLowerCase()
    .replace(/\([^)]*\)/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

// Match the singular and the plural from whichever the name happens to use, so
// "Fresh Peas" is found in "add the peas" and "Onion" in "dice the onions".
function lastWordPattern(word) {
  if (/ies$/.test(word)) return `${escapeRegExp(word.slice(0, -3))}(?:y|ies)`
  if (/oes$/.test(word)) return `${escapeRegExp(word.slice(0, -2))}(?:es)?`
  if (/(?:ch|sh|s|x|z)es$/.test(word)) return `${escapeRegExp(word.slice(0, -2))}(?:es)?`
  if (/[^s]s$/.test(word)) return `${escapeRegExp(word.slice(0, -1))}s?`
  if (/(?:ch|sh|s|x|z)$/.test(word)) return `${escapeRegExp(word)}(?:es)?`
  if (/y$/.test(word)) return `${escapeRegExp(word.slice(0, -1))}(?:y|ies)`
  return `${escapeRegExp(word)}(?:e?s)?`
}

// The tail of "Water for the Sauce" or "Sugar for the Sauce" names the part of
// the dish it goes into, not the ingredient, and a step's "the sauce" means the
// thing in the pan. So the phrases are drawn from the head of the name only,
// while the name in full stays matchable.
function derivableName(normalized) {
  const cut = normalized.indexOf(' for ')
  return cut === -1 ? normalized : normalized.slice(0, cut)
}

function phrasePattern(phrase) {
  const words = phrase.split(' ')
  const head = words.slice(0, -1).map(escapeRegExp)
  const pattern = [...head, lastWordPattern(words[words.length - 1])].join('\\s+')
  // The names use a typewriter apostrophe and the steps a typographic one, or
  // the other way round, and "Goat's Cheese" should not care which.
  return pattern.replace(/['\u2019]/g, "['\u2019]")
}

// Every contiguous run of words in the name that could stand in for it.
function candidatePhrases(normalized) {
  const words = normalized.split(' ').filter(Boolean)
  if (words.length === 0) return []
  const phrases = []
  for (let start = 0; start < words.length; start += 1) {
    for (let end = start + 1; end <= words.length; end += 1) {
      const run = words.slice(start, end)
      const first = run[0]
      const last = run[run.length - 1]
      if (FUNCTION_WORDS.has(first) || FUNCTION_WORDS.has(last)) continue
      if (run.length === 1) {
        if (first.length < 3) continue
        if (MODIFIER_WORDS.has(first) || CONTAINER_WORDS.has(first)) continue
        if (WEAK_SINGLE_WORDS.has(first)) continue
      } else if (MODIFIER_WORDS.has(last)) {
        continue
      } else if (!run.some((w) => !FUNCTION_WORDS.has(w) && !MODIFIER_WORDS.has(w))) {
        continue
      }
      phrases.push(run.join(' '))
    }
  }
  return phrases
}

// The categories this name is a member of, e.g. "Sea Bass Fillets" -> fish.
function categoriesOf(normalized) {
  const words = normalized.split(' ')
  const found = []
  for (const [category, members] of CATEGORY_WORDS) {
    if (words.some((word) => members.has(word))) found.push(category)
  }
  return found
}

function isDiluted(ingredient) {
  return normalizeName(ingredient.name ?? '')
    .split(' ')
    .some((word) => DILUTED_WORDS.has(word))
}

/**
 * Build a matcher over one recipe's ingredients. `ingredients` are used as
 * given; filter out the ones with nothing to show before calling.
 * Returns null when there is nothing worth matching.
 */
export function buildIngredientMatcher(ingredients) {
  if (!ingredients?.length) return null

  // phrase -> ingredient, or null once two ingredients both claim it.
  const claims = new Map()
  const claim = (phrase, ingredient, exact) => {
    const existing = claims.get(phrase)
    if (!existing) {
      claims.set(phrase, { ingredient, exact })
      return
    }
    if (existing.ingredient === ingredient) return
    // The name as written outranks a word borrowed from a longer name, so a
    // recipe holding both "Butter" and "Garlic Butter" still marks "butter".
    if (existing.exact && !exact) return
    if (!existing.exact && exact) {
      claims.set(phrase, { ingredient, exact })
      return
    }
    // Already contested by two others at this standing, and a third does not
    // settle it.
    if (!existing.ingredient) return
    // Two of the same standing: the food beats the bottled version of it.
    const wasDiluted = isDiluted(existing.ingredient)
    if (wasDiluted !== isDiluted(ingredient)) {
      if (wasDiluted) claims.set(phrase, { ingredient, exact })
      return
    }
    claims.set(phrase, { ingredient: null, exact: existing.exact })
  }

  for (const ingredient of ingredients) {
    const normalized = normalizeName(ingredient.name ?? '')
    // A name that is nothing but a weak word is no better as the name than it
    // was as a fragment: a box listing "Water" still cannot tell its 150ml from
    // the panful the first step brings to the boil.
    if (!normalized || WEAK_SINGLE_WORDS.has(normalized)) continue
    claim(normalized, ingredient, true)
  }
  for (const ingredient of ingredients) {
    const normalized = normalizeName(ingredient.name ?? '')
    for (const phrase of candidatePhrases(derivableName(normalized))) {
      if (phrase !== normalized) claim(phrase, ingredient, false)
    }
    for (const category of categoriesOf(normalized)) claim(category, ingredient, false)
  }

  const byPhrase = new Map()
  for (const [phrase, entry] of claims) {
    if (entry.ingredient) byPhrase.set(phrase, entry.ingredient)
  }
  if (byPhrase.size === 0) return null

  // Longest first, so "goat's cheese" wins over "cheese" at the same position.
  // Each phrase gets its own capture group: the text that matched can be a
  // plural of the phrase, so which group fired is the only reliable way back
  // from a match to the ingredient it belongs to.
  const phrases = [...byPhrase.keys()].sort((a, b) => b.length - a.length)
  const pattern = `\\b(?:${phrases.map((phrase) => `(${phrasePattern(phrase)})`).join('|')})\\b`
  return { pattern, phrases, byPhrase }
}

// "Season with salt and pepper" is the seasoning in the cupboard, not the Bell
// Pepper on the counter. Common enough in these steps, and wrong enough when it
// happens, to be worth ruling out by hand.
function isSeasoning(text, match) {
  if (!/^peppers?$/i.test(match[0])) return false
  const before = text.slice(Math.max(0, match.index - 14), match.index)
  const after = text.slice(match.index + match[0].length)
  return /(?:salt\s*(?:,|and|&)\s*|black\s+)$/i.test(before) || /^\s*(?:,|and|&)\s*salt/i.test(after)
}

/**
 * Split step text into runs of plain text and runs that name an ingredient.
 * Each segment is `{ key, text }`, with `ingredient` set on the matches.
 */
export function splitStepText(text, matcher) {
  if (!text) return []
  if (!matcher) return [{ key: 't0', text }]

  const regex = new RegExp(matcher.pattern, 'gi')
  const segments = []
  let cursor = 0
  for (const match of text.matchAll(regex)) {
    const group = match.findIndex((value, index) => index > 0 && value !== undefined)
    const ingredient = group > 0 ? matcher.byPhrase.get(matcher.phrases[group - 1]) : null
    if (!ingredient || isSeasoning(text, match)) continue
    if (match.index > cursor) {
      segments.push({ key: `t${cursor}`, text: text.slice(cursor, match.index) })
    }
    segments.push({ key: `i${match.index}`, text: match[0], ingredient })
    cursor = match.index + match[0].length
  }
  if (cursor < text.length) segments.push({ key: `t${cursor}`, text: text.slice(cursor) })
  return segments
}
