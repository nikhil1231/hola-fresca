import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Divider,
  Group,
  Loader,
  Stack,
  Text,
  Title,
} from '@mantine/core'
import { IconAlertCircle, IconChevronUp, IconHistory } from '@tabler/icons-react'

import { RECIPE_PLACEHOLDER_IMAGE } from '../constants/images.js'
import { formatWeekStart, MAX_PAST_WEEKS, useSchedule } from '../hooks/useSchedule.js'
import { formatProteinModifier, useWeeklyPlan } from '../hooks/useWeeklyPlan.js'
import {
  formatHeld,
  useCooked,
  usePantry,
  useSetCooked,
  useSetPantryItem,
} from '../hooks/usePastRecipes.js'
import PageHeader from '../components/PageHeader.jsx'
import classes from './PastRecipesPage.module.css'

const PAST_WEEKS_STEP = 4

function weekBadge(week, shopped) {
  if (week.skipped) return { label: 'Skipped', color: 'orange' }
  if (!week.complete) return { label: 'This week', color: 'fresh' }
  if (!shopped) return { label: 'Not shopped', color: 'gray' }
  return { label: 'Shopped', color: 'fresh' }
}

function RecipeRow({ entry, weekStart, cooked, marked, pending, onToggle }) {
  const { recipe } = entry
  const modifier = formatProteinModifier(entry.protein)
  return (
    <Group className={classes.recipeRow} gap="sm" wrap="nowrap">
      <Link to={`/recipes/${recipe.id}?week=${weekStart}`} className={classes.recipeMain}>
        <img
          className={classes.recipeThumb}
          src={recipe.image_url || RECIPE_PLACEHOLDER_IMAGE}
          alt=""
          loading="lazy"
        />
        <div>
          <Text size="sm" fw={600} className={classes.recipeName}>
            {recipe.name}
          </Text>
          <Text size="xs" c="dimmed">
            {entry.portions} portions
            {modifier ? ` · ${modifier}` : ''}
          </Text>
        </div>
      </Link>
      <Checkbox
        checked={cooked}
        disabled={pending}
        onChange={(event) => onToggle(event.currentTarget.checked)}
        label="Cooked"
        // The assumption and the statement read differently on purpose: a
        // faded label is the model guessing, a full one is your answer.
        className={marked ? classes.checkMarked : classes.checkAssumed}
        aria-label={`${recipe.name}: cooked`}
      />
    </Group>
  )
}

function WeekSection({ week, entries, cookedWeek, onToggle, pendingKey }) {
  const shopped = Boolean(cookedWeek?.shopped)
  const badge = weekBadge(week, shopped)
  const flags = useMemo(() => {
    const map = new Map()
    for (const recipe of cookedWeek?.recipes ?? []) map.set(recipe.recipe_id, recipe)
    return map
  }, [cookedWeek])

  if (entries.length === 0) return null
  return (
    <Box className={classes.weekSection}>
      <Group gap={8} className={classes.weekHeading}>
        <Title order={4}>{formatWeekStart(week.week_start)}</Title>
        <Badge size="sm" variant="light" color={badge.color} radius="sm">
          {badge.label}
        </Badge>
      </Group>
      {!shopped && week.complete && !week.skipped && (
        <Text size="xs" c="dimmed">
          Never pushed to a cart, so nothing here counts as cooked.
        </Text>
      )}
      <Stack gap={4} mt="xs">
        {entries.map((entry) => {
          const flag = flags.get(entry.recipe.id)
          return (
            <RecipeRow
              key={entry.recipe.id}
              entry={entry}
              weekStart={week.week_start}
              cooked={flag?.cooked ?? false}
              marked={flag?.marked ?? false}
              pending={pendingKey === `${week.week_start}:${entry.recipe.id}`}
              onToggle={(cooked) => onToggle(week.week_start, entry.recipe.id, cooked)}
            />
          )
        })}
      </Stack>
    </Box>
  )
}

function CupboardItem({ item, onSet, pending }) {
  return (
    <Group className={classes.cupboardRow} gap="sm" wrap="nowrap">
      <div className={classes.cupboardMain}>
        <Text size="sm" fw={600}>
          {item.name}
        </Text>
        <Text size="xs" c="dimmed">
          {formatHeld(item)} · from the shop of {formatWeekStart(item.week_start)}
        </Text>
      </div>
      <Group gap={6} wrap="nowrap">
        <Button
          size="compact-xs"
          variant="subtle"
          color="fresh"
          disabled={pending}
          onClick={() => onSet(item.ingredient_key, true)}
        >
          Still there
        </Button>
        <Button
          size="compact-xs"
          variant="subtle"
          color="red"
          disabled={pending}
          onClick={() => onSet(item.ingredient_key, false)}
        >
          Ran out
        </Button>
      </Group>
    </Group>
  )
}

