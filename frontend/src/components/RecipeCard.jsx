import { Link, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Badge,
  Card,
  Group,
  Image,
  Skeleton,
  Stack,
  Text,
  Tooltip,
} from '@mantine/core'
import {
  IconClock,
  IconFlame,
  IconGauge,
  IconHeartFilled,
  IconMeat,
  IconMinus,
  IconPlus,
  IconStarFilled,
  IconTrash,
} from '@tabler/icons-react'

import {
  formatProteinModifier,
  MAX_PORTIONS,
  MIN_PORTIONS,
} from '../hooks/useWeeklyPlan.js'
import { useProteinPreview } from '../hooks/useRecipeQueries.js'
import { RECIPE_PLACEHOLDER_IMAGE } from '../constants/images.js'
import classes from './RecipeCard.module.css'

const PROTEIN_DENSITY_BREAKPOINTS = [4, 6, 8]
const PROTEIN_DENSITY_SEGMENTS = PROTEIN_DENSITY_BREAKPOINTS.length + 1

function round(value) {
  return value == null ? null : Math.round(value)
}

function formatMarginalScore(value) {
  if (value == null) return null
  const absolute = Math.abs(value).toFixed(2)
  return `${value < 0 ? '-' : '+'}£${absolute}`
}

const COURSE_LABELS = {
  side: 'Side',
  breakfast: 'Breakfast',
  dessert: 'Dessert',
  product: 'Ready-made',
}

function formatBasketBadge(marginalScore, unpricedGapCount, basketAvailable) {
  if (!basketAvailable) return 'No basket data'
  const marginal = formatMarginalScore(marginalScore)
  if (!marginal) return null
  return marginal
}

function proteinDensityLevel(value) {
  if (value == null) return 0

  return PROTEIN_DENSITY_BREAKPOINTS.filter((breakpoint) => value >= breakpoint).length + 1
}

function densityFromMacros(proteinG, kcal) {
  if (proteinG == null || !kcal) return null
  return Math.round((proteinG / kcal) * 1000) / 10
}

function differsRounded(left, right) {
  if (left == null || right == null) return false
  return round(left) !== round(right)
}

function ProteinDensityMeter({ value }) {
  const activeSegments = proteinDensityLevel(value)
  const label = `${value}g/100kcal protein density`

  return (
    <Tooltip label={label} withArrow position="top">
      <span className={classes.densityMetric} aria-label={label}>
        <IconGauge size={14} className={classes.densityIcon} />
        <span className={classes.densityMeter} aria-hidden="true">
          {Array.from({ length: PROTEIN_DENSITY_SEGMENTS }).map((_, index) => (
            <span
              key={index}
              className={[
                classes.densitySegment,
                index < activeSegments ? classes.densitySegmentActive : '',
              ]
                .filter(Boolean)
                .join(' ')}
            />
          ))}
        </span>
      </span>
    </Tooltip>
  )
}

function StatSlot({ children, strong = false }) {
  return (
    <div className={strong ? `${classes.statSlot} ${classes.statSlotStrong}` : classes.statSlot}>
      {children}
    </div>
  )
}

function stopCardNavigation(event) {
  event.preventDefault()
  event.stopPropagation()
}

function PlannerControls({ disabled, entry, readOnly = false, onAdd, onPortionsChange, onRemove }) {
  if (!entry && !onAdd) return null

  // A shop that has been and gone still shows what was cooked, and nothing that
  // offers to change it. The count is the record, so it stays; the buttons that
  // would rewrite it do not.
  if (readOnly) {
    if (!entry) return null
    return (
      <div className={classes.plannerControlGroup}>
        <div
          className={classes.portionStepper}
          aria-label={`${entry.portions} portions, as shopped for`}
        >
          <Text span fw={700} size="sm" className={classes.portionCount}>
            {entry.portions}
          </Text>
        </div>
      </div>
    )
  }

  if (!entry) {
    return (
      <Tooltip
        label={disabled ? 'This week is already full' : 'Add to the week you are planning'}
        withArrow
      >
        <ActionIcon
          component="button"
          type="button"
          color="fresh"
          radius="xl"
          size="lg"
          variant="filled"
          className={classes.addButton}
          disabled={disabled}
          aria-label="Add to this week"
          onClick={(event) => {
            stopCardNavigation(event)
            if (!disabled) onAdd?.()
          }}
        >
          <IconPlus size={20} />
        </ActionIcon>
      </Tooltip>
    )
  }

  const portions = entry.portions
  const minusLabel = 'Decrease portions'

  return (
    <div className={classes.plannerControlGroup}>
      <div className={classes.portionStepper} aria-label={`${portions} portions selected`}>
        <Tooltip label={minusLabel} withArrow>
          <ActionIcon
            component="button"
            type="button"
            size="sm"
            radius="xl"
            variant="subtle"
            color="gray"
            disabled={portions <= MIN_PORTIONS}
            aria-label={minusLabel}
            onClick={(event) => {
              stopCardNavigation(event)
              onPortionsChange?.(portions - 1)
            }}
          >
            <IconMinus size={16} />
          </ActionIcon>
        </Tooltip>
        <Text span fw={700} size="sm" className={classes.portionCount}>
          {portions}
        </Text>
        <Tooltip label="Increase portions" withArrow>
          <ActionIcon
            component="button"
            type="button"
            size="sm"
            radius="xl"
            variant="subtle"
            color="fresh"
            disabled={portions >= MAX_PORTIONS}
            aria-label="Increase portions"
            onClick={(event) => {
              stopCardNavigation(event)
              onPortionsChange?.(portions + 1)
            }}
          >
            <IconPlus size={16} />
          </ActionIcon>
        </Tooltip>
      </div>
      <Tooltip label="Remove recipe" withArrow>
        <ActionIcon
          component="button"
          type="button"
          color="red"
          radius="xl"
          size="lg"
          variant="filled"
          className={classes.removeButton}
          aria-label="Remove recipe"
          onClick={(event) => {
            stopCardNavigation(event)
            onRemove?.()
          }}
        >
          <IconTrash size={17} />
        </ActionIcon>
      </Tooltip>
    </div>
  )
}

