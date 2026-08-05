import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Divider,
  Grid,
  Group,
  Image,
  Loader,
  Menu,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  SimpleGrid,
  Skeleton,
  Table,
  Stack,
  Text,
  ThemeIcon,
  Title,
  Tooltip,
} from '@mantine/core'
import {
  IconAlertTriangle,
  IconArrowBackUp,
  IconArrowLeft,
  IconCalendarWeek,
  IconChefHat,
  IconClock,
  IconDots,
  IconExternalLink,
  IconFlag,
  IconFlame,
  IconHeart,
  IconHeartFilled,
  IconEyeOff,
  IconLock,
  IconMinus,
  IconPhoto,
  IconPlus,
  IconBasketPlus,
  IconStarFilled,
  IconTrash,
  IconUsers,
} from '@tabler/icons-react'

import {
  useAuditRecipe,
  useHideRecipe,
  usePlannerBasket,
  usePersonalRecipeRating,
  useProteinPreview,
  useRecipe,
  useRecipeWishlist,
  useRevertRecipeEdits,
} from '../hooks/useRecipeQueries.js'
import { usePreloadStepImages } from '../hooks/usePreloadStepImages.js'
import {
  formatWeekRange,
  isPastWeekStart,
  resolveTargetWeek,
  useScheduleWithHistory,
} from '../hooks/useSchedule.js'
import {
  DEFAULT_PORTIONS,
  DEFAULT_RECIPES_PER_WEEK,
  formatProteinModifier,
  MAX_PORTIONS,
  MIN_PORTIONS,
  normalizeProtein,
  toPlannerSelections,
  useWeeklyPlan,
} from '../hooks/useWeeklyPlan.js'
import { RECIPE_PLACEHOLDER_IMAGE } from '../constants/images.js'
import classes from './RecipeDetailPage.module.css'

const MACRO_LABELS = {
  energy_kcal: 'Energy',
  protein_g: 'Protein',
  carbs_g: 'Carbs',
  fat_g: 'Fat',
}

const MACRO_COLORS = {
  Energy: 'var(--mantine-color-yellow-5)',
  Protein: 'var(--mantine-color-green-6)',
  Carbs: 'var(--mantine-color-blue-6)',
  Fat: 'var(--mantine-color-red-6)',
}

const money = new Intl.NumberFormat('en-GB', {
  style: 'currency',
  currency: 'GBP',
})

function formatMoney(value) {
  return money.format(value ?? 0)
}

function formatSignedMoney(value) {
  if (value == null) return null
  const absolute = Math.abs(value)
  return `${value < 0 ? '-' : '+'}${formatMoney(absolute)}`
}

const DIFFICULTY = { 1: 'Easy', 2: 'Medium', 3: 'Hard' }
const SERVINGS = [0.5, 1, 2, 3, 4, 6, 8]
const SERVINGS_LABELS = { 0.5: '½', 1: '1', 2: '2', 3: '3', 4: '4', 6: '6', 8: '8' }
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

// Format one ingredient at the chosen scale.
//
// The source's own unit leads. Where a line says "1 sachet", the weight beside
// it is our estimate from a reference table, not something HelloFresh published
// — so leading with it would put the least trustworthy number first and, worse,
// hand the cook a figure to measure against that we cannot stand behind. Native
// unit first, then the actionable translation: teaspoons for a pre-portioned
// container, the approximate weight for anything else. A line that states grams
// or millilitres outright keeps them as the headline, because there it is the
// source speaking.
function hasDisplayQuantity(ing) {
  if (ing.amount != null && ing.amount <= 0) return false
  if (ing.amount_g != null && ing.amount_g <= 0) return false
  return ing.amount != null || ing.amount_g != null
}

// Worth warning about only where both halves are true: the quantity is our
// estimate, and getting it wrong would actually hurt the dish. An estimated
// weight of onion is not worth a badge; an estimated weight of ground cloves is.
function estimatedPotent(ing) {
  return ing.amount_g_estimated && ing.potency === 'high'
}

// "1–2" for a potent spice whose range is worth showing, else null. Collapses
// when both ends round to the same figure, so a narrow range never renders as
// the nonsense "1–1".
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

