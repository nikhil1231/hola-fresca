import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Button,
  Divider,
  Group,
  Loader,
  NumberInput,
  Paper,
  Select,
  Stack,
  Switch,
  Text,
  TextInput,
  Title,
} from '@mantine/core'
import { IconAlertCircle, IconDeviceFloppy, IconSettings } from '@tabler/icons-react'

import {
  formatWeekRange,
  useSchedule,
  useUpdateScheduleSettings,
} from '../hooks/useSchedule.js'

const CADENCE_OPTIONS = [
  { value: '1', label: 'Every week' },
  { value: '2', label: 'Every 2 weeks' },
  { value: '3', label: 'Every 3 weeks' },
  { value: '4', label: 'Every 4 weeks' },
]

const weekdayFormat = new Intl.DateTimeFormat(undefined, { weekday: 'long' })

// The cutoff is stored as "this many days before the week starts", which is what
// makes it apply to every week without a row each. It reads as a weekday, so
// that is what the picker offers.
function cutoffOptions() {
  // Any Monday will do; only the weekday names come out of it.
  const reference = new Date('2026-01-05T00:00:00')
  return Array.from({ length: 14 }, (_, daysBefore) => {
    const day = new Date(reference)
    day.setDate(day.getDate() - daysBefore)
    const weekday = weekdayFormat.format(day)
    if (daysBefore === 0) return { value: '0', label: `${weekday} — the week itself` }
    const week = daysBefore < 7 ? 'before' : 'the week before'
    return { value: String(daysBefore), label: `${weekday} ${week}` }
  })
}

// Which week the cadence counts from. Only bites when the cadence is longer than
// a week, but that is exactly when getting it wrong shows up as "why is it
// shopping on the wrong weeks".
function anchorOptions(weeks) {
  return weeks.map((week) => ({
    value: week.week_start,
    label: formatWeekRange(week.week_start),
  }))
}

function toDraft(settings) {
  return {
    cadence_weeks: settings.cadence_weeks,
    anchor_week_start: settings.anchor_week_start,
    cutoff_days_before: settings.cutoff_days_before,
    cutoff_time: settings.cutoff_time,
    paused: settings.paused,
    horizon_weeks: settings.horizon_weeks,
    recipes_per_week: settings.recipes_per_week,
    default_portions: settings.default_portions,
  }
}