export default function RecipeCard({
  recipe,
  basketAvailable = true,
  basketBadgeLabel = null,
  highlighted = false,
  marginalScore = null,
  unpricedGapCount = 0,
  plannerEntry = null,
  plannerControlsVisible = false,
  plannerDisabled = false,
  plannerReadOnly = false,
  onAddToPlan,
  onPortionsChange,
  onRemoveFromPlan,
  showStats = true,
}) {
  const [searchParams] = useSearchParams()
  // Carry the week being edited through to the detail page, so adding it from
  // there lands in the same week the browse grid was filling.
  const editingWeek = searchParams.get('week')
  const detailHref = editingWeek
    ? `/recipes/${recipe.id}?week=${editingWeek}`
    : `/recipes/${recipe.id}`
  const basketBadge =
    basketBadgeLabel ?? formatBasketBadge(marginalScore, unpricedGapCount, basketAvailable)
  const proteinLabel = formatProteinModifier(plannerEntry?.protein)
  const proteinPreview = useProteinPreview(recipe.id, plannerEntry?.protein, {
    enabled: showStats && Boolean(plannerEntry?.protein),
  })
  const adjustedMacros = proteinPreview.data?.changed ? proteinPreview.data.macros_after : null
  const energyKcal = adjustedMacros?.kcal ?? recipe.energy_kcal
  const proteinG = adjustedMacros?.protein_g ?? recipe.protein_g
  const proteinDensity = adjustedMacros
    ? densityFromMacros(adjustedMacros.protein_g, adjustedMacros.kcal)
    : recipe.protein_energy_ratio
  const energyAdjusted = differsRounded(energyKcal, recipe.energy_kcal)
  const proteinAdjusted = differsRounded(proteinG, recipe.protein_g)
  const densityAdjusted = differsRounded(proteinDensity, recipe.protein_energy_ratio)
  const cardClass = [
    classes.card,
    plannerEntry ? classes.cardSelected : '',
    highlighted ? classes.cardHighlighted : '',
  ]
    .filter(Boolean)
    .join(' ')
  const controlClass = [
    classes.planControls,
    plannerEntry ? classes.planControlsSelected : '',
    plannerControlsVisible ? classes.planControlsVisible : '',
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <Card padding="0" radius="md" withBorder className={cardClass}>
      <Link to={detailHref} className={classes.mainLink}>
        <Card.Section className={classes.imageWrap}>
          <Image
            src={recipe.image_url || RECIPE_PLACEHOLDER_IMAGE}
            fallbackSrc={RECIPE_PLACEHOLDER_IMAGE}
            alt={recipe.name}
            className={classes.image}
            loading="lazy"
          />
          {recipe.ratings_count != null && (
            <Badge className={classes.ratingBadge} variant="filled" color="dark" radius="sm">
              <Group gap={3} wrap="nowrap">
                <IconStarFilled size={11} />
                {recipe.avg_rating != null ? recipe.avg_rating.toFixed(1) : '-'}
              </Group>
            </Badge>
          )}
          {recipe.personal_rating != null && (
            <Badge className={classes.personalRatingBadge} variant="filled" radius="sm">
              <Group gap={3} wrap="nowrap">
                <IconStarFilled size={11} /> {recipe.personal_rating}
              </Group>
            </Badge>
          )}
          {recipe.wishlisted && (
            <Tooltip label="On your wishlist" withArrow>
              <span className={classes.wishlistBadge} aria-label="On your wishlist">
                <IconHeartFilled size={15} />
              </span>
            </Tooltip>
          )}
          {basketBadge && (
            <span className={classes.marginalBadgeWrap}>
              <Badge
                className={classes.marginalBadge}
                variant="filled"
                color={basketAvailable ? 'fresh' : 'gray'}
                radius="sm"
              >
                {basketBadge}
              </Badge>
              {basketAvailable && unpricedGapCount > 0 && (
                <span
                  className={classes.unpricedBadge}
                  data-darkreader-ignore="true"
                  style={{ backgroundColor: 'rgb(250, 82, 82)', color: 'rgb(255, 255, 255)' }}
                >
                  {unpricedGapCount}
                </span>
              )}
            </span>
          )}
        </Card.Section>

        <Stack gap={6} p="sm" className={classes.body}>
          <div className={classes.content}>
            {recipe.cuisines?.length > 0 && (
              <Text size="xs" c="fresh.8" fw={600} tt="uppercase" className={classes.cuisine}>
                {recipe.cuisines[0]}
              </Text>
            )}
            <Text fw={600} lineClamp={2} className={classes.title}>
              {recipe.name}
            </Text>
            {recipe.headline && (
              <Text size="xs" c="dimmed" lineClamp={1}>
                {recipe.headline}
              </Text>
            )}
            {(recipe.course && recipe.course !== 'main') ||
            recipe.tags?.length > 0 ||
            proteinLabel ? (
              <Group gap={6} mt={4}>
                {/* Say what this is when it is not dinner: a side, a dessert or
                    something you just buy. Mains are the norm and go unlabelled. */}
                {recipe.course && recipe.course !== 'main' && (
                  <Badge variant="light" color="gray" size="sm" radius="sm">
                    {COURSE_LABELS[recipe.course] ?? recipe.course}
                  </Badge>
                )}
                {/* A planned recipe whose protein has been swapped or scaled is
                    not the dish its name and macros describe, and the basket is
                    priced for the modified one. Say so on the card. */}
                {proteinLabel && (
                  <Badge variant="light" color="grape" size="sm" radius="sm">
                    {proteinLabel}
                  </Badge>
                )}
                {(recipe.tags ?? []).slice(0, 2).map((tag) => (
                  <Badge key={tag} variant="light" color="fresh" size="sm" radius="sm">
                    {tag}
                  </Badge>
                ))}
              </Group>
            ) : null}
          </div>

          {showStats && (
            <div className={classes.stats}>
              <div className={classes.statRow}>
                <StatSlot>
                  {energyKcal != null && (
                    <>
                      <IconFlame size={14} />
                      <Text size="xs" className={energyAdjusted ? classes.adjustedStat : ''}>
                        {round(energyKcal)} kcal
                      </Text>
                    </>
                  )}
                </StatSlot>
                <StatSlot strong>
                  {proteinG != null && (
                    <>
                      <IconMeat size={14} />
                      <Text size="xs" className={proteinAdjusted ? classes.adjustedStat : ''}>
                        {round(proteinG)}g protein
                      </Text>
                    </>
                  )}
                </StatSlot>
              </div>

              <div className={classes.statRow}>
                <StatSlot>
                  {proteinDensity != null && (
                    <span className={densityAdjusted ? classes.adjustedStat : ''}>
                      <ProteinDensityMeter value={proteinDensity} />
                    </span>
                  )}
                </StatSlot>
                <StatSlot>
                  {recipe.total_time_min != null && (
                    <>
                      <IconClock size={14} />
                      <Text size="xs">{recipe.total_time_min} min</Text>
                    </>
                  )}
                </StatSlot>
              </div>
            </div>
          )}
        </Stack>
      </Link>

      <div className={controlClass}>
        <PlannerControls
          disabled={plannerDisabled}
          readOnly={plannerReadOnly}
          entry={plannerEntry}
          onAdd={onAddToPlan}
          onPortionsChange={onPortionsChange}
          onRemove={onRemoveFromPlan}
        />
      </div>
    </Card>
  )
}

