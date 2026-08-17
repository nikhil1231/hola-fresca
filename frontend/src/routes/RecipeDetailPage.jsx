import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Collapse,
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
  Stack,
  Table,
  Text,
  ThemeIcon,
  Title,
  Tooltip,
  UnstyledButton,
} from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'
import {
  IconAlertTriangle,
  IconArrowBackUp,
  IconArrowLeft,
  IconCalendarWeek,
  IconChefHat,
  IconChevronDown,
  IconClock,
  IconDots,
  IconExternalLink,
  IconFlag,
  IconHeart,
  IconHeartFilled,
  IconEyeOff,
  IconFlame,
  IconLock,
  IconMinus,
  IconPhoto,
  IconPlus,
  IconStar,
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
  useRecipeLibrary,
  useRecipeWishlist,
  useRevertRecipeEdits,
} from '../hooks/useRecipeQueries.js'
import { useCanEditCatalogue } from '../hooks/useAccount.js'
import { usePreloadStepImages } from '../hooks/usePreloadStepImages.js'
import {
  formatWeekRange,
  formatWeekStart,
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
import {
  estimatedPotent,
  hasDisplayQuantity,
  scaledQuantity,
  splitQuantityLabel,
} from '../utils/ingredientQuantity.js'
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

// Whether this recipe is one of ours, from a payload that might not say.
//
// Only an explicit `false` means it is not. Absence has to read as *yes*, and it
// is worth being blunt about why: everything the page can do — cook, plan, rate,
// wishlist — hangs off this, so reading a missing field as "no" empties the page
// of every action for every recipe. That is exactly what a backend serving the
// schema from before `in_library` existed produces, and the honest behaviour
// against one is the old behaviour, not a dead page. It also matches the field's
// own default on `RecipeDetail`.
function inLibrary(recipe) {
  return recipe?.in_library !== false
}

function formatMoney(value) {
  return money.format(value ?? 0)
}

function formatSignedMoney(value) {
  if (value == null) return null
  const absolute = Math.abs(value)
  return `${value < 0 ? '-' : '+'}${formatMoney(absolute)}`
}

const DIFFICULTY = { 1: 'Easy', 2: 'Medium', 3: 'Hard' }
const SERVINGS = [0.5, 1, 2, 3, 4, 5, 6, 8]
const SERVINGS_LABELS = { 0.5: '½', 1: '1', 2: '2', 3: '3', 4: '4', 6: '6', 8: '8' }

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

// `compact` is the desktop hero's variant: a plain Mantine button that inherits
// the same default radius, height and weight as the "Let's cook!" beside it.
// The two used to disagree on all three, because this reused `.secondaryCta` —
// a class built for the mobile CTA grid, where the button is a big two-line
// block. Dressing that down with overrides is what left a heavy pill next to a
// light rounded rectangle; not wearing it at all is simpler and matches.
function PlannerControls({
  disabled,
  entry,
  readOnly = false,
  onAdd,
  onPortionsChange,
  onRemove,
  marginalPerPortion,
  compact = false,
}) {
  if (readOnly) {
    if (!entry) return null
    return (
      <Badge color="gray" variant="light" radius="sm" size="lg" leftSection={<IconUsers size={13} />}>
        {entry.portions} portions
      </Badge>
    )
  }
  if (!entry) {
    if (compact) {
      return (
        <Button
          variant="outline"
          color="fresh"
          disabled={disabled}
          onClick={() => !disabled && onAdd?.()}
        >
          Add to this week
        </Button>
      )
    }
    return (
      <Button
        variant="outline"
        color="fresh"
        radius="xl"
        className={classes.secondaryCta}
        disabled={disabled}
        onClick={() => !disabled && onAdd?.()}
      >
        <span>
          Add to this week
          <small>{marginalPerPortion != null ? `${formatSignedMoney(marginalPerPortion)} pp` : 'Price pending'}</small>
        </span>
      </Button>
    )
  }

  const portions = entry.portions
  return (
    <Group
      gap="xs"
      wrap="nowrap"
      className={compact ? classes.plannedControlsCompact : classes.plannedControls}
    >
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
  const [selection, setSelection] = useState(value)

  useEffect(() => {
    setSelection(value)
  }, [value])

  return (
    <Group
      gap={3}
      wrap="nowrap"
      role="radiogroup"
      aria-label="Personal rating"
      aria-busy={pending || undefined}
    >
      {Array.from({ length: 5 }).map((_, index) => {
        const starValue = index + 1
        const active = starValue <= (selection ?? 0)
        return (
          <ActionIcon
            key={starValue}
            type="button"
            variant="transparent"
            color="gray"
            size="md"
            radius="xl"
            data-darkreader-ignore="true"
            className={active ? classes.personalStarActive : classes.personalStar}
            aria-label={
              selection === starValue
                ? `Clear ${starValue} star personal rating`
                : `Set ${starValue} star personal rating`
            }
            aria-checked={selection === starValue}
            role="radio"
            onClick={() => {
              const nextValue = selection === starValue ? null : starValue
              setSelection(nextValue)
              onSet(nextValue)
            }}
          >
            {active ? <IconStarFilled size={18} /> : <IconStar size={18} />}
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
        aria-busy={pending || undefined}
        aria-label={label}
        aria-pressed={wishlisted}
        onClick={() => onToggle(!wishlisted)}
      >
        {wishlisted ? <IconHeartFilled size={19} /> : <IconHeart size={19} />}
      </ActionIcon>
    </Tooltip>
  )
}

function RecipeOptionsMenu({
  hidePending,
  onHide,
  audit,
  revert,
  hasEdits,
  inLibrary = true,
  curated = true,
  library,
  isAdmin = false,
}) {
  const pending = hidePending || audit.running || revert.isPending || library?.isPending
  // Only offered for a recipe somebody added by hand. Withdrawing one the rules
  // curated in is a different judgement — that the source row is broken — and it
  // has its own flag; the menu must not blur the two into one item.
  const canWithdraw = isAdmin && inLibrary && !curated

  return (
    <Menu shadow="md" position="bottom-end" withinPortal>
      <Menu.Target>
        <ActionIcon variant="subtle" color="gray" size="lg" aria-label="Recipe options">
          {pending ? <Loader size={14} /> : <IconDots size={18} />}
        </ActionIcon>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Nutrition data</Menu.Label>
        <Menu.Item
          leftSection={<IconFlag size={14} />}
          onClick={audit.run}
          disabled={audit.running}
        >
          {hasEdits ? 'Re-check nutrition numbers' : 'Report wrong nutrition numbers'}
        </Menu.Item>
        {hasEdits && (
          <Menu.Item
            leftSection={<IconArrowBackUp size={14} />}
            onClick={() => revert.mutate()}
            disabled={revert.isPending}
          >
            Restore original nutrition numbers
          </Menu.Item>
        )}
        <Menu.Divider />
        <Menu.Label>Recipe</Menu.Label>
        {canWithdraw && (
          <Menu.Item
            leftSection={<IconMinus size={14} />}
            onClick={() => library.mutate(false)}
            disabled={library.isPending}
          >
            Remove from library
          </Menu.Item>
        )}
        <Menu.Item color="red" leftSection={<IconEyeOff size={14} />} onClick={onHide} disabled={hidePending}>
          Hide from library
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  )
}

function MacroStat({ label, value, unit, corrected }) {
  return (
    <Paper withBorder radius="md" p="sm" className={classes.macro}>
      <Text className={classes.macroValue}>
        <span className={classes.macroDot} style={{ '--macro-color': MACRO_COLORS[label] }} />
        {value == null ? '—' : `${Math.round(value)}`}
        <Text span fz="sm" c="dimmed" fw={500} className={classes.macroUnit}>
          {unit}
        </Text>
      </Text>
      <Group gap={6} justify="center" wrap="nowrap">
        <Text className={classes.macroLabel}>
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

function HeroTag({ children, tone = 'green' }) {
  return <span className={`${classes.heroTag} ${classes[tone] ?? ''}`}>{children}</span>
}

function RatingSummary({ recipe }) {
  if (recipe.avg_rating == null) return null

  return (
    <Group gap={8} wrap="nowrap" className={classes.heroRating}>
      <IconStarFilled size={17} className={classes.star} />
      <Text className={classes.ratingValue}>{recipe.avg_rating.toFixed(1)}</Text>
      {recipe.ratings_count != null && (
        <Text className={classes.ratingCount}>
          ({recipe.ratings_count.toLocaleString()})
        </Text>
      )}
    </Group>
  )
}

function MacroPanel({ view, modified, recipe, originalMacros }) {
  const proteinDensity = view.energy_kcal > 0 && view.protein_g != null
    ? ((view.protein_g / view.energy_kcal) * 100).toFixed(1)
    : recipe.protein_energy_ratio

  return (
    <Paper withBorder className={classes.macroPanel}>
      <SimpleGrid cols={4} spacing={0} className={classes.macroGrid}>
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
      <Group justify="flex-start" align="center" wrap="nowrap" className={classes.macroFooter}>
        <Group gap={6} wrap="nowrap">
          <Text className={classes.macroCaption}>
            {modified ? 'Per serving, as modified' : 'Per serving'}
            {!modified && recipe.serving_size_g ? ` · ~${Math.round(recipe.serving_size_g)}g` : ''}
            {proteinDensity != null
              ? ` · ${proteinDensity}g protein / 100 kcal`
              : ''}
          </Text>
          {!modified && recipe.macros_suspect && (
            <Tooltip label="These four numbers don't add up against each other" withArrow multiline w={220}>
              <ThemeIcon variant="transparent" color="orange" size="xs">
                <IconAlertTriangle size={14} />
              </ThemeIcon>
            </Tooltip>
          )}
        </Group>
      </Group>
    </Paper>
  )
}

function RateRecipePanel({ recipe, personalRating }) {
  return (
    <Paper withBorder className={classes.ratePanel}>
      <Text className={classes.rateLabel}>Rate this recipe</Text>
      <PersonalRatingControl
        value={recipe.personal_rating}
        pending={personalRating.isPending}
        onSet={(rating) => personalRating.mutate(rating)}
      />
    </Paper>
  )
}

function DetailControls({ servings, setServingsOverride, recipe, protein, proteinPreview, baseYield, applyProtein }) {
  const servingOptions = Array.from(new Set([...SERVINGS, servings]))
    .sort((first, second) => first - second)
    .map((value) => ({ label: SERVINGS_LABELS[value] ?? String(value), value: String(value) }))

  return (
    <Paper withBorder className={classes.detailControls}>
      <div className={classes.servingControl}>
        <Group justify="space-between" align="baseline" mb={10}>
          <Text className={classes.panelLabel}>
            Servings
          </Text>
          <Text size="sm" c="dimmed">Updates ingredients and costs</Text>
        </Group>
        <SegmentedControl
          size="sm"
          color="fresh"
          fullWidth
          value={String(servings)}
          onChange={(v) => setServingsOverride(Number(v))}
          data={servingOptions}
        />
      </div>
      {recipe.protein && (
        <ProteinControls
          profile={recipe.protein}
          modifier={protein}
          preview={proteinPreview}
          baseYield={baseYield}
          onChange={applyProtein}
        />
      )}
    </Paper>
  )
}

function IngredientList({
  ingredients,
  factor,
  ingredientCosts,
  navigate,
  expanded,
  setExpanded,
  opened,
  onToggle,
  marginalTotal,
  usedTotal,
  leftoverTotal,
}) {
  const sortedIngredients = ingredients
    .map((ingredient, index) => ({
      ingredient,
      index,
      cost: ingredient.ingredient_key ? ingredientCosts.get(ingredient.ingredient_key) : null,
    }))
    .sort((first, second) => (
      (second.cost ?? Number.NEGATIVE_INFINITY) - (first.cost ?? Number.NEGATIVE_INFINITY)
      || first.index - second.index
    ))
  const visible = expanded ? sortedIngredients : sortedIngredients.slice(0, 4)
  const hiddenCount = Math.max(sortedIngredients.length - visible.length, 0)

  return (
    <section className={classes.recipeSection}>
      <UnstyledButton className={classes.collapsibleHeader} onClick={onToggle} aria-expanded={opened}>
        <Title order={2} className={classes.sectionHeading}>
          Ingredients <span>{ingredients.length} items</span>
        </Title>
        <IconChevronDown
          size={20}
          className={`${classes.sectionChevron} ${opened ? classes.sectionChevronOpen : ''}`}
        />
      </UnstyledButton>
      <Collapse expanded={opened}>
        <div className={classes.ingredientList}>
        {visible.map(({ ingredient: ing, cost, index }) => {
          const { quantity, estimate } = splitQuantityLabel(scaledQuantity(ing, factor))
          const canOpenMapping = Boolean(ing.ingredient_key)
          return (
            <UnstyledButton
              key={`${ing.name}-${index}`}
              className={classes.ingredientItem}
              disabled={!canOpenMapping}
              onClick={() =>
                canOpenMapping && navigate(`/mapping/${encodeURIComponent(ing.ingredient_key)}`)
              }
            >
              <span className={classes.ingredientImage} aria-hidden="true">
                {ing.image_url ? <Image src={ing.image_url} alt="" loading="lazy" /> : null}
              </span>
              <span className={classes.ingredientName}>
                {ing.name}
                {ing.unmapped && <span className={classes.inlineWarning}> unmapped</span>}
                {estimatedPotent(ing) && <span className={classes.inlineWarning}> estimate</span>}
              </span>
              <strong>
                {quantity}
                {estimate ? ` (${estimate})` : ''}
              </strong>
              {cost != null && <small>{formatMoney(cost)}</small>}
            </UnstyledButton>
          )
        })}
        {hiddenCount > 0 && (
          <UnstyledButton className={classes.showMoreButton} onClick={() => setExpanded(true)}>
            Show {hiddenCount} more
          </UnstyledButton>
        )}
        {expanded && sortedIngredients.length > 4 && (
          <UnstyledButton className={classes.showMoreButton} onClick={() => setExpanded(false)}>
            Show less
          </UnstyledButton>
        )}
        </div>
        {marginalTotal != null && (
          <div className={classes.recipeTotals}>
            <span>Used by this recipe <strong>{formatMoney(usedTotal)}</strong></span>
            {leftoverTotal > 0.005 && (
              <span>Left over in packs <strong>{formatMoney(leftoverTotal)}</strong></span>
            )}
            <span>Added to the shop <strong>{formatMoney(marginalTotal)}</strong></span>
          </div>
        )}
      </Collapse>
    </section>
  )
}

function CollapsibleRecipeSection({ title, meta, opened, onToggle, children }) {
  return (
    <section className={classes.recipeSection}>
      <UnstyledButton className={classes.collapsibleHeader} onClick={onToggle} aria-expanded={opened}>
        <Title order={2} className={classes.sectionHeading}>
          {title} {meta && <span>{meta}</span>}
        </Title>
        <IconChevronDown
          size={20}
          className={`${classes.sectionChevron} ${opened ? classes.sectionChevronOpen : ''}`}
        />
      </UnstyledButton>
      <Collapse expanded={opened}>
        <div className={classes.collapsibleBody}>{children}</div>
      </Collapse>
    </section>
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

// Source rating and your own, on one line.
//
// This was a bordered panel of three labelled columns, one of which held the
// wishlist button — a control filed under a heading as though it were a reading.
// The button belongs with the other controls, and what is left is two facts that
// fit on a line and no longer need a box drawn round them.
function DesktopRecipeRatings({ recipe, personalRating, wishlist }) {
  return (
    <Group gap="lg" align="center" wrap="wrap" className={classes.desktopRatingsRow}>
      {recipe.avg_rating != null ? (
        <Group gap={6} wrap="nowrap">
          <IconStarFilled size={16} className={classes.star} />
          <Text fw={700} size="sm">{recipe.avg_rating.toFixed(1)}</Text>
          {recipe.ratings_count != null && (
            <Text c="dimmed" size="sm">({recipe.ratings_count.toLocaleString()})</Text>
          )}
        </Group>
      ) : (
        <Text size="sm" c="dimmed">Not rated</Text>
      )}

      {/* The label is gone because the control already says it: filled stars are
          the source's, hollow ones are waiting for yours, and they sit next to
          each other. The wishlist joins them — all three are what *you* think of
          the recipe, as against the facts about it on the line above. */}
      <PersonalRatingControl
        value={recipe.personal_rating}
        pending={personalRating.isPending}
        onSet={(rating) => personalRating.mutate(rating)}
      />

      <WishlistButton
        wishlisted={recipe.wishlisted}
        pending={wishlist.isPending}
        onToggle={(wishlisted) => wishlist.mutate(wishlisted)}
      />
    </Group>
  )
}

// The facts about the recipe, as one dimmed line.
//
// Time, difficulty and servings were already here; the week and the marginal
// cost arrived as `Badge`s in the row of buttons above, where they read as
// controls that did not respond to being clicked. They say something rather than
// do something, so they say it here.
function DesktopHeroMeta({ recipe, weekStart, weekReadOnly, marginalPerPortion }) {
  const facts = []
  if (recipe.total_time_min != null) {
    facts.push({ key: 'time', icon: <IconClock size={16} />, label: `${recipe.total_time_min} min` })
  }
  if (recipe.base_yield != null) {
    facts.push({ key: 'yield', icon: <IconUsers size={16} />, label: `Serves ${recipe.base_yield}` })
  }
  facts.push({
    key: 'week',
    icon: weekReadOnly ? <IconLock size={15} /> : <IconCalendarWeek size={15} />,
    // The start date names the week on its own — every week is seven days from
    // a Monday, so spelling out the end was six words to say nothing.
    label: formatWeekStart(weekStart),
    hint: weekReadOnly ? 'Shown as it was cooked this week' : 'The week this adds to',
  })
  if (marginalPerPortion != null) {
    facts.push({
      key: 'cost',
      label: `${formatSignedMoney(marginalPerPortion)} pp`,
      hint: "What this recipe adds to the week's shop per portion.",
      strong: true,
    })
  }

  return (
    <Group gap="md" align="center" wrap="wrap" className={classes.desktopHeroMeta}>
      {facts.map((fact) => {
        const body = (
          <Group gap={6} wrap="nowrap">
            {fact.icon}
            <Text size="sm" c={fact.strong ? 'fresh.4' : undefined} fw={fact.strong ? 700 : undefined}>
              {fact.label}
            </Text>
          </Group>
        )
        return fact.hint ? (
          <Tooltip key={fact.key} label={fact.hint} withArrow>
            {body}
          </Tooltip>
        ) : (
          <span key={fact.key}>{body}</span>
        )
      })}
    </Group>
  )
}

// What to do about a recipe the curation rules cut.
//
// It stands where the cook and plan buttons stand for a library recipe, because
// it is the same slot in the same decision — there is simply one thing to settle
// first. Admitting it is a shared act, so a non-admin is told what the state is
// rather than given a button that would be refused.
function LibraryNotice({ recipe, library, isAdmin }) {
  if (inLibrary(recipe)) return null
  return (
    <Alert
      color="orange"
      variant="light"
      radius="md"
      icon={<IconAlertTriangle size={18} />}
      className={classes.libraryNotice}
    >
      <Group justify="space-between" align="center" wrap="wrap" gap="sm">
        <Text size="sm">
          Not in your library, so it can't be planned, priced or rated yet.
        </Text>
        {isAdmin && (
          <Button
            size="xs"
            color="fresh"
            leftSection={<IconPlus size={15} />}
            loading={library.isPending}
            onClick={() => library.mutate(true)}
          >
            Add to library
          </Button>
        )}
      </Group>
    </Alert>
  )
}

function DesktopRecipeDetail({
  recipe,
  view,
  modified,
  originalMacros,
  heroUrl,
  setSettledHeroUrl,
  navigate,
  weekStart,
  weekReadOnly,
  plannerEntry,
  upcomingWeekFull,
  addRecipeToWeek,
  removeRecipeFromWeek,
  setRecipePortions,
  recipesPerWeek,
  protein,
  marginalPerPortion,
  personalRating,
  wishlist,
  hideRecipe,
  library,
  isAdmin,
  audit,
  revert,
  servings,
  setServingsOverride,
  proteinPreview,
  baseYield,
  applyProtein,
  factor,
  ingredientCosts,
  marginalTotal,
  usedTotal,
  leftoverTotal,
}) {
  const sortedIngredients = view.ingredients
    .filter(hasDisplayQuantity)
    .map((ingredient, index) => ({
      ingredient,
      index,
      cost: ingredient.ingredient_key ? ingredientCosts.get(ingredient.ingredient_key) : null,
    }))
    .sort((first, second) => (
      (second.cost ?? Number.NEGATIVE_INFINITY) - (first.cost ?? Number.NEGATIVE_INFINITY)
      || first.index - second.index
    ))

  const hide = () => {
    if (!window.confirm('Hide this recipe from the library?')) return
    if (!weekReadOnly) removeRecipeFromWeek(weekStart, recipe.id)
    hideRecipe.mutate(undefined, { onSuccess: () => navigate('/browse') })
  }

  return (
    <main className={classes.desktopDetailPage}>
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

        <Grid gutter={48} align="stretch">
          <Grid.Col span={6}>
            <Image
              src={heroUrl}
              fallbackSrc={RECIPE_PLACEHOLDER_IMAGE}
              alt={recipe.name}
              radius="md"
              className={classes.desktopHero}
              loading="eager"
              fetchPriority="high"
              onLoad={() => setSettledHeroUrl(heroUrl)}
              onError={() => setSettledHeroUrl(heroUrl)}
            />
          </Grid.Col>
          <Grid.Col span={6}>
            <Stack gap="md" h="100%" justify="center" pl={32}>
              <Group gap="xs">
                {recipe.cuisines.map((cuisine) => (
                  <Badge key={cuisine} color="fresh" variant="light" radius="sm">{cuisine}</Badge>
                ))}
                {recipe.tags.map((tag) => (
                  <Badge key={tag} color="gray" variant="light" radius="sm">{tag}</Badge>
                ))}
              </Group>

              {/* The menu rides the title's first line. It acts on the whole
                  recipe, so it belongs where the recipe is named rather than
                  queued behind the buttons, where it read as a third action. */}
              <Group gap="sm" align="flex-start" wrap="nowrap" className={classes.desktopTitleRow}>
                <Title order={1} className={classes.desktopTitle}>{recipe.name}</Title>
                <RecipeOptionsMenu
                  hidePending={hideRecipe.isPending}
                  onHide={hide}
                  audit={audit}
                  revert={revert}
                  hasEdits={(recipe.edits ?? []).length > 0}
                  inLibrary={inLibrary(recipe)}
                  curated={recipe.curated}
                  library={library}
                  isAdmin={isAdmin}
                />
              </Group>
              {recipe.headline && <Text c="dimmed" fz="lg">{recipe.headline}</Text>}

              <LibraryNotice recipe={recipe} library={library} isAdmin={isAdmin} />

              {/* The two things worth doing, and nothing else. The week and the
                  marginal cost used to sit here as badges that looked like
                  buttons and did nothing; they are facts, so they are on the
                  meta line below. */}
              {inLibrary(recipe) && (
                <Group gap="md" mt="md" wrap="nowrap" className={classes.desktopHeroActions}>
                  {recipe.steps.length > 0 && (
                    <Button
                      component={Link}
                      to={`/recipes/${recipe.id}/cook${weekStart ? `?week=${weekStart}` : ''}`}
                      replace
                      color="fresh"
                      leftSection={<IconChefHat size={17} />}
                    >
                      Let's cook!
                    </Button>
                  )}
                  <PlannerControls
                    compact
                    entry={plannerEntry}
                    readOnly={weekReadOnly}
                    disabled={!plannerEntry && upcomingWeekFull}
                    marginalPerPortion={marginalPerPortion}
                    onAdd={() => addRecipeToWeek(recipe, weekStart, { protein, limit: recipesPerWeek })}
                    onRemove={() => removeRecipeFromWeek(weekStart, recipe.id)}
                    onPortionsChange={(portions) => setRecipePortions(weekStart, recipe.id, portions)}
                  />
                </Group>
              )}

              <DesktopHeroMeta
                recipe={recipe}
                weekStart={weekStart}
                weekReadOnly={weekReadOnly}
                marginalPerPortion={marginalPerPortion}
              />

              {inLibrary(recipe) && (
                <DesktopRecipeRatings
                  recipe={recipe}
                  personalRating={personalRating}
                  wishlist={wishlist}
                />
              )}
            </Stack>
          </Grid.Col>
        </Grid>

        <SimpleGrid cols={4} spacing="md" className={classes.desktopMacroGrid}>
          <MacroStat label="Energy" value={view.energy_kcal} unit=" kcal" corrected={modified ? null : originalMacros.energy_kcal} />
          <MacroStat label="Protein" value={view.protein_g} unit="g" corrected={modified ? null : originalMacros.protein_g} />
          <MacroStat label="Carbs" value={view.carbs_g} unit="g" corrected={modified ? null : originalMacros.carbs_g} />
          <MacroStat label="Fat" value={view.fat_g} unit="g" corrected={modified ? null : originalMacros.fat_g} />
        </SimpleGrid>

        <Group justify="space-between" align="center" wrap="nowrap" mt={-12}>
          <Text size="xs" c="dimmed">
            {modified ? 'Per serving, as modified' : 'Per serving'}
            {!modified && recipe.serving_size_g ? ` · ~${Math.round(recipe.serving_size_g)}g` : ''}
            {view.energy_kcal > 0 && view.protein_g != null
              ? ` · ${((view.protein_g / view.energy_kcal) * 100).toFixed(1)}g protein / 100 kcal`
              : ''}
          </Text>
        </Group>

        {personalRating.isError && (
          <Alert color="red" variant="light">{personalRating.error?.message ?? "Couldn't save your rating."}</Alert>
        )}
        {wishlist.isError && (
          <Alert color="red" variant="light">{wishlist.error?.message ?? "Couldn't update your wishlist."}</Alert>
        )}
        {hideRecipe.isError && (
          <Alert color="red" variant="light">{hideRecipe.error?.message ?? "Couldn't hide this recipe."}</Alert>
        )}
        <MacroNotes audit={audit} edits={recipe.edits ?? []} />

        <Divider />

        <Grid gutter="xl">
          <Grid.Col span={4}>
            <Stack gap="md">
              <Title order={3}>Ingredients</Title>
              <DetailControls
                servings={servings}
                setServingsOverride={setServingsOverride}
                recipe={recipe}
                protein={protein}
                proteinPreview={proteinPreview}
                baseYield={baseYield}
                applyProtein={applyProtein}
              />

              <Table.ScrollContainer minWidth={320}>
                <Table verticalSpacing="xs" className={classes.desktopIngredientsTable}>
                  <Table.Thead>
                    <Table.Tr>
                      <Table.Th />
                      <Table.Th>Qty</Table.Th>
                      <Table.Th>Item</Table.Th>
                      <Table.Th ta="right">Cost</Table.Th>
                    </Table.Tr>
                  </Table.Thead>
                  <Table.Tbody>
                    {sortedIngredients.map(({ ingredient, index, cost }) => {
                      const { quantity, estimate } = splitQuantityLabel(scaledQuantity(ingredient, factor))
                      const canOpenMapping = Boolean(ingredient.ingredient_key)
                      return (
                        <Table.Tr
                          key={`${ingredient.name}-${index}`}
                          className={canOpenMapping ? classes.desktopIngredientRow : undefined}
                          onClick={() => canOpenMapping && navigate(`/mapping/${encodeURIComponent(ingredient.ingredient_key)}`)}
                        >
                          <Table.Td w={42}>
                            {ingredient.image_url ? (
                              <Image src={ingredient.image_url} w={34} h={34} radius="sm" className={classes.desktopIngredientImage} />
                            ) : (
                              <ThemeIcon variant="light" color="gray" size={34} radius="sm">
                                <IconPhoto size={17} />
                              </ThemeIcon>
                            )}
                          </Table.Td>
                          <Table.Td className={classes.desktopQuantityCell}>
                            <Text size="sm" fw={600}>{quantity}</Text>
                            {estimate && <Text size="xs" c="dimmed">({estimate})</Text>}
                          </Table.Td>
                          <Table.Td>
                            <Text size="sm" fw={500}>
                              {ingredient.name}
                              {ingredient.unmapped && (
                                <Tooltip label="Not mapped to a basket product" withArrow>
                                  <ThemeIcon component="span" variant="light" color="yellow" size="sm" radius="xl" className={classes.desktopIngredientWarning}>
                                    <IconAlertTriangle size={13} />
                                  </ThemeIcon>
                                </Tooltip>
                              )}
                              {estimatedPotent(ingredient) && (
                                <Tooltip label="Estimated quantity; add to taste." withArrow>
                                  <ThemeIcon component="span" variant="light" color="orange" size="sm" radius="xl" className={classes.desktopIngredientWarning}>
                                    <IconFlame size={13} />
                                  </ThemeIcon>
                                </Tooltip>
                              )}
                            </Text>
                          </Table.Td>
                          <Table.Td ta="right">
                            <Text size="xs" c="dimmed" fw={600}>{cost == null ? '—' : formatMoney(cost)}</Text>
                          </Table.Td>
                        </Table.Tr>
                      )
                    })}
                  </Table.Tbody>
                  {marginalTotal != null && (
                    <Table.Tfoot>
                      <Table.Tr>
                        <Table.Td colSpan={3}><Text size="xs" c="dimmed" fw={600}>Used by this recipe</Text></Table.Td>
                        <Table.Td ta="right"><Text size="xs" c="dimmed" fw={700}>{formatMoney(usedTotal)}</Text></Table.Td>
                      </Table.Tr>
                      {leftoverTotal > 0.005 && (
                        <Table.Tr>
                          <Table.Td colSpan={3}><Text size="xs" c="dimmed" fw={600}>Left over in the packs</Text></Table.Td>
                          <Table.Td ta="right"><Text size="xs" c="dimmed" fw={700}>{formatMoney(leftoverTotal)}</Text></Table.Td>
                        </Table.Tr>
                      )}
                      <Table.Tr>
                        <Table.Td colSpan={3}><Text size="sm" fw={700}>Added to the shop</Text></Table.Td>
                        <Table.Td ta="right"><Text size="sm" fw={700}>{formatMoney(marginalTotal)}</Text></Table.Td>
                      </Table.Tr>
                    </Table.Tfoot>
                  )}
                </Table>
              </Table.ScrollContainer>

              {recipe.allergens.length > 0 && (
                <>
                  <Divider mt="sm" />
                  <div>
                    <Text size="sm" fw={700} mb={6}>Allergens</Text>
                    <Group gap={6}>
                      {recipe.allergens.map((allergen) => (
                        <Badge key={allergen} variant="outline" color="gray" radius="sm" size="sm">{allergen}</Badge>
                      ))}
                    </Group>
                  </div>
                </>
              )}
            </Stack>
          </Grid.Col>

          <Grid.Col span={8}>
            <Stack gap="md">
              <Title order={3}>Method</Title>
              <Stack gap="lg">
                {view.steps.map((step) => (
                  <Group key={step.index} gap="md" align="flex-start" wrap="nowrap">
                    <ThemeIcon color="fresh" radius="xl" size={30} variant="filled">{step.index}</ThemeIcon>
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
    </main>
  )
}

export default function RecipeDetailPage() {
  const { id } = useParams()
  const recipeId = Number(id)
  const navigate = useNavigate()
  const desktopLayout = useMediaQuery('(min-width: 60em)')
  const [searchParams] = useSearchParams()
  const { data: recipe, isLoading, isError } = useRecipe(id)
  const [servingsOverride, setServingsOverride] = useState(null)
  const [ingredientsOpen, setIngredientsOpen] = useState(true)
  const [ingredientsExpanded, setIngredientsExpanded] = useState(false)
  const [methodOpen, setMethodOpen] = useState(false)
  const [adjustmentsOpen, setAdjustmentsOpen] = useState(false)
  const [settledHeroUrl, setSettledHeroUrl] = useState(null)
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
  const library = useRecipeLibrary(id)
  // Admitting a recipe is a catalogue write, so it is offered on the same terms
  // as the mapping tools: presentation only, with `require_admin` doing the
  // actual refusing.
  const { allowed: isAdmin } = useCanEditCatalogue()
  const heroUrl = recipe?.image_url || RECIPE_PLACEHOLDER_IMAGE
  usePreloadStepImages(recipe?.steps, { enabled: settledHeroUrl === heroUrl })

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

  const displayIngredients = view.ingredients.filter(hasDisplayQuantity)
  const primaryTags = [...recipe.cuisines, ...recipe.tags].slice(0, 2)

  if (desktopLayout) {
    return (
      <DesktopRecipeDetail
        recipe={recipe}
        view={view}
        modified={modified}
        originalMacros={originalMacros}
        heroUrl={heroUrl}
        setSettledHeroUrl={setSettledHeroUrl}
        navigate={navigate}
        weekStart={weekStart}
        weekReadOnly={weekReadOnly}
        plannerEntry={plannerEntry}
        upcomingWeekFull={upcomingWeekFull}
        addRecipeToWeek={addRecipeToWeek}
        removeRecipeFromWeek={removeRecipeFromWeek}
        setRecipePortions={setRecipePortions}
        recipesPerWeek={recipesPerWeek}
        protein={protein}
        marginalPerPortion={marginalPerPortion}
        personalRating={personalRating}
        wishlist={wishlist}
        hideRecipe={hideRecipe}
        library={library}
        isAdmin={isAdmin}
        audit={audit}
        revert={revert}
        servings={servings}
        setServingsOverride={setServingsOverride}
        proteinPreview={proteinPreview}
        baseYield={baseYield}
        applyProtein={applyProtein}
        factor={factor}
        ingredientCosts={ingredientCosts}
        marginalTotal={marginalTotal}
        usedTotal={usedTotal}
        leftoverTotal={leftoverTotal}
      />
    )
  }

  return (
    <main className={classes.detailPage}>
      <section className={classes.heroShell}>
        <Image
          src={heroUrl}
          fallbackSrc={RECIPE_PLACEHOLDER_IMAGE}
          alt={recipe.name}
          className={classes.hero}
          loading="eager"
          fetchPriority="high"
          onLoad={() => setSettledHeroUrl(heroUrl)}
          onError={() => setSettledHeroUrl(heroUrl)}
        />
        <div className={classes.heroOverlay} />
        <div className={classes.heroTopControls}>
          <ActionIcon
            variant="filled"
            color="dark"
            radius="xl"
            size={58}
            className={classes.circleButton}
            aria-label="Back"
            onClick={() => navigate(-1)}
          >
            <IconArrowLeft size={26} />
          </ActionIcon>
          <Group gap="md" wrap="nowrap">
            {inLibrary(recipe) && (
              <WishlistButton
                wishlisted={recipe.wishlisted}
                pending={wishlist.isPending}
                onToggle={(wishlisted) => wishlist.mutate(wishlisted)}
              />
            )}
            <RecipeOptionsMenu
              hidePending={hideRecipe.isPending}
              audit={audit}
              revert={revert}
              hasEdits={(recipe.edits ?? []).length > 0}
              inLibrary={inLibrary(recipe)}
              curated={recipe.curated}
              library={library}
              isAdmin={isAdmin}
              onHide={() => {
                if (!window.confirm('Hide this recipe from the library?')) return
                // Hiding takes it out of the week you are planning, not out of
                // the record of a week you have already cooked.
                if (!weekReadOnly) removeRecipeFromWeek(weekStart, recipe.id)
                hideRecipe.mutate(undefined, { onSuccess: () => navigate('/browse') })
              }}
            />
          </Group>
        </div>
        <div className={classes.heroContent}>
          <Group gap="xs" className={classes.heroTags}>
            {primaryTags.map((tag, index) => (
              <HeroTag key={tag} tone={index === 0 ? 'green' : 'gray'}>
                {tag}
              </HeroTag>
            ))}
          </Group>
          <Title order={1} className={classes.title}>{recipe.name}</Title>
        </div>
      </section>

      <section className={classes.contentShell}>
        <div className={classes.overviewGrid}>
          <div className={classes.introPanel}>
            {recipe.headline && (
              <Text className={classes.headline}>
                {recipe.headline.toLowerCase().startsWith('with ') ? recipe.headline : `with ${recipe.headline}`}
              </Text>
            )}

            <Group justify="space-between" align="center" wrap="nowrap" className={classes.metaRow}>
              <Group gap="lg" wrap="wrap" className={classes.metaFacts}>
                {recipe.total_time_min != null && <Text>{recipe.total_time_min} min</Text>}
                {recipe.difficulty != null && <Text>{DIFFICULTY[recipe.difficulty] ?? 'Unknown difficulty'}</Text>}
              </Group>
              <RatingSummary recipe={recipe} />
            </Group>

            <LibraryNotice recipe={recipe} library={library} isAdmin={isAdmin} />

            {inLibrary(recipe) && (
              <div className={classes.ctaGrid}>
                {recipe.steps.length > 0 && (
                  <Button
                    component={Link}
                    to={`/recipes/${recipe.id}/cook${weekStart ? `?week=${weekStart}` : ''}`}
                    replace
                    color="fresh"
                    radius="md"
                    className={classes.primaryCta}
                  >
                    <span>
                      Let's cook
                      <small>{recipe.total_time_min ?? 25} min · step by step</small>
                    </span>
                  </Button>
                )}
                <PlannerControls
                  entry={plannerEntry}
                  readOnly={weekReadOnly}
                  disabled={!plannerEntry && upcomingWeekFull}
                  marginalPerPortion={marginalPerPortion}
                  onAdd={() => addRecipeToWeek(recipe, weekStart, { protein, limit: recipesPerWeek })}
                  onRemove={() => removeRecipeFromWeek(weekStart, recipe.id)}
                  onPortionsChange={(portions) => setRecipePortions(weekStart, recipe.id, portions)}
                />
              </div>
            )}
          </div>

          <MacroPanel
            view={view}
            modified={modified}
            recipe={recipe}
            originalMacros={originalMacros}
          />

          <aside className={classes.sideStack}>
            {inLibrary(recipe) && (
              <RateRecipePanel recipe={recipe} personalRating={personalRating} />
            )}
            <div className={classes.desktopAdjustments}>
              <DetailControls
                servings={servings}
                setServingsOverride={setServingsOverride}
                recipe={recipe}
                protein={protein}
                proteinPreview={proteinPreview}
                baseYield={baseYield}
                applyProtein={applyProtein}
              />
            </div>
          </aside>
        </div>

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

        <div className={classes.fullWidth}>
          <MacroNotes audit={audit} edits={recipe.edits ?? []} />
        </div>

        <div className={classes.mobileAdjustments}>
          <CollapsibleRecipeSection
            title="Servings & protein"
            meta={`serves ${servings}`}
            opened={adjustmentsOpen}
            onToggle={() => setAdjustmentsOpen((value) => !value)}
          >
            <DetailControls
              servings={servings}
              setServingsOverride={setServingsOverride}
              recipe={recipe}
              protein={protein}
              proteinPreview={proteinPreview}
              baseYield={baseYield}
              applyProtein={applyProtein}
            />
          </CollapsibleRecipeSection>
        </div>

        <IngredientList
          ingredients={displayIngredients}
          factor={factor}
          ingredientCosts={ingredientCosts}
          navigate={navigate}
          expanded={ingredientsExpanded}
          setExpanded={setIngredientsExpanded}
          opened={ingredientsOpen}
          onToggle={() => setIngredientsOpen((value) => !value)}
          marginalTotal={marginalTotal}
          usedTotal={usedTotal}
          leftoverTotal={leftoverTotal}
        />

        <CollapsibleRecipeSection
          title="Method"
          meta={`${view.steps.length} steps`}
          opened={methodOpen}
          onToggle={() => setMethodOpen((value) => !value)}
        >
          <Stack gap="lg">
            {view.steps.map((step) => (
              <Group key={step.index} gap="md" align="flex-start" wrap="nowrap" className={classes.methodStep}>
                <ThemeIcon color="fresh" radius="xl" size={30} variant="filled">
                  {step.index}
                </ThemeIcon>
                <Text className={classes.step}>{step.text}</Text>
              </Group>
            ))}
          </Stack>
        </CollapsibleRecipeSection>

        {recipe.source_url && (
          <Anchor href={recipe.source_url} target="_blank" c="dimmed" size="sm" className={classes.sourceLink}>
            <Group gap={6} wrap="nowrap">
              <IconExternalLink size={14} />
              View original on HelloFresh
            </Group>
          </Anchor>
        )}
      </section>
    </main>
  )

}
