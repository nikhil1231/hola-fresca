import { Link } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
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
  IconPlayerPause,
  IconPlayerPlay,
  IconPlus,
  IconSettings,
  IconX,
} from '@tabler/icons-react'

import { RECIPE_PLACEHOLDER_IMAGE } from '../constants/images.js'
import {
  formatCutoff,
  formatCutoffCountdown,
  formatWeekRange,
  useSchedule,
  useSetWeekSkipped,
  useUpdateScheduleSettings,
} from '../hooks/useSchedule.js'
import { DEFAULT_RECIPES_PER_WEEK, useWeeklyPlan } from '../hooks/useWeeklyPlan.js'
import classes from './HomePage.module.css'

const STATUS_BADGE = {
  open: { label: 'Planning', color: 'fresh' },
  closed: { label: 'Cutoff passed', color: 'gray' },
  skipped: { label: 'Skipped', color: 'orange' },
  paused: { label: 'Paused', color: 'gray' },
}

function cadenceLabel(cadenceWeeks) {
  if (cadenceWeeks === 1) return 'Every week'
  if (cadenceWeeks === 2) return 'Every fortnight'
  return `Every ${cadenceWeeks} weeks`
}

function RecipeTile({ entry, weekStart, onRemove, editable }) {
  const { recipe } = entry
  return (
    <Box className={classes.tile}>
      <Link to={`/recipes/${recipe.id}`} aria-label={recipe.name}>
        <img
          className={classes.tileImage}
          src={recipe.image_url || RECIPE_PLACEHOLDER_IMAGE}
          alt=""
          loading="lazy"
        />
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

function AddTile({ weekStart, remaining }) {
  return (
    <Tooltip label={`Pick recipes for ${formatWeekRange(weekStart)}`} withArrow>
      <Box
        component={Link}
        to={`/browse?week=${weekStart}`}
        className={classes.addTile}
        aria-label={`Add recipes to the week of ${weekStart}`}
      >
        <IconPlus size={22} />
        <span className={classes.addTileLabel}>
          {remaining} {remaining === 1 ? 'slot' : 'slots'}
        </span>
      </Box>
    </Tooltip>
  )
}

function WeekRow({ week, entries, recipesPerWeek, onRemoveRecipe, onToggleSkip, skipPending }) {
  const badge = STATUS_BADGE[week.status] ?? STATUS_BADGE.open
  const countdown = week.status === 'open' ? formatCutoffCountdown(week.cutoff_at) : null
  const editable = week.status === 'open' || week.status === 'closed'
  const remaining = Math.max(recipesPerWeek - entries.length, 0)

  return (
    <Box
      className={[
        classes.weekRow,
        week.is_active ? classes.weekRowActive : '',
        week.status === 'skipped' || week.status === 'paused' ? classes.weekRowInactive : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className={classes.weekMeta}>
        <Group gap={6} wrap="nowrap">
          <Title order={4} className={classes.weekTitle}>
            {formatWeekRange(week.week_start)}
          </Title>
          {week.is_active && (
            <Badge size="xs" variant="filled" color="fresh" radius="sm">
              Now
            </Badge>
          )}
        </Group>

        <Group gap={6}>
          <Badge size="sm" variant="light" color={badge.color} radius="sm">
            {badge.label}
          </Badge>
          <Text size="xs" c="dimmed">
            {entries.length}/{recipesPerWeek} recipes
          </Text>
        </Group>

        <Text size="xs" c={countdown === 'closed' || week.closed ? 'orange' : 'dimmed'}>
          {week.closed ? 'Cutoff was ' : 'Cutoff '}
          {formatCutoff(week.cutoff_at)}
          {countdown ? ` · ${countdown}` : ''}
        </Text>

        <Group gap="xs" mt={4}>
          <Button
            size="compact-xs"
            variant="subtle"
            color={week.skipped ? 'fresh' : 'gray'}
            loading={skipPending}
            onClick={() => onToggleSkip(week)}
            disabled={week.status === 'paused'}
          >
            {week.skipped ? 'Plan this week' : 'Skip week'}
          </Button>
          {entries.length > 0 && (
            <Button
              size="compact-xs"
              variant="subtle"
              component={Link}
              to={`/basket?week=${week.week_start}`}
              leftSection={<IconBasket size={14} />}
            >
              Basket
            </Button>
          )}
        </Group>
      </div>

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
        {editable && remaining > 0 && (
          <AddTile weekStart={week.week_start} remaining={remaining} />
        )}
        {!editable && entries.length === 0 && (
          <div className={classes.emptyHint}>
            {week.status === 'skipped' ? 'Skipped — no shop this week.' : 'Paused.'}
          </div>
        )}
      </div>
    </Box>
  )
}

export default function HomePage() {
  const { data: schedule, isLoading, isError, error } = useSchedule()
  const { getWeekRecipes, removeRecipeFromWeek } = useWeeklyPlan()
  const setSkipped = useSetWeekSkipped()
  const updateSettings = useUpdateScheduleSettings()

  const settings = schedule?.settings
  const paused = Boolean(settings?.paused)
  const recipesPerWeek = settings?.recipes_per_week ?? DEFAULT_RECIPES_PER_WEEK

  return (
    <Stack gap="lg" className={classes.pageStack}>
      <Group justify="space-between" align="flex-end" wrap="nowrap">
        <div>
          <Group gap="xs">
            <IconCalendarWeek size={28} color="var(--mantine-color-fresh-7)" />
            <Title order={2}>Your shops</Title>
          </Group>
          {settings ? (
            <Text c="dimmed" size="sm">
              {cadenceLabel(settings.cadence_weeks)} · recipes settled by{' '}
              {settings.cutoff_days_before === 0
                ? `the start of the week, ${settings.cutoff_time}`
                : `${settings.cutoff_days_before} ${
                    settings.cutoff_days_before === 1 ? 'day' : 'days'
                  } before, ${settings.cutoff_time}`}
            </Text>
          ) : (
            <Skeleton height={16} width={280} mt={6} />
          )}
        </div>
        <Group gap="xs" wrap="nowrap">
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
      </Group>

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

      {isError ? (
        <Alert color="red" title="Couldn't load your schedule" icon={<IconAlertCircle size={18} />}>
          {error?.message ?? 'Please check the backend is running and try again.'}
        </Alert>
      ) : isLoading ? (
        <Group justify="center" py="xl">
          <Loader color="fresh" />
        </Group>
      ) : (
        <Stack gap="md">
          {schedule.weeks.map((week) => (
            <WeekRow
              key={week.week_start}
              week={week}
              entries={getWeekRecipes(week.week_start)}
              recipesPerWeek={recipesPerWeek}
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
