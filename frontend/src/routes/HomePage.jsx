import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Group,
  Loader,
  SegmentedControl,
  Skeleton,
  Stack,
  Text,
  Title,
  Tooltip,
} from '@mantine/core'
import {
  IconAlertCircle,
  IconBasket,
  IconCalendarWeek,
  IconChevronUp,
  IconPlayerPause,
  IconPlus,
  IconX,
} from '@tabler/icons-react'

import { RECIPE_PLACEHOLDER_IMAGE } from '../constants/images.js'
import {
  formatWeekStart,
  MAX_PAST_WEEKS,
  useSchedule,
  useSetWeekSkipped,
  useUpdateScheduleSettings,
} from '../hooks/useSchedule.js'
import {
  DEFAULT_RECIPES_PER_WEEK,
  formatProteinModifier,
  useWeeklyPlan,
} from '../hooks/useWeeklyPlan.js'
import { useCooked, useSetCooked } from '../hooks/usePastRecipes.js'
import PageHeader from '../components/PageHeader.jsx'
import classes from './HomePage.module.css'

const STATUS_BADGE = {
  open: { label: 'Planning', color: 'fresh' },
  closed: { label: 'Cutoff passed', color: 'gray' },
  skipped: { label: 'Skipped', color: 'orange' },
  paused: { label: 'Paused', color: 'gray' },
}

// How many more finished shops each click of the history button reveals.
const PAST_WEEKS_STEP = 4

// History says what became of a week, not how long is left to change it.
function pastBadge(week, shopped) {
  if (week.skipped) return { label: 'Skipped', color: 'orange' }
  if (!week.complete) return { label: 'This week', color: 'fresh' }
  if (shopped === false) return {
    label: 'Not shopped',
    color: 'gray',
    tooltip: 'Never pushed to a cart, so nothing here counts as cooked.',
  }
  return { label: 'Done', color: 'gray' }
}

function cadenceLabel(cadenceWeeks) {
  if (cadenceWeeks === 1) return 'Every week'
  if (cadenceWeeks === 2) return 'Every fortnight'
  return `Every ${cadenceWeeks} weeks`
}

function cutoffDaysLabel(cutoffAt) {
  const cutoff = new Date(cutoffAt).getTime()
  if (!Number.isFinite(cutoff)) return null
  const days = Math.max(0, Math.ceil((cutoff - Date.now()) / 86_400_000))
  return `Cutoff in ${days} ${days === 1 ? 'day' : 'days'}`
}

