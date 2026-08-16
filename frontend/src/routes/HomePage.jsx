import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Divider,
  Group,
  Loader,
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
  IconPlayerPlay,
  IconPlus,
  IconSettings,
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
function pastBadge(week) {
  if (week.skipped) return { label: 'Skipped', color: 'orange' }
  if (!week.complete) return { label: 'This week', color: 'fresh' }
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

function RecipeTile({ entry, weekStart, onRemove, editable }) {
  const { recipe } = entry
  // How it was cooked, not how it is written. A week's protein swap or scaling
  // is the difference between the dish you planned and the one in the library,
  // and it is worth nothing if you have to remember it yourself.
  const modifier = formatProteinModifier(entry.protein)
  return (
    <Box className={classes.tile}>
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
}) {
  const badge = past ? pastBadge(week) : STATUS_BADGE[week.status] ?? STATUS_BADGE.open
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
          <Badge size="sm" variant="light" color={badge.color} radius="sm">
            {badge.label}
          </Badge>
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
          {entries.map((entry) => (
            <RecipeTile
              key={entry.recipe.id}
              entry={entry}
              weekStart={week.week_start}
              editable={editable}
              onRemove={() => onRemoveRecipe(week.week_start, entry.recipe.id)}
            />
          ))}
        </div>
      )}
    </Box>
  )
}

export default function HomePage() {
  // The last finished shop is shown by default — it is the one you are most
  // likely to be looking back at — and older ones are asked for a few at a time.
  const [pastWeeks, setPastWeeks] = useState(1)
  const { data: schedule, isError, error, isFetching, isPaused } = useSchedule(pastWeeks)
  const { getWeekRecipes, removeRecipeFromWeek } = useWeeklyPlan()
  const setSkipped = useSetWeekSkipped()
  const updateSettings = useUpdateScheduleSettings()

  // The old, shorter history stays on screen while a longer one loads, so the
  // spinner belongs to that request only — not to every background refetch.
  const expanding = isFetching && (schedule?.past_weeks?.length ?? 0) < pastWeeks
  const settings = schedule?.settings
  const paused = Boolean(settings?.paused)
  const recipesPerWeek = settings?.recipes_per_week ?? DEFAULT_RECIPES_PER_WEEK

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
          <Group gap="xs" wrap="nowrap" className={classes.headerActions}>
          <Button
            variant={paused ? 'filled' : 'default'}
            color={paused ? 'fresh' : 'gray'}
            size="sm"
            loading={updateSettings.isPending}
            leftSection={
              paused ? <IconPlayerPlay size={16} /> : <IconPlayerPause size={16} />
            }
            onClick={() => updateSettings.mutate({ paused: !paused })}
          >
            {paused ? 'Resume' : 'Pause'}
          </Button>
          <Tooltip label="Schedule settings" withArrow>
            <ActionIcon
              className={classes.settingsButton}
              component={Link}
              to="/settings"
              variant="default"
              size="lg"
              aria-label="Schedule settings"
            >
              <IconSettings size={18} />
            </ActionIcon>
          </Tooltip>
          </Group>
        )}
      />

      {paused && (
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
      ) : (
        <Stack gap="md">
          {schedule.has_more_past && (
            <Group justify="center">
              <Button
                size="compact-sm"
                variant="subtle"
                color="gray"
                loading={expanding}
                leftSection={<IconChevronUp size={14} />}
                onClick={() =>
                  setPastWeeks((count) => Math.min(count + PAST_WEEKS_STEP, MAX_PAST_WEEKS))
                }
              >
                Show {PAST_WEEKS_STEP} earlier weeks
              </Button>
            </Group>
          )}

          {(schedule.past_weeks ?? []).map((week) => (
            <WeekRow
              key={week.week_start}
              week={week}
              entries={getWeekRecipes(week.week_start)}
              recipesPerWeek={recipesPerWeek}
              past
            />
          ))}

          {schedule.past_weeks?.length > 0 && (
            <Divider
              className={classes.historyDivider}
              labelPosition="center"
              label="Coming up"
            />
          )}

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