function scaledQuantity(ing, factor) {
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
      // A bare count needs no unit word — the ingredient name is already the
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

function splitQuantityLabel(label) {
  const match = label.match(/^(.*) \((.+)\)$/)
  if (!match) return { quantity: label, estimate: null }
  return { quantity: match[1], estimate: match[2] }
}

// What the recipe consumes, priced pro-rata: a recipe using 5 g from a 60 g bag
// of sesame seeds is charged a twelfth of the bag. This is deliberately not what
// the basket costs — see the totals row, which shows both.
function ingredientCostByKey(basket, recipeId) {
  const costs = new Map()
  for (const line of basket?.lines ?? []) {
    const contribution = line.contributions?.find((c) => c.recipe_id === recipeId)
    if (!contribution) continue

    let cost = 0
    if (line.need_qty != null && line.capacity_qty) {
      cost = line.cost * ((contribution.quantity ?? 0) / line.capacity_qty)
    } else if (line.capacity_g) {
      cost = line.cost * ((contribution.grams ?? 0) / line.capacity_g)
    }
    costs.set(line.key, (costs.get(line.key) ?? 0) + cost)
  }
  return costs
}

function sumCosts(costs) {
  let total = 0
  for (const cost of costs.values()) total += cost
  return total
}

function PlannerControls({ disabled, entry, readOnly = false, onAdd, onPortionsChange, onRemove }) {
  // On a week that has been shopped for, the portions are what was cooked. Shown
  // plainly, with nothing offering to change them; adding the recipe to a
  // finished week is not a thing that can happen either.
  if (readOnly) {
    if (!entry) return null
    return (
      <Badge color="gray" variant="light" radius="sm" size="lg" leftSection={<IconUsers size={13} />}>
        {entry.portions} portions
      </Badge>
    )
  }

  if (!entry) {
    return (
      <Button
        color="fresh"
        leftSection={<IconBasketPlus size={17} />}
        disabled={disabled}
        onClick={() => !disabled && onAdd?.()}
      >
        Add to this week
      </Button>
    )
  }

  const portions = entry.portions
  return (
    <Group gap="xs" wrap="nowrap">
      <Group gap={4} wrap="nowrap" className={classes.portionStepper}>
        <Tooltip label="Decrease portions" withArrow>
          <ActionIcon
            variant="subtle"
            color="gray"
            radius="xl"
            disabled={portions <= MIN_PORTIONS}
            aria-label="Decrease portions"
            onClick={() => onPortionsChange?.(portions - 1)}
          >
            <IconMinus size={16} />
          </ActionIcon>
        </Tooltip>
        <Text fw={700} size="sm" className={classes.portionCount}>
          {portions}
        </Text>
        <Tooltip label="Increase portions" withArrow>
          <ActionIcon
            variant="subtle"
            color="fresh"
            radius="xl"
            disabled={portions >= MAX_PORTIONS}
            aria-label="Increase portions"
            onClick={() => onPortionsChange?.(portions + 1)}
          >
            <IconPlus size={16} />
          </ActionIcon>
        </Tooltip>
      </Group>
      <Tooltip label="Remove recipe" withArrow>
        <ActionIcon color="red" variant="filled" radius="xl" aria-label="Remove recipe" onClick={onRemove}>
          <IconTrash size={17} />
        </ActionIcon>
      </Tooltip>
    </Group>
  )
}

function PersonalRatingControl({ value, onSet, pending }) {
  const [hovered, setHovered] = useState(null)
  const displayValue = hovered ?? value ?? 0

  return (
    <Group gap={1} wrap="nowrap" role="radiogroup" aria-label="Personal rating">
      {Array.from({ length: 5 }).map((_, index) => {
        const starValue = index + 1
        const active = starValue <= displayValue
        return (
          <ActionIcon
            key={starValue}
            type="button"
            variant="transparent"
            color="gray"
            size="xs"
            radius="xl"
            disabled={pending}
            data-darkreader-ignore="true"
            className={active ? classes.personalStarActive : classes.personalStar}
            aria-label={
              value === starValue
                ? `Clear ${starValue} star personal rating`
                : `Set ${starValue} star personal rating`
            }
            aria-checked={value === starValue}
            role="radio"
            onMouseEnter={() => setHovered(starValue)}
            onMouseLeave={() => setHovered(null)}
            onFocus={() => setHovered(starValue)}
            onBlur={() => setHovered(null)}
            onClick={() => onSet(value === starValue ? null : starValue)}
          >
            <IconStarFilled size={14} />
          </ActionIcon>
        )
      })}
    </Group>
  )
}

function WishlistButton({ wishlisted, pending, onToggle }) {
  const label = wishlisted ? 'Remove from wishlist' : 'Add to wishlist'

  return (
    <Tooltip label={label} withArrow>
      <ActionIcon
        type="button"
        variant={wishlisted ? 'filled' : 'light'}
        color={wishlisted ? 'red' : 'gray'}
        radius="xl"
        size="lg"
        className={classes.wishlistButton}
        data-active={wishlisted || undefined}
        disabled={pending}
        aria-label={label}
        aria-pressed={wishlisted}
        onClick={() => onToggle(!wishlisted)}
      >
        {wishlisted ? <IconHeartFilled size={19} /> : <IconHeart size={19} />}
      </ActionIcon>
    </Tooltip>
  )
}

function RecipeRatings({ recipe, personalRating, wishlist }) {
  return (
    <Paper withBorder radius="md" p="sm" className={classes.ratingsPanel}>
      <Group gap="lg" align="center" wrap="wrap">
        <Stack gap={3} className={classes.ratingItem}>
          <Text size="xs" c="dimmed" tt="uppercase" fw={700}>
            Source
          </Text>
          {recipe.avg_rating != null ? (
            <Group gap={6} wrap="nowrap" className={classes.ratingValue}>
              <IconStarFilled size={17} className={classes.star} />
              <Text fw={700}>{recipe.avg_rating.toFixed(1)}</Text>
              {recipe.ratings_count != null && (
                <Text c="dimmed" size="sm">
                  ({recipe.ratings_count.toLocaleString()})
                </Text>
              )}
            </Group>
          ) : (
            <Text size="sm" c="dimmed">
              Not rated
            </Text>
          )}
        </Stack>

        <Stack gap={3} className={classes.ratingItem}>
          <Text size="xs" c="dimmed" tt="uppercase" fw={700}>
            Yours
          </Text>
          <PersonalRatingControl
            value={recipe.personal_rating}
            pending={personalRating.isPending}
            onSet={(rating) => personalRating.mutate(rating)}
          />
        </Stack>

        <Stack gap={3} className={classes.ratingItem}>
          <Text size="xs" c="dimmed" tt="uppercase" fw={700}>
            Wishlist
          </Text>
          <WishlistButton
            wishlisted={recipe.wishlisted}
            pending={wishlist.isPending}
            onToggle={(wishlisted) => wishlist.mutate(wishlisted)}
          />
        </Stack>
      </Group>
    </Paper>
  )
}

function RecipeOptionsMenu({ pending, onHide }) {
  return (
    <Menu shadow="md" position="bottom-end" withinPortal>
      <Menu.Target>
        <ActionIcon variant="subtle" color="gray" size="lg" aria-label="Recipe options">
          {pending ? <Loader size={14} /> : <IconDots size={18} />}
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Recipe</Menu.Label>
        <Menu.Item color="red" leftSection={<IconEyeOff size={14} />} onClick={onHide} disabled={pending}>
          Hide from library
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  )
}

function MacroStat({ label, value, unit, corrected }) {
  return (
    <Paper withBorder radius="md" p="sm" className={classes.macro}>
      <Text fz="xl" fw={700}>
        <span className={classes.macroDot} style={{ '--macro-color': MACRO_COLORS[label] }} />
        {value == null ? '—' : `${Math.round(value)}`}
        <Text span fz="sm" c="dimmed" fw={500}>
          {unit}
        </Text>
      </Text>
      <Group gap={6} justify="center" wrap="nowrap">
        <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
          {label}
        </Text>
        {corrected != null && (
          <Badge size="xs" variant="light" color="orange">
            was {Math.round(corrected)}
          </Badge>
        )}
      </Group>
    </Paper>
  )
}

// The macro numbers come from HelloFresh and are sometimes wrong. Reporting that
// is a rare, deliberate act, so it lives behind a quiet menu rather than a
// permanent orange button that reads as a warning about the recipe itself.
function MacroMenu({ audit, revert, hasEdits }) {
  return (
    <Menu shadow="md" position="bottom-end" withinPortal>
      <Menu.Target>
        <ActionIcon
          variant="subtle"
          color="gray"
          size="sm"
          aria-label="Nutrition data options"
        >
          {audit.running ? <Loader size={12} /> : <IconDots size={16} />}
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Nutrition data</Menu.Label>
        <Menu.Item
          leftSection={<IconFlag size={14} />}
          onClick={audit.run}
          disabled={audit.running}
        >
          {hasEdits ? 'Re-check these numbers' : 'Report wrong macros'}
        </Menu.Item>
        {hasEdits && (
          <Menu.Item
            leftSection={<IconArrowBackUp size={14} />}
            onClick={() => revert.mutate()}
            disabled={revert.isPending}
          >
            Restore original numbers
          </Menu.Item>
        )}
      </Menu.Dropdown>
    </Menu>
  )
}

// Outcomes of a check, and the standing record of any correction. Split from the
// menu so the trigger can sit inline with the macro caption while the detail
// appears below the numbers it concerns.
function MacroNotes({ audit, edits }) {
  const result = audit.job?.result

  if (!audit.running && !audit.error && !result && edits.length === 0) return null

  return (
    <Stack gap="xs">
      {audit.running && (
        <Text size="xs" c="dimmed">
          Checking the arithmetic, then the ingredients if it can't be settled that way…
        </Text>
      )}

      {audit.error && (
        <Alert color="red" variant="light" onClose={audit.dismiss} withCloseButton>
          {audit.error}
        </Alert>
      )}

      {result?.verdict === 'ok' && (
        <Alert
          color="green"
          variant="light"
          icon={<IconFlag size={16} />}
          onClose={audit.dismiss}
          withCloseButton
        >
          Checked {result.checked.join(' and ')} — the macros hold up.
        </Alert>
      )}

      {result?.verdict === 'inconclusive' && (
        <Alert color="yellow" variant="light" onClose={audit.dismiss} withCloseButton>
          Something looks off, but nothing here can say which number is wrong, so the
          figures were left alone rather than guessed at.
        </Alert>
      )}

      {/* Shown even when the macros pass: they cross-check against each other, which
          says nothing about whether the quantities are right — and the quantities are
          what the shopping basket is built from. */}
      {result?.ingredient_gaps?.length > 0 && (
        <Alert
          color="orange"
          variant="light"
          icon={<IconAlertTriangle size={16} />}
          title="The ingredient quantities are wrong"
        >
          <Stack gap={4}>
            {result.ingredient_gaps.map((gap) => (
              <Text key={gap} size="xs">
                {gap}
              </Text>
            ))}
            <Text size="xs" c="dimmed">
              Macros can't be verified from the ingredients until these are fixed, and the
              shopping basket will be wrong for this recipe.
            </Text>
          </Stack>
        </Alert>
      )}

      {/* A standing note rather than an alert: the correction is settled, and the
          tiles already carry a "was N" badge. */}
      {edits.length > 0 && (
        <Paper withBorder radius="md" p="xs" bg="var(--mantine-color-default)">
          <Text size="xs" fw={600} c="dimmed" tt="uppercase" mb={4}>
            Corrected
          </Text>
          <Stack gap={2}>
            {edits.map((e, i) => (
              <Text key={`${e.field}-${i}`} size="xs" c="dimmed">
                <Text span fw={600} c="var(--mantine-color-text)">
                  {MACRO_LABELS[e.field] ?? e.field}
                </Text>{' '}
                {e.old_value == null ? 'was missing' : Math.round(e.old_value)}
                {' → '}
                {e.new_value == null ? '—' : Math.round(e.new_value)} · {e.reason}
                {e.source === 'llm' ? ' (from ingredient composition)' : ' (arithmetic)'}
              </Text>
            ))}
          </Stack>
        </Paper>
      )}
    </Stack>
  )
}

// Scale presets, plus the mode where you name the number you want a portion to
// hit and the weight is solved backwards from it.
const PROTEIN_SCALES = [
  { value: '1', label: 'As written' },
  { value: '1.5', label: '1.5x' },
  { value: '2', label: '2x' },
  { value: 'target', label: 'Target' },
]
const DEFAULT_TARGETS = { protein_g: 50, energy_kcal: 700 }

function proteinScaleMode(modifier) {
  if (modifier?.target_mode) return 'target'
  if (modifier?.scale) return String(modifier.scale)
  return '1'
}

function ProteinControls({ profile, modifier, preview, baseYield, onChange, readOnly = false }) {
  const scaleMode = proteinScaleMode(modifier)
  const targetMode = modifier?.target_mode ?? 'protein_g'
  const targetValue = modifier?.target_value ?? DEFAULT_TARGETS[targetMode]
  const summary = formatProteinModifier(modifier)

  const setScale = (value) => {
    const { scale: _scale, target_mode: _mode, target_value: _value, ...rest } = modifier ?? {}
    if (value === 'target') {
      onChange({ ...rest, target_mode: targetMode, target_value: targetValue })
    } else if (value === '1') {
      onChange(rest)
    } else {
      onChange({ ...rest, scale: Number(value) })
    }
  }

  const setTarget = (mode, value) => {
    const { scale: _scale, ...rest } = modifier ?? {}
    onChange({ ...rest, target_mode: mode, target_value: value })
  }

  const changed = preview?.changed
  const macrosBefore = preview?.macros_before
  const macrosAfter = preview?.macros_after

  return (
    <Stack gap={8}>
      <Group gap={6} justify="space-between" align="baseline">
        <Text size="sm" fw={600}>
          Protein
        </Text>
        {changed && !readOnly && (
          <Anchor component="button" type="button" size="xs" c="dimmed" onClick={() => onChange(null)}>
            Reset
          </Anchor>
        )}
      </Group>
      {/* A finished week states its modifier rather than offering it: the swap
          is what you cooked, and the controls that would change it belong to a
          week still being planned. The comparison below stays either way — it is
          the whole reason to look. */}
      {readOnly ? (
        <Badge
          color={summary ? 'grape' : 'gray'}
          variant="light"
          radius="sm"
          w="fit-content"
        >
          {summary ?? `${profile.name}, as written`}
        </Badge>
      ) : (
        <>
          <Select
            size="xs"
            allowDeselect={false}
            value={modifier?.swap_to ?? ''}
            onChange={(value) => {
              const { swap_to: _swap, ...rest } = modifier ?? {}
              onChange(value ? { ...rest, swap_to: value } : rest)
            }}
            data={[
              { value: '', label: `${profile.name} (as written)` },
              ...profile.targets.map((target) => ({
                value: target.id,
                label: target.available ? target.label : `${target.label} (out of stock)`,
              })),
            ]}
          />
          <SegmentedControl
            size="xs"
            color="fresh"
            fullWidth
            value={scaleMode}
            onChange={setScale}
            data={PROTEIN_SCALES}
          />
          {scaleMode === 'target' && (
            <Group gap={6} wrap="nowrap">
              <NumberInput
                size="xs"
                min={1}
                max={2000}
                step={5}
                value={targetValue}
                onChange={(value) => setTarget(targetMode, Number(value) || targetValue)}
                style={{ flex: '0 0 84px' }}
              />
              <Select
                size="xs"
                allowDeselect={false}
                value={targetMode}
                onChange={(mode) => setTarget(mode, DEFAULT_TARGETS[mode])}
                data={[
                  { value: 'protein_g', label: 'g protein a serving' },
                  { value: 'energy_kcal', label: 'kcal a serving' },
                ]}
                style={{ flex: 1 }}
              />
            </Group>
          )}
        </>
      )}
      {/* Everything here is stated a serving, so it reads against the macros
          above rather than against the ingredient list, which is sized to
          whatever the servings control says. */}
      {changed && macrosAfter && (
        <Text size="xs" c="dimmed">
          {Math.round(preview.grams_before / baseYield)}g →{' '}
          <b>{Math.round(preview.grams_after / baseYield)}g</b> of{' '}
          {preview.protein_name_after ?? preview.protein_name} a serving ·{' '}
          {macrosBefore.protein_g}g → <b>{macrosAfter.protein_g}g</b> protein and {macrosBefore.kcal}{' '}
          → <b>{macrosAfter.kcal}</b> kcal
        </Text>
      )}
      {preview?.diet_changes?.length > 0 && (
        <Group gap={4}>
          {preview.diet_changes.map((label) => (
            <Badge key={label} size="sm" radius="sm" variant="light" color="fresh">
              {label}
            </Badge>
          ))}
        </Group>
      )}
      {preview?.cook_note && (
        <Text size="xs" c="dimmed" fs="italic">
          {preview.cook_note}
        </Text>
      )}
      {(preview?.warnings ?? []).map((warning) => (
        <Text key={warning} size="xs" c="orange">
          {warning}
        </Text>
      ))}
      {changed && (
        <Text size="xs" c="dimmed">
          Macros are estimated from reference figures for each protein, not measured.
        </Text>
      )}
    </Stack>
  )
}

export default function RecipeDetailPage() {
  const { id } = useParams()
  const recipeId = Number(id)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { data: recipe, isLoading, isError } = useRecipe(id)
  const [servingsOverride, setServingsOverride] = useState(null)
  const {
    upcomingWeekStart,
    getWeekRecipes,
    getRecipeEntry,
    addRecipeToWeek,
    removeRecipeFromWeek,
    setRecipePortions,
    setRecipeProtein,
  } = useWeeklyPlan()
  // `undefined` means "not touched on this page", which is what lets a planned
  // recipe open showing the swap the week already holds.
  const [proteinOverride, setProteinOverride] = useState(undefined)
  // Which week this page is about: the one carried in from browse or from the
  // week rows on Home, otherwise whichever week the schedule says is being
  // planned. Fetched with history, because a link from a finished week is how
  // you find out that those five recipes were cooked at 1.5x protein — and
  // without the history it would resolve to next week and show you nothing.
  const { data: schedule } = useScheduleWithHistory()
  const targetWeek = resolveTargetWeek(schedule, searchParams.get('week'))
  const weekStart = targetWeek?.week_start ?? upcomingWeekStart
  const weekReadOnly = isPastWeekStart(weekStart)
  const recipesPerWeek = schedule?.settings?.recipes_per_week ?? DEFAULT_RECIPES_PER_WEEK
  const upcomingRecipes = useMemo(
    () => getWeekRecipes(weekStart),
    [getWeekRecipes, weekStart],
  )
  const plannerEntry = getRecipeEntry(recipeId, weekStart)
  const upcomingWeekFull = upcomingRecipes.length >= recipesPerWeek
  // The servings control drives every number on the page. Seeded from the
  // planner so a recipe already in the week at six portions opens showing six —
  // it used to open at the default and price a six-portion basket beside a
  // four-portion ingredient list, with nothing on screen to say why they
  // disagreed.
  const selectedServings = servingsOverride ?? plannerEntry?.portions ?? DEFAULT_PORTIONS
  const protein = proteinOverride !== undefined ? proteinOverride : plannerEntry?.protein ?? null
  const currentSelections = useMemo(() => toPlannerSelections(upcomingRecipes), [upcomingRecipes])
  const withoutRecipeSelections = useMemo(
    () => currentSelections.filter((selection) => selection.recipe_id !== recipeId),
    [currentSelections, recipeId],
  )
  // The week including this recipe at the servings on screen — whether it is
  // already planned (at whatever portions) or not planned at all. One basket
  // behind both the per-ingredient costs and the marginal total, so the two can
  // no longer be computed at different sizes.
  const withRecipeSelections = useMemo(() => {
    if (!recipe) return currentSelections
    const modifier = protein ? { protein } : {}
    if (plannerEntry) {
      return currentSelections.map((selection) =>
        selection.recipe_id === recipe.id
          ? { recipe_id: selection.recipe_id, portions: selectedServings, ...modifier }
          : selection,
      )
    }
    return [
      ...currentSelections,
      { recipe_id: recipe.id, portions: selectedServings, ...modifier },
    ]
  }, [currentSelections, plannerEntry, protein, recipe, selectedServings])
  const { data: withoutRecipeBasket } = usePlannerBasket(withoutRecipeSelections)
  const { data: withRecipeBasket } = usePlannerBasket(withRecipeSelections)
  // Keyed on the route param, so these sit above the loading/error returns below
  // and are never called conditionally.
  const { data: proteinPreview } = useProteinPreview(id, protein, {
    enabled: Boolean(recipe?.protein),
  })
  const audit = useAuditRecipe(id)
  const revert = useRevertRecipeEdits(id)
  const personalRating = usePersonalRecipeRating(id)
  const wishlist = useRecipeWishlist(id)
  const hideRecipe = useHideRecipe(id)
  usePreloadStepImages(recipe?.steps)

  if (isLoading) {
    return (
      <Stack gap="lg">
        <Skeleton height={36} width={120} />
        <Skeleton height={360} radius="md" />
        <Skeleton height={28} width="60%" />
        <Skeleton height={80} />
      </Stack>
    )
  }

  if (isError || !recipe) {
    return (
      <Stack gap="md">
        <Alert color="red" title="Recipe not found">
          We couldn't find that recipe.
        </Alert>
        <Button component={Link} to="/browse" variant="light" color="fresh" w="fit-content">
          Back to recipes
        </Button>
      </Stack>
    )
  }

  const baseYield = recipe.base_yield || 2
  const servings = selectedServings
  const factor = servings / baseYield
  // The modified recipe stands in for the stored one everywhere the page shows
  // the dish itself — quantities, method, macros. Everything else (ratings, the
  // audit trail, the library's own flags) still describes the recipe as
  // published, because that is what it is a record of.
  const modified = proteinPreview?.changed ? proteinPreview : null
  const view = modified
    ? {
        ...recipe,
        ingredients: modified.ingredients,
        steps: modified.steps,
        energy_kcal: modified.macros_after.kcal,
        protein_g: modified.macros_after.protein_g,
        carbs_g: modified.macros_after.carbs_g,
        fat_g: modified.macros_after.fat_g,
      }
    : recipe
  const applyProtein = (next) => {
    if (weekReadOnly) return
    const value = normalizeProtein(next)
    setProteinOverride(value)
    if (plannerEntry) setRecipeProtein(weekStart, recipe.id, value)
  }
  const ingredientCosts = ingredientCostByKey(withRecipeBasket, recipe.id)
  // Two different questions, and the page shows both because neither alone is
  // the answer. `usedTotal` is what the recipe consumes, priced pro-rata across
  // the packs it draws on. `marginalTotal` is what the shop actually costs you:
  // whole packs, minus whatever the rest of the week already covers. A recipe
  // wanting 5 g of black sesame seeds pays 33p of a bag in the first and £2.00
  // for the bag in the second, and the difference is sitting in your cupboard.
  const usedTotal = sumCosts(ingredientCosts)
  const marginalTotal =
    withRecipeBasket && withoutRecipeBasket
      ? withRecipeBasket.cost - withoutRecipeBasket.cost
      : null
  const marginalPerPortion = marginalTotal == null ? null : marginalTotal / servings
  const leftoverTotal = marginalTotal == null ? null : marginalTotal - usedTotal

  // What each corrected macro used to say. Edits arrive oldest-first, so the
  // first entry for a field holds the original source value even if it has since
  // been corrected more than once.
  const originalMacros = {}
  for (const edit of recipe.edits ?? []) {
    if (!(edit.field in originalMacros)) originalMacros[edit.field] = edit.old_value
  }

  return (
    <Stack gap="xl">
      <Button
        variant="subtle"
        color="gray"
        size="sm"
        w="fit-content"
        leftSection={<IconArrowLeft size={16} />}
        onClick={() => navigate(-1)}
      >
        Back
      </Button>

      <Grid gutter="xl">
        <Grid.Col span={{ base: 12, sm: 6 }}>
          <Image
            src={recipe.image_url || RECIPE_PLACEHOLDER_IMAGE}
            fallbackSrc={RECIPE_PLACEHOLDER_IMAGE}
            alt={recipe.name}
            radius="md"
            className={classes.hero}
          />
        </Grid.Col>
        <Grid.Col span={{ base: 12, sm: 6 }}>
          <Stack gap="sm" h="100%" justify="center">
            <Group gap="xs">
              {recipe.cuisines.map((c) => (
                <Badge key={c} color="fresh" variant="light" radius="sm">
                  {c}
                </Badge>
              ))}
              {recipe.tags.map((t) => (
                <Badge key={t} color="gray" variant="light" radius="sm">
                  {t}
                </Badge>
              ))}
            </Group>
            <Title order={1} className={classes.title}>
              {recipe.name}
            </Title>
            {recipe.headline && (
              <Text c="dimmed" fz="lg">
                {recipe.headline}
              </Text>
            )}

            <Group gap="sm" mt="xs">
              {recipe.steps.length > 0 && (
                <Button
                  component={Link}
                  /* The week goes with you into the kitchen: the steps are
                     rewritten for whatever swap it holds, and cooking the dish
                     as published when the shop bought something else is exactly
                     the mistake this is here to stop. */
                  to={`/recipes/${recipe.id}/cook?week=${weekStart}`}
                  replace
                  color="fresh"
                  leftSection={<IconChefHat size={17} />}
                >
                  Let's cook!
                </Button>
              )}
              <PlannerControls
                entry={plannerEntry}
                readOnly={weekReadOnly}
                disabled={!plannerEntry && upcomingWeekFull}
                onAdd={() =>
                  addRecipeToWeek(recipe, weekStart, { protein, limit: recipesPerWeek })
                }
                onRemove={() => removeRecipeFromWeek(weekStart, recipe.id)}
                onPortionsChange={(portions) =>
                  setRecipePortions(weekStart, recipe.id, portions)
                }
              />
              {/* Which week this is about — the page is reachable from any of
                  them, and the controls alone do not say. On a finished week it
                  is the more important half: everything below describes how the
                  dish was cooked then, not what it would be now. */}
              <Tooltip
                label={
                  weekReadOnly ? 'Shown as it was cooked this week' : 'The week this adds to'
                }
                withArrow
              >
                <Badge
                  color="gray"
                  variant="light"
                  radius="sm"
                  size="lg"
                  leftSection={
                    weekReadOnly ? <IconLock size={13} /> : <IconCalendarWeek size={13} />
                  }
                >
                  {formatWeekRange(weekStart)}
                </Badge>
              </Tooltip>
              <RecipeOptionsMenu
                pending={hideRecipe.isPending}
                onHide={() => {
                  if (!window.confirm('Hide this recipe from the library?')) return
                  // Hiding takes it out of the week you are planning, not out of
                  // the record of a week you have already cooked.
                  if (!weekReadOnly) removeRecipeFromWeek(weekStart, recipe.id)
                  hideRecipe.mutate(undefined, { onSuccess: () => navigate('/browse') })
                }}
              />
              {marginalPerPortion != null && (
                <Tooltip
                  label={
                    `What this recipe adds to the week's shop, per portion: whole packs, ` +
                    `less anything the rest of the week already buys. The ingredient list ` +
                    `below prices only what the recipe uses, so it comes to less.`
                  }
                  withArrow
                  multiline
                  w={280}
                >
                  <Badge color="fresh" variant="light" radius="sm" size="lg" className={classes.costBadge}>
                    {formatSignedMoney(marginalPerPortion)} pp to shop
                  </Badge>
                </Tooltip>
              )}
            </Group>

            <RecipeRatings recipe={recipe} personalRating={personalRating} wishlist={wishlist} />

            <Group gap="lg" mt="xs" align="flex-start">
              {recipe.total_time_min != null && (
                <Group gap={6} wrap="nowrap">
                  <IconClock size={18} />
                  <Text>{recipe.total_time_min} min</Text>
                </Group>
              )}
              {recipe.difficulty != null && (
                <Group gap={6} wrap="nowrap">
                  <IconChefHat size={18} />
                  <Text>{DIFFICULTY[recipe.difficulty] ?? '—'}</Text>
                </Group>
              )}
              {recipe.base_yield != null && (
                <Group gap={6} wrap="nowrap">
                  <IconUsers size={18} />
                  <Text>Serves {recipe.base_yield}</Text>
                </Group>
              )}
            </Group>
          </Stack>
        </Grid.Col>
      </Grid>

      <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="md">
        <MacroStat
          label="Energy"
          value={view.energy_kcal}
          unit=" kcal"
          corrected={modified ? null : originalMacros.energy_kcal}
        />
        <MacroStat
          label="Protein"
          value={view.protein_g}
          unit="g"
          corrected={modified ? null : originalMacros.protein_g}
        />
        <MacroStat
          label="Carbs"
          value={view.carbs_g}
          unit="g"
          corrected={modified ? null : originalMacros.carbs_g}
        />
        <MacroStat
          label="Fat"
          value={view.fat_g}
          unit="g"
          corrected={modified ? null : originalMacros.fat_g}
        />
      </SimpleGrid>
      <Group justify="space-between" align="center" wrap="nowrap" mt={-12}>
        <Group gap={6} wrap="nowrap">
          <Text size="xs" c="dimmed">
            {modified ? 'Per serving, as modified' : 'Per serving'}
            {!modified && recipe.serving_size_g
              ? ` · ~${Math.round(recipe.serving_size_g)}g`
              : ''}
            {!modified && recipe.protein_energy_ratio != null
              ? ` · ${recipe.protein_energy_ratio}g protein / 100 kcal`
              : ''}
          </Text>
          {!modified && recipe.macros_suspect && (
            <Tooltip
              label="These four numbers don't add up against each other"
              withArrow
              multiline
              w={220}
            >
              <ThemeIcon variant="transparent" color="orange" size="xs">
                <IconAlertTriangle size={14} />
              </ThemeIcon>
            </Tooltip>
          )}
        </Group>
        <MacroMenu audit={audit} revert={revert} hasEdits={(recipe.edits ?? []).length > 0} />
      </Group>

      {personalRating.isError && (
        <Alert color="red" variant="light">
          {personalRating.error?.message ?? "Couldn't save your rating."}
        </Alert>
      )}

      {wishlist.isError && (
        <Alert color="red" variant="light">
          {wishlist.error?.message ?? "Couldn't update your wishlist."}
        </Alert>
      )}

      {hideRecipe.isError && (
        <Alert color="red" variant="light">
          {hideRecipe.error?.message ?? "Couldn't hide this recipe."}
        </Alert>
      )}

      <MacroNotes audit={audit} edits={recipe.edits ?? []} />

      <Divider />

      <Grid gutter="xl">
        <Grid.Col span={{ base: 12, md: 4 }}>
          <Stack gap="md">
            <Title order={3}>Ingredients</Title>
            <div>
              <Text size="sm" fw={600} mb={4}>
                Servings
              </Text>
              <SegmentedControl
                size="xs"
                color="fresh"
                fullWidth
                value={String(servings)}
                onChange={(v) => setServingsOverride(Number(v))}
                data={SERVINGS.map((n) => ({ label: SERVINGS_LABELS[n], value: String(n) }))}
              />
            </div>
            {recipe.protein && (
              <ProteinControls
                profile={recipe.protein}
                modifier={protein}
                preview={proteinPreview}
                baseYield={baseYield}
                onChange={applyProtein}
                readOnly={weekReadOnly}
              />
            )}
            <Table.ScrollContainer minWidth={320}>
              <Table verticalSpacing="xs" className={classes.ingredientsTable}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th />
                    <Table.Th>Qty</Table.Th>
                    <Table.Th>Item</Table.Th>
                    <Table.Th ta="right">Cost</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {view.ingredients.filter(hasDisplayQuantity).map((ing, i) => {
                    const { quantity, estimate } = splitQuantityLabel(scaledQuantity(ing, factor))
                    const cost = ing.ingredient_key ? ingredientCosts.get(ing.ingredient_key) : null
                    const canOpenMapping = Boolean(ing.ingredient_key)
                    return (
                      <Table.Tr
                        key={i}
                        className={canOpenMapping ? classes.ingredientRow : undefined}
                        onClick={() =>
                          canOpenMapping && navigate(`/mapping/${encodeURIComponent(ing.ingredient_key)}`)
                        }
                      >
                        <Table.Td w={42}>
                          {ing.image_url ? (
                            <Image src={ing.image_url} w={34} h={34} radius="sm" className={classes.ingImg} />
                          ) : (
                            <ThemeIcon variant="light" color="gray" size={34} radius="sm" className={classes.ingPlaceholder}>
                              <IconPhoto size={17} />
                            </ThemeIcon>
                          )}
                        </Table.Td>
                        <Table.Td className={classes.quantityCell}>
                          <Text size="sm" fw={600}>{quantity}</Text>
                          {estimate && <Text size="xs" c="dimmed">({estimate})</Text>}
                        </Table.Td>
                        <Table.Td>
                          <Text size="sm" fw={500}>
                            {ing.name}
                            {ing.unmapped && (
                              <Tooltip label="Not mapped to a basket product" withArrow>
                                <ThemeIcon
                                  component="span"
                                  variant="light"
                                  color="yellow"
                                  size="sm"
                                  radius="xl"
                                  className={classes.ingredientWarning}
                                  data-darkreader-ignore="true"
                                  style={{
                                    backgroundColor: 'rgb(255, 243, 191)',
                                    color: 'rgb(124, 77, 0)',
                                  }}
                                  aria-label="Unmapped ingredient"
                                >
                                  <IconAlertTriangle size={13} />
                                </ThemeIcon>
                              </Tooltip>
                            )}
                            {estimatedPotent(ing) && (
                              <Tooltip
                                label="Quantity is our estimate - the source ships this pre-portioned and states no weight. Easy to overdo, so add to taste."
                                withArrow
                                multiline
                                w={260}
                              >
                                <ThemeIcon
                                  component="span"
                                  variant="light"
                                  color="orange"
                                  size="sm"
                                  radius="xl"
                                  className={classes.ingredientWarning}
                                  aria-label="Estimated quantity, easy to overdo"
                                >
                                  <IconFlame size={13} />
                                </ThemeIcon>
                              </Tooltip>
                            )}
                          </Text>
                        </Table.Td>
                        <Table.Td ta="right">
                          <Text size="xs" c="dimmed" fw={600}>
                            {cost == null ? '-' : formatMoney(cost)}
                          </Text>
                        </Table.Td>
                      </Table.Tr>
                    )
                  })}
                </Table.Tbody>
                {marginalTotal != null && (
                  <Table.Tfoot>
                    <Table.Tr>
                      <Table.Td colSpan={3}>
                        <Text size="xs" c="dimmed" fw={600}>
                          Used by this recipe
                        </Text>
                      </Table.Td>
                      <Table.Td ta="right">
                        <Text size="xs" c="dimmed" fw={700}>{formatMoney(usedTotal)}</Text>
                      </Table.Td>
                    </Table.Tr>
                    {leftoverTotal > 0.005 && (
                      <Table.Tr>
                        <Table.Td colSpan={3}>
                          <Group gap={6} wrap="nowrap">
                            <Text size="xs" c="dimmed" fw={600}>
                              Left over in the packs
                            </Text>
                            <Tooltip
                              label="Packs come in fixed sizes, so a recipe needing 5 g of something still buys the whole bag. This is what stays in the cupboard for next time."
                              withArrow
                              multiline
                              w={260}
                            >
                              <ThemeIcon variant="transparent" color="gray" size="xs">
                                <IconAlertTriangle size={13} />
                              </ThemeIcon>
                            </Tooltip>
                          </Group>
                        </Table.Td>
                        <Table.Td ta="right">
                          <Text size="xs" c="dimmed" fw={700}>{formatMoney(leftoverTotal)}</Text>
                        </Table.Td>
                      </Table.Tr>
                    )}
                    <Table.Tr>
                      <Table.Td colSpan={3}>
                        <Text size="sm" fw={700}>Added to the shop</Text>
                      </Table.Td>
                      <Table.Td ta="right">
                        <Text size="sm" fw={700}>{formatMoney(marginalTotal)}</Text>
                      </Table.Td>
                    </Table.Tr>
                  </Table.Tfoot>
                )}
              </Table>
            </Table.ScrollContainer>

            {recipe.allergens.length > 0 && (
              <>
                <Divider mt="sm" />
                <div>
                  <Text size="sm" fw={700} mb={6}>
                    Allergens
                  </Text>
                  <Group gap={6}>
                    {recipe.allergens.map((a) => (
                      <Badge key={a} variant="outline" color="gray" radius="sm" size="sm">
                        {a}
                      </Badge>
                    ))}
                  </Group>
                </div>
              </>
            )}
          </Stack>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 8 }}>
          <Stack gap="md">
            <Title order={3}>Method</Title>
            <Stack gap="lg">
              {view.steps.map((step) => (
                <Group key={step.index} gap="md" align="flex-start" wrap="nowrap">
                  <ThemeIcon color="fresh" radius="xl" size={30} variant="filled">
                    {step.index}
                  </ThemeIcon>
                  <Text className={classes.step}>{step.text}</Text>
                </Group>
              ))}
            </Stack>
          </Stack>
        </Grid.Col>
      </Grid>

      {recipe.source_url && (
        <>
          <Divider />
          <Anchor href={recipe.source_url} target="_blank" c="dimmed" size="sm">
            <Group gap={6} wrap="nowrap">
              <IconExternalLink size={14} />
              View original on HelloFresh
            </Group>
          </Anchor>
        </>
      )}
    </Stack>
  )
}