export default function SettingsPage() {
  const { data: schedule, isLoading, isError, error } = useSchedule()
  const updateSettings = useUpdateScheduleSettings()
  const [draft, setDraft] = useState(null)

  const settings = schedule?.settings
  useEffect(() => {
    if (settings) setDraft((current) => current ?? toDraft(settings))
  }, [settings])

  const dirty = useMemo(() => {
    if (!settings || !draft) return false
    return Object.entries(draft).some(([key, value]) => settings[key] !== value)
  }, [draft, settings])

  const anchorChoices = useMemo(() => {
    if (!schedule) return []
    const options = anchorOptions(schedule.weeks)
    // The stored anchor is usually one of the shown weeks, but a cadence change
    // can move it out of the window; keep it selectable rather than blank.
    if (draft && !options.some((option) => option.value === draft.anchor_week_start)) {
      options.unshift({
        value: draft.anchor_week_start,
        label: formatWeekRange(draft.anchor_week_start),
      })
    }
    return options
  }, [draft, schedule])

  function set(key, value) {
    setDraft((current) => ({ ...current, [key]: value }))
  }

  if (isError) {
    return (
      <Alert color="red" title="Couldn't load settings" icon={<IconAlertCircle size={18} />}>
        {error?.message ?? 'Please check the backend is running and try again.'}
      </Alert>
    )
  }

  if (isLoading || !draft) {
    return (
      <Group justify="center" py="xl">
        <Loader color="fresh" />
      </Group>
    )
  }

  return (
    <Stack gap="lg" maw={720}>
      <div>
        <Group gap="xs">
          <IconSettings size={28} color="var(--mantine-color-fresh-7)" />
          <Title order={2}>Settings</Title>
        </Group>
        <Text c="dimmed" size="sm">
          How often you shop, and when each week's recipes have to be settled.
        </Text>
      </div>

      {updateSettings.error && (
        <Alert color="red" icon={<IconAlertCircle size={18} />}>
          {updateSettings.error.message}
        </Alert>
      )}

      <Paper withBorder radius="md" p="lg">
        <Stack gap="md">
          <Title order={4}>Shopping rhythm</Title>

          <Group grow align="flex-start">
            <Select
              label="Cadence"
              description="How often a shop happens"
              data={CADENCE_OPTIONS}
              value={String(draft.cadence_weeks)}
              onChange={(value) => value && set('cadence_weeks', Number(value))}
              allowDeselect={false}
            />
            <Select
              label="Counting from"
              description="Which week the cadence lands on"
              data={anchorChoices}
              value={draft.anchor_week_start}
              onChange={(value) => value && set('anchor_week_start', value)}
              allowDeselect={false}
              disabled={draft.cadence_weeks === 1}
            />
          </Group>

          <Divider />

          <Title order={4}>Cutoff</Title>
          <Text size="sm" c="dimmed" mt={-8}>
            After this moment a week stops being the one you're planning, and
            attention moves to the next shop.
          </Text>

          <Group grow align="flex-start">
            <Select
              label="Recipes settled by"
              data={cutoffOptions()}
              value={String(draft.cutoff_days_before)}
              onChange={(value) => value && set('cutoff_days_before', Number(value))}
              allowDeselect={false}
              searchable
            />
            <TextInput
              label="At"
              type="time"
              value={draft.cutoff_time}
              onChange={(event) => set('cutoff_time', event.currentTarget.value)}
            />
          </Group>

          <Divider />

          <Title order={4}>Defaults</Title>

          <Group grow align="flex-start">
            <NumberInput
              label="Weeks shown"
              description="How far ahead you can plan"
              min={1}
              max={12}
              value={draft.horizon_weeks}
              onChange={(value) => set('horizon_weeks', Number(value) || 1)}
            />
            <NumberInput
              label="Recipes per week"
              min={1}
              max={14}
              value={draft.recipes_per_week}
              onChange={(value) => set('recipes_per_week', Number(value) || 1)}
            />
            <NumberInput
              label="Portions"
              description="Per recipe, by default"
              min={1}
              max={8}
              value={draft.default_portions}
              onChange={(value) => set('default_portions', Number(value) || 1)}
            />
          </Group>

          <Divider />

          <Switch
            checked={draft.paused}
            onChange={(event) => set('paused', event.currentTarget.checked)}
            color="fresh"
            label="Pause the schedule"
            description="No week is planned and no cutoff runs until you resume."
          />

          <Group justify="flex-end" gap="sm" mt="xs">
            <Button
              variant="default"
              disabled={!dirty || updateSettings.isPending}
              onClick={() => setDraft(toDraft(settings))}
            >
              Reset
            </Button>
            <Button
              color="fresh"
              leftSection={<IconDeviceFloppy size={16} />}
              loading={updateSettings.isPending}
              disabled={!dirty}
              onClick={() =>
                updateSettings.mutate(draft, {
                  onSuccess: (next) => setDraft(toDraft(next.settings)),
                })
              }
            >
              Save
            </Button>
          </Group>
        </Stack>
      </Paper>

      <Paper withBorder radius="md" p="lg">
        <Title order={4} mb="xs">
          Next shops
        </Title>
        <Text size="sm" c="dimmed" mb="sm">
          {dirty ? 'Saved settings — save to see these change.' : 'As currently scheduled.'}
        </Text>
        <Group gap="xs">
          {schedule.weeks.map((week) => (
            <Badge
              key={week.week_start}
              variant={week.is_active ? 'filled' : 'light'}
              color={week.status === 'skipped' || week.status === 'paused' ? 'gray' : 'fresh'}
              radius="sm"
            >
              {formatWeekRange(week.week_start)}
            </Badge>
          ))}
        </Group>
      </Paper>
    </Stack>
  )
}