/** A loading tile built from the card's own layout.
 *
 * The card has no fixed height: its image is a 4/3 slice of whatever the column
 * is wide, and the body grows with the title and stats. A plain box of a chosen
 * height therefore never lines up — and the browse grid shows placeholders
 * beside real cards while the next page loads, where a mismatch reads as a gap.
 * Sharing the stylesheet keeps the two the same shape at every breakpoint, and
 * the same `height: 100%` lets a placeholder stretch to its row.
 */
export function RecipeCardSkeleton({ showStats = true }) {
  return (
    <Card padding="0" radius="md" withBorder className={classes.card} aria-hidden>
      <Card.Section className={classes.imageWrap}>
        <Skeleton height="100%" radius={0} />
      </Card.Section>

      <Stack gap={6} p="sm" className={classes.body}>
        <div className={classes.content}>
          <Skeleton height={12} width="42%" radius="sm" />
          <Skeleton height={18} radius="sm" />
          <Skeleton height={18} width="72%" radius="sm" />
          <Skeleton height={12} width="88%" radius="sm" />
          <Group gap={6} mt={4}>
            <Skeleton height={20} width={62} radius="sm" />
            <Skeleton height={20} width={44} radius="sm" />
          </Group>
        </div>

        {showStats && (
          <div className={classes.stats}>
            <div className={classes.statRow}>
              <Skeleton height={18} width="72%" radius="sm" />
              <Skeleton height={18} width="80%" radius="sm" />
            </div>
            <div className={classes.statRow}>
              <Skeleton height={18} width="88%" radius="sm" />
              <Skeleton height={18} width="56%" radius="sm" />
            </div>
          </div>
        )}
      </Stack>
    </Card>
  )
}
