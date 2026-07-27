import { Link } from 'react-router-dom'
import { ActionIcon, Badge, Card, Group, Image, Stack, Text, Tooltip } from '@mantine/core'
import {
  IconClock,
  IconFlame,
  IconGauge,
  IconMinus,
  IconPlus,
  IconStarFilled,
  IconTrash,
} from '@tabler/icons-react'

import { MAX_PORTIONS, MIN_PORTIONS } from '../hooks/useWeeklyPlan.js'
import classes from './RecipeCard.module.css'

const PROTEIN_DENSITY_BREAKPOINTS = [4, 6, 8]
const PROTEIN_DENSITY_SEGMENTS = PROTEIN_DENSITY_BREAKPOINTS.length + 1

const PLACEHOLDER =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="100%" height="100%" fill="#e9f9f0"/></svg>',
  )

function round(value) {
  return value == null ? null : Math.round(value)
}

function formatMarginalScore(value) {
  if (value == null) return null
  const absolute = Math.abs(value).toFixed(2)
  return `${value < 0 ? '-' : '+'}£${absolute}`
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

function PlannerControls({ disabled, entry, onAdd, onPortionsChange, onRemove }) {
  if (!entry && !onAdd) return null

  if (!entry) {
    return (
      <Tooltip label={disabled ? 'Week already has 5 recipes' : 'Add to upcoming week'} withArrow>
        <ActionIcon
          component="button"
          type="button"
          color="fresh"
          radius="xl"
          size="lg"
          variant="filled"
          className={classes.addButton}
          disabled={disabled}
          aria-label="Add to upcoming week"
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
  onAddToPlan,
  onPortionsChange,
  onRemoveFromPlan,
  showStats = true,
}) {
  const basketBadge =
    basketBadgeLabel ?? formatBasketBadge(marginalScore, unpricedGapCount, basketAvailable)
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
      <Link to={`/recipes/${recipe.id}`} className={classes.mainLink}>
        <Card.Section className={classes.imageWrap}>
          <Image
            src={recipe.image_url || PLACEHOLDER}
            fallbackSrc={PLACEHOLDER}
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
            {recipe.tags?.length > 0 && (
              <Group gap={6} mt={4}>
                {recipe.tags.slice(0, 2).map((tag) => (
                  <Badge key={tag} variant="light" color="fresh" size="sm" radius="sm">
                    {tag}
                  </Badge>
                ))}
              </Group>
            )}
          </div>

          {showStats && (
            <div className={classes.stats}>
            <div className={classes.statRow}>
              <StatSlot>
                {recipe.energy_kcal != null && (
                  <>
                    <IconFlame size={14} />
                    <Text size="xs">{round(recipe.energy_kcal)} kcal</Text>
                  </>
                )}
              </StatSlot>
              <StatSlot strong>
                {recipe.protein_g != null && (
                  <Text size="xs">{round(recipe.protein_g)}g protein</Text>
                )}
              </StatSlot>
            </div>

            <div className={classes.statRow}>
              <StatSlot>
                {recipe.protein_energy_ratio != null && (
                  <ProteinDensityMeter value={recipe.protein_energy_ratio} />
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
          entry={plannerEntry}
          onAdd={onAddToPlan}
          onPortionsChange={onPortionsChange}
          onRemove={onRemoveFromPlan}
        />
      </div>
    </Card>
  )
}