function RecipeTile({ entry, weekStart, onRemove, editable, cookedState }) {
  const { recipe } = entry
  // How it was cooked, not how it is written. A week's protein swap or scaling
  // is the difference between the dish you planned and the one in the library,
  // and it is worth nothing if you have to remember it yourself.
  const modifier = formatProteinModifier(entry.protein)
  return (
    <Box
      className={[
        classes.tile,
        cookedState && cookedState.cooked ? classes.tileCooked : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {/* The week goes with the link: the detail page shows the modifications
          the week holds, and without it a tile from March opens showing next
          week's plan for the same recipe. */}
      <Link to={`/recipes/${recipe.id}?week=${weekStart}`} aria-label={recipe.name}>
        <img
          className={classes.tileImage}
          src={recipe.image_url || RECIPE_PLACEHOLDER_IMAGE}
          alt=""
          loading="lazy"
        />
        {modifier && (
          <Tooltip label={`Cooked with: ${modifier}`} withArrow>
            <span className={classes.tileModifier}>{modifier}</span>
          </Tooltip>
        )}
        <div className={classes.tileCaption}>
          <div className={classes.tileName}>{recipe.name}</div>
          <div className={classes.tilePortions}>{entry.portions} portions</div>
        </div>
      </Link>
      {editable && (
        <Tooltip label="Remove from this week" withArrow>
          <ActionIcon
            className={classes.tileRemove}
            size="sm"
            radius="xl"
            color="red"
            variant="filled"
            aria-label={`Remove ${recipe.name} from week of ${weekStart}`}
            onClick={onRemove}
          >
            <IconX size={14} />
          </ActionIcon>
        </Tooltip>
      )}
      {cookedState && (
        <Checkbox
          className={[
            classes.tileCookedCheck,
            cookedState.marked ? classes.tileCookedMarked : classes.tileCookedAssumed,
          ]
            .filter(Boolean)
            .join(' ')}
          checked={cookedState.cooked}
          disabled={cookedState.pending}
          onChange={(event) => cookedState.onToggle(event.currentTarget.checked)}
          label="Cooked"
          size="xs"
          aria-label={`${recipe.name}: cooked`}
        />
      )}
    </Box>
  )
}

function AddMealsButton({ weekStart }) {
  return (
    <Button
      component={Link}
      to={`/browse?week=${weekStart}`}
      className={classes.addMealsButton}
      size="compact-sm"
      leftSection={<IconPlus size={15} />}
      aria-label={`Add meals to the week of ${weekStart}`}
    >
      Add meals
    </Button>
  )
}

function WeekRow({
  week,
  entries,
  recipesPerWeek,
  past = false,
  showCutoff = false,
  onRemoveRecipe,
  onToggleSkip,
  skipPending,
  cookedFlags,
  cookedPendingKey,
  onToggleCooked,
  shopped,
}) {
  const badge = past
    ? pastBadge(week, shopped)
    : STATUS_BADGE[week.status] ?? STATUS_BADGE.open
  const cutoffLabel = showCutoff ? cutoffDaysLabel(week.cutoff_at) : null
  // A week that has been shopped for is a record, not a draft: its recipes are
  // what was cooked, and editing them would rewrite history rather than change
  // anything. The planning window is where recipes are chosen.
  const editable = !past && (week.status === 'open' || week.status === 'closed')
  const remaining = Math.max(recipesPerWeek - entries.length, 0)

  return (
    <Box
      className={[
        classes.weekRow,
        week.is_active ? classes.weekRowActive : '',
        past ? classes.weekRowPast : '',
        past && !week.complete ? classes.weekRowCurrent : '',
        entries.length > 0 ? classes.weekRowWithMeals : classes.weekRowEmpty,
        !past && (week.status === 'skipped' || week.status === 'paused')
          ? classes.weekRowInactive
          : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className={classes.weekInfo}>
        <Group gap={8} wrap="wrap" className={classes.weekHeading}>
          <Title order={4} className={classes.weekTitle}>
            {formatWeekStart(week.week_start)}
          </Title>
          {badge.tooltip ? (
            <Tooltip label={badge.tooltip} withArrow multiline maw={260}>
              <Badge size="sm" variant="light" color={badge.color} radius="sm" style={{ cursor: 'help' }}>
                {badge.label}
              </Badge>
            </Tooltip>
          ) : (
            <Badge size="sm" variant="light" color={badge.color} radius="sm">
              {badge.label}
            </Badge>
          )}
        </Group>

        <Group gap={6} className={classes.weekDetails}>
          <Text size="xs" c="dimmed">
            {past
              ? `${entries.length} ${entries.length === 1 ? 'recipe' : 'recipes'}`
              : `${entries.length}/${recipesPerWeek} recipes`}
          </Text>

          {cutoffLabel && (
            <Text size="xs" c="dimmed" className={classes.cutoff}>
              {cutoffLabel}
            </Text>
          )}
        </Group>

      </div>

      <div className={classes.weekActions}>
        {!past && (
          <Button
            size="compact-sm"
            variant="subtle"
            color={week.skipped ? 'fresh' : 'gray'}
            loading={skipPending}
            onClick={() => onToggleSkip(week)}
            disabled={week.status === 'paused'}
          >
            {week.skipped ? 'Plan this week' : 'Skip week'}
          </Button>
        )}
        <Group gap="xs" wrap="nowrap">
          {entries.length > 0 && (
            <Button
              size="compact-sm"
              variant="subtle"
              component={Link}
              to={`/basket?week=${week.week_start}`}
              leftSection={<IconBasket size={14} />}
            >
              Basket
            </Button>
          )}
          {editable && remaining > 0 && <AddMealsButton weekStart={week.week_start} />}
        </Group>
      </div>

      {entries.length > 0 && (
        <div className={classes.tiles}>
          {entries.map((entry) => {
            const flag = cookedFlags?.get(entry.recipe.id)
            return (
              <RecipeTile
                key={entry.recipe.id}
                entry={entry}
                weekStart={week.week_start}
                editable={editable}
                onRemove={
                  onRemoveRecipe
                    ? () => onRemoveRecipe(week.week_start, entry.recipe.id)
                    : undefined
                }
                cookedState={
                  past && cookedFlags
                    ? {
                        cooked: flag?.cooked ?? false,
                        marked: flag?.marked ?? false,
                        pending:
                          cookedPendingKey ===
                          `${week.week_start}:${entry.recipe.id}`,
                        onToggle: (cooked) =>
                          onToggleCooked(
                            week.week_start,
                            entry.recipe.id,
                            cooked,
                          ),
                      }
                    : undefined
                }
              />
            )
          })}
        </div>
      )}
    </Box>
  )
}

export default function HomePage() {
  const [view, setView] = useState('current')
  const isPastView = view === 'past'

  // In past view we want more history by default; in current view the most
  // recent completed week is enough context.
  const [pastWeeks, setPastWeeks] = useState(1)
  const effectivePastWeeks = isPastView
    ? Math.max(pastWeeks, PAST_WEEKS_STEP)
    : pastWeeks

  const { data: schedule, isError, error, isFetching, isPaused } = useSchedule(effectivePastWeeks)
  const { getWeekRecipes, removeRecipeFromWeek } = useWeeklyPlan()
  const setSkipped = useSetWeekSkipped()
  const updateSettings = useUpdateScheduleSettings()

  // --- Cooked state (only active in past view) ---
  const pastList = schedule?.past_weeks ?? []
  const weekStarts = useMemo(
    () => pastList.map((week) => week.week_start),
    [pastList],
  )
  const cooked = useCooked(isPastView ? weekStarts : [])
  const setCooked = useSetCooked()
  const cookedByWeek = useMemo(() => {
    const map = new Map()
    for (const week of cooked.data?.weeks ?? []) map.set(week.week_start, week)
    return map
  }, [cooked.data])
  const cookedPendingKey = setCooked.isPending
    ? `${setCooked.variables?.weekStart}:${setCooked.variables?.recipeId}`
    : null
  const onToggleCooked = (weekStart, recipeId, value) =>
    setCooked.mutate({ weekStart, recipeId, cooked: value })

  // Build per-week cooked flag maps for passing to WeekRow.
  const cookedFlagsForWeek = useMemo(() => {
    const result = new Map()
    for (const [weekStart, weekData] of cookedByWeek) {
      const map = new Map()
      for (const recipe of weekData.recipes ?? []) {
        map.set(recipe.recipe_id, recipe)
      }
      result.set(weekStart, map)
    }
    return result
  }, [cookedByWeek])

  // The old, shorter history stays on screen while a longer one loads, so the
  // spinner belongs to that request only — not to every background refetch.
  const expanding = isFetching && (schedule?.past_weeks?.length ?? 0) < effectivePastWeeks
  const settings = schedule?.settings
  const paused = Boolean(settings?.paused)
  const recipesPerWeek = settings?.recipes_per_week ?? DEFAULT_RECIPES_PER_WEEK

  // Past view: reverse chronological, only weeks that have recipes.
  const pastWeeksWithRecipes = useMemo(
    () =>
      pastList
        .slice()
        .reverse()
        .filter((week) => getWeekRecipes(week.week_start).length > 0),
    [pastList, getWeekRecipes],
  )

  const scheduleDescription = settings ? (
    `${cadenceLabel(settings.cadence_weeks)} · recipes settled by ${
      settings.cutoff_days_before === 0
        ? `the start of the week, ${settings.cutoff_time}`
        : `${settings.cutoff_days_before} ${
            settings.cutoff_days_before === 1 ? 'day' : 'days'
          } before, ${settings.cutoff_time}`
    }`
  ) : (
    <Skeleton height={16} width={280} />
  )

  return (
    <Stack gap={{ base: 'lg', sm: 'xl' }} className={classes.pageStack}>
      <PageHeader
        title="Your shops"
        description={scheduleDescription}
        icon={<IconCalendarWeek size={22} />}
        actions={(
          <Group gap="sm" wrap="nowrap" className={classes.headerActions}>
          <SegmentedControl
            className={classes.viewToggle}
            size="sm"
            value={view}
            onChange={setView}
            data={[
              { label: 'Current', value: 'current' },
              { label: 'Past', value: 'past' },
            ]}
          />
          </Group>
        )}
      />

      {!isPastView && paused && (
        <Alert color="gray" variant="light" icon={<IconPlayerPause size={18} />}>
          The schedule is paused — no week is being planned. Resume to pick up the
          cadence where it left off.
        </Alert>
      )}

      {(setSkipped.error || updateSettings.error) && (
        <Alert color="red" icon={<IconAlertCircle size={18} />}>
          {(setSkipped.error ?? updateSettings.error).message}
        </Alert>
      )}

      {setCooked.error && (
        <Alert color="red" icon={<IconAlertCircle size={18} />}>
          {setCooked.error.message}
        </Alert>
      )}

      {/* Three failure states, not one, and the middle one is why this page used
          to go blank. React Query defaults to ``networkMode: 'online'``: when a
          request fails and it decides the browser is offline it *pauses* the
          retry rather than failing, leaving status ``pending``, nothing
          fetching, no error and no data — permanently. ``isLoading`` is only
          ``isPending && isFetching``, so it was false, ``isError`` was false,
          and the render fell through to ``schedule.has_more_past`` and threw on
          undefined. Hence: report the paused case, and gate the rest on the data
          itself rather than on a loading flag that does not mean what it looks
          like. */}
      {isError ? (
        <Alert color="red" title="Couldn't load your schedule" icon={<IconAlertCircle size={18} />}>
          {error?.message ?? 'Please check the backend is running and try again.'}
        </Alert>
      ) : isPaused ? (
        <Alert color="orange" title="Can't reach the backend" icon={<IconAlertCircle size={18} />}>
          Your schedule will load by itself once the connection is back.
        </Alert>
      ) : !schedule ? (
        <Group justify="center" py="xl">
          <Loader color="fresh" />
        </Group>
      ) : isPastView ? (
        /* ── Past view ────────────────────────────────────── */
        <Stack gap="md">
          {pastWeeksWithRecipes.length === 0 ? (
            <Box className={classes.emptyState}>
              <Text fw={800}>No shops behind you yet</Text>
              <Text size="sm" c="dimmed">
                Finished weeks land here once their baskets have been pushed.
              </Text>
            </Box>
          ) : (
            pastWeeksWithRecipes.map((week) => {
              const cookedWeek = cookedByWeek.get(week.week_start)
              return (
                <WeekRow
                  key={week.week_start}
                  week={week}
                  entries={getWeekRecipes(week.week_start)}
                  recipesPerWeek={recipesPerWeek}
                  past
                  shopped={Boolean(cookedWeek?.shopped)}
                  cookedFlags={cookedFlagsForWeek.get(week.week_start) ?? new Map()}
                  cookedPendingKey={cookedPendingKey}
                  onToggleCooked={onToggleCooked}
                />
              )
            })
          )}

          {schedule.has_more_past && (
            <Group justify="center">
              <Button
                size="compact-sm"
                variant="subtle"
                color="gray"
                loading={expanding}
                leftSection={<IconChevronUp size={14} />}
                onClick={() =>
                  setPastWeeks((count) =>
                    Math.min(count + PAST_WEEKS_STEP, MAX_PAST_WEEKS),
                  )
                }
              >
                Show {PAST_WEEKS_STEP} earlier weeks
              </Button>
            </Group>
          )}

          <Text size="xs" c="dimmed">
            What these recipes did not use is in the{' '}
            <Link to="/pantry">pantry</Link>, and comes off the next shop.
          </Text>
        </Stack>
      ) : (
        /* ── Current view ─────────────────────────────────── */
        <Stack gap="md">
          {schedule.weeks.map((week, index) => (
            <WeekRow
              key={week.week_start}
              week={week}
              entries={getWeekRecipes(week.week_start)}
              recipesPerWeek={recipesPerWeek}
              showCutoff={index === 0}
              onRemoveRecipe={removeRecipeFromWeek}
              onToggleSkip={(target) =>
                setSkipped.mutate({ weekStart: target.week_start, skipped: !target.skipped })
              }
              skipPending={
                setSkipped.isPending && setSkipped.variables?.weekStart === week.week_start
              }
            />
          ))}
        </Stack>
      )}
    </Stack>
  )
}