export default function PastRecipesPage() {
  const [pastWeeks, setPastWeeks] = useState(PAST_WEEKS_STEP)
  const { data: schedule, isError, error, isFetching, isPaused } = useSchedule(pastWeeks)
  const { getWeekRecipes } = useWeeklyPlan()
  const setCooked = useSetCooked()
  const setPantryItem = useSetPantryItem()
  const pantry = usePantry()

  const pastList = schedule?.past_weeks ?? []
  const weekStarts = useMemo(
    () => pastList.map((week) => week.week_start),
    [pastList],
  )
  const cooked = useCooked(weekStarts)
  const cookedByWeek = useMemo(() => {
    const map = new Map()
    for (const week of cooked.data?.weeks ?? []) map.set(week.week_start, week)
    return map
  }, [cooked.data])

  const expanding = isFetching && pastList.length < pastWeeks
  const pendingKey = setCooked.isPending
    ? `${setCooked.variables?.weekStart}:${setCooked.variables?.recipeId}`
    : null

  const onToggle = (weekStart, recipeId, value) =>
    setCooked.mutate({ weekStart, recipeId, cooked: value })
  const onSetItem = (ingredientKey, present) =>
    setPantryItem.mutate({ ingredientKey, present })

  const weeksWithRecipes = pastList
    .slice()
    .reverse()
    .filter((week) => getWeekRecipes(week.week_start).length > 0)

  return (
    <Stack gap={{ base: 'lg', sm: 'xl' }}>
      <PageHeader
        title="Past recipes"
        description="A recipe counts as cooked once its shopped-for week ends. Untick anything that didn't get made and its ingredients go back in the cupboard."
        icon={<IconHistory size={22} />}
      />

      {(setCooked.error || setPantryItem.error) && (
        <Alert color="red" icon={<IconAlertCircle size={18} />}>
          {(setCooked.error ?? setPantryItem.error).message}
        </Alert>
      )}

      {isError ? (
        <Alert color="red" title="Couldn't load your history" icon={<IconAlertCircle size={18} />}>
          {error?.message ?? 'Please check the backend is running and try again.'}
        </Alert>
      ) : isPaused ? (
        <Alert color="orange" title="Can't reach the backend" icon={<IconAlertCircle size={18} />}>
          Your history will load by itself once the connection is back.
        </Alert>
      ) : !schedule ? (
        <Group justify="center" py="xl">
          <Loader color="fresh" />
        </Group>
      ) : (
        <Stack gap="md">
          {weeksWithRecipes.length === 0 ? (
            <Box className={classes.emptyState}>
              <Text fw={800}>No shops behind you yet</Text>
              <Text size="sm" c="dimmed">
                Finished weeks land here once their baskets have been pushed.
              </Text>
            </Box>
          ) : (
            weeksWithRecipes.map((week) => (
              <WeekSection
                key={week.week_start}
                week={week}
                entries={getWeekRecipes(week.week_start)}
                cookedWeek={cookedByWeek.get(week.week_start)}
                onToggle={onToggle}
                pendingKey={pendingKey}
              />
            ))
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
                  setPastWeeks((count) => Math.min(count + PAST_WEEKS_STEP, MAX_PAST_WEEKS))
                }
              >
                Show {PAST_WEEKS_STEP} earlier weeks
              </Button>
            </Group>
          )}

          <Divider labelPosition="center" label="In the cupboard" />

          {pantry.isError ? (
            <Alert color="red" icon={<IconAlertCircle size={18} />}>
              Couldn&apos;t load the cupboard: {pantry.error?.message}
            </Alert>
          ) : (pantry.data?.items ?? []).length === 0 ? (
            <Box className={classes.emptyState}>
              <Text fw={800}>Nothing carried over</Text>
              <Text size="sm" c="dimmed">
                Leftovers that keep — rice, tins, spices, frozen — land here after
                a basket is pushed, and come off the next shop.
              </Text>
            </Box>
          ) : (
            <Stack gap={4}>
              {pantry.data.items.map((item) => (
                <CupboardItem
                  key={item.ingredient_key}
                  item={item}
                  pending={setPantryItem.isPending}
                  onSet={onSetItem}
                />
              ))}
            </Stack>
          )}
        </Stack>
      )}
    </Stack>
  )
}
