import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Avatar,
  Badge,
  Button,
  Divider,
  Group,
  Loader,
  NumberInput,
  Paper,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Switch,
  Text,
  TextInput,
  Title,
} from '@mantine/core'
import {
  IconAlertCircle,
  IconCircleCheck,
  IconDeviceFloppy,
  IconLogout,
  IconSettings,
} from '@tabler/icons-react'

import PageHeader from '../components/PageHeader.jsx'
import RetailerLoginPanel, {
  RetailerAccountStatus,
} from '../components/RetailerLoginPanel.jsx'
import { useAccount } from '../hooks/useAccount.js'
import { useCartConnection } from '../hooks/useCartConnection.js'
import {
  formatWeekRange,
  useSchedule,
  useUpdateScheduleSettings,
} from '../hooks/useSchedule.js'
import { useRetailers, useSetRetailer } from '../hooks/useRetailer.js'
import { accountInitials } from '../utils/account.js'

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
    pack_shortfall_tolerance_pct: settings.pack_shortfall_tolerance_pct ?? 10,
  }
}

/** The retailer toggle.
 *
 *  Saved on change rather than through the draft/Save pair the rest of this page
 *  uses. Switching shops is not a setting you tune alongside the others — it
 *  re-prices every basket and swaps the mapping queue underneath you — so it
 *  wants to take effect when you press it, and it has its own endpoint. */
function WhereYouShop() {
  const { data, isPending, isError, error } = useRetailers()
  const setRetailer = useSetRetailer()
  const connection = useCartConnection(data?.active ?? null)

  if (isPending) {
    return (
      <Paper withBorder radius="md" p={{ base: 'md', sm: 'lg' }}>
        <Group gap="sm">
          <Loader size="sm" color="fresh" />
          <Text size="sm" c="dimmed">
            Loading shops…
          </Text>
        </Group>
      </Paper>
    )
  }

  if (isError) {
    return (
      <Alert color="red" icon={<IconAlertCircle size={18} />}>
        Couldn't load the list of shops: {error?.message}
      </Alert>
    )
  }

  const items = data?.items ?? []
  const active = items.find((item) => item.id === data?.active)

  return (
    <Paper withBorder radius="md" p={{ base: 'md', sm: 'lg' }}>
      <Stack gap="md">
        <div>
          <Title order={4}>Where you shop</Title>
          <Text size="sm" c="dimmed">
            Prices, pack sizes and ingredient mappings all come from the shop you pick here.
          </Text>
        </div>

        <SegmentedControl
          fullWidth
          color="fresh"
          value={data?.active ?? ''}
          disabled={setRetailer.isPending}
          onChange={(value) => setRetailer.mutate(value)}
          data={items.map((item) => ({ value: item.id, label: item.label }))}
        />

        {setRetailer.isError && (
          <Alert color="red" icon={<IconAlertCircle size={18} />}>
            {setRetailer.error?.message}
          </Alert>
        )}

        {active?.shoppable && (
          <Stack gap="sm">
            <Group gap="sm">
              <RetailerAccountStatus connection={connection} shop={active.label} />
            </Group>
            {connection.connected ? (
              <Alert color="green" variant="light" icon={<IconCircleCheck size={18} />}>
                Connected to {active.label}.
              </Alert>
            ) : (
              <RetailerLoginPanel connection={connection} shop={active.label} />
            )}
            {(connection.connected || connection.logout.isPending) && (
              <RetailerLogout key={active.id} retailer={active} connection={connection} />
            )}
          </Stack>
        )}

        <Text size="xs" c="dimmed">
          {active?.shoppable
            ? 'Baskets can be sent straight to this shop\u2019s trolley.'
            : 'This shop is priced and planned here, but has no trolley to send a basket to \u2014 ' +
              'the basket page gives you a list to shop from instead.'}
        </Text>

        <Text size="xs" c="dimmed">
          Every shop keeps its own approved ingredient mappings, so an ingredient reviewed at one
          is not automatically reviewed at the other.
        </Text>
      </Stack>
    </Paper>
  )
}

function RetailerLogout({ retailer, connection }) {
  const { disconnect, logout } = connection

  return (
    <Stack gap="xs" align="flex-start">
      <Button
        variant="default"
        leftSection={<IconLogout size={16} />}
        loading={logout.isPending}
        onClick={disconnect}
      >
        Log out of {retailer.label}
      </Button>

      {logout.isSuccess && (
        <Text size="xs" c="dimmed">
          Logged out of {retailer.label}.
        </Text>
      )}
      {logout.isError && (
        <Alert color="red" icon={<IconAlertCircle size={18} />}>
          {logout.error?.message}
        </Alert>
      )}
    </Stack>
  )
}

function AccountCard() {
  const { data: account, isPending, isError, error } = useAccount()

  return (
    <Paper withBorder radius="md" p={{ base: 'md', sm: 'lg' }}>
      <Stack gap="md">
        <Title order={4}>Account</Title>

        {isPending ? (
          <Group gap="sm">
            <Loader size="sm" color="fresh" />
            <Text size="sm" c="dimmed">Loading account…</Text>
          </Group>
        ) : isError ? (
          <Alert color="red" icon={<IconAlertCircle size={18} />}>
            Couldn't load your account: {error?.message}
          </Alert>
        ) : (
          <Group justify="space-between" align="center" wrap="wrap" gap="lg">
            <Group gap="md" wrap="nowrap">
              <Avatar color="fresh" variant="light" size={48}>
                {accountInitials(account)}
              </Avatar>
              <Stack gap={2}>
                <Text fw={700}>{account?.name || 'Name unavailable'}</Text>
                <Text size="sm" c="dimmed">{account?.email || 'Email unavailable'}</Text>
              </Stack>
            </Group>
            <Button
              component="a"
              href={account?.logout_url ?? undefined}
              variant="default"
              leftSection={<IconLogout size={16} />}
              disabled={!account?.logout_url}
            >
              Log out
            </Button>
          </Group>
        )}

        {account && !account.access_authenticated && (
          <Text size="xs" c="dimmed">
            Local mock identity. Production uses your Google profile through Cloudflare Access,
            where sign-out is also available.
          </Text>
        )}
      </Stack>
    </Paper>
  )
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

  const pageHeader = (
    <PageHeader
      title="Settings"
      description="Where you shop, how often, and when each week's recipes have to be settled."
      icon={<IconSettings size={22} />}
    />
  )

  if (isError) {
    return (
      <Stack gap="xl" maw={720} w="100%" mx="auto">
        {pageHeader}
        <AccountCard />
        <Alert color="red" title="Couldn't load settings" icon={<IconAlertCircle size={18} />}>
          {error?.message ?? 'Please check the backend is running and try again.'}
        </Alert>
      </Stack>
    )
  }

  if (isLoading || !draft) {
    return (
      <Stack gap="xl" maw={720} w="100%" mx="auto">
        {pageHeader}
        <AccountCard />
        <Group justify="center" py="xl">
          <Loader color="fresh" />
        </Group>
      </Stack>
    )
  }

  return (
    <Stack gap="xl" maw={720} w="100%" mx="auto">
      {pageHeader}

      <AccountCard />

      {updateSettings.error && (
        <Alert color="red" icon={<IconAlertCircle size={18} />}>
          {updateSettings.error.message}
        </Alert>
      )}

      <WhereYouShop />

      <Paper withBorder radius="md" p={{ base: 'md', sm: 'lg' }}>
        <Stack gap="md">
          <Title order={4}>Shopping rhythm</Title>

          <SimpleGrid cols={{ base: 1, xs: 2 }} spacing="md" verticalSpacing="sm">
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
          </SimpleGrid>

          <Divider />

          <Title order={4}>Cutoff</Title>
          <Text size="sm" c="dimmed" mt={-8}>
            After this moment a week stops being the one you're planning, and
            attention moves to the next shop.
          </Text>

          <SimpleGrid cols={{ base: 1, xs: 2 }} spacing="md" verticalSpacing="sm">
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
          </SimpleGrid>

          <Divider />

          <Title order={4}>Defaults</Title>

          <SimpleGrid cols={{ base: 1, xs: 3 }} spacing="md" verticalSpacing="sm">
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
          </SimpleGrid>

          <Divider />

          <Switch
            checked={draft.paused}
            onChange={(event) => set('paused', event.currentTarget.checked)}
            color="fresh"
            label="Pause the schedule"
            description="No week is planned and no cutoff runs until you resume."
          />

        </Stack>
      </Paper>

      <Paper withBorder radius="md" p={{ base: 'md', sm: 'lg' }}>
        <Stack gap="md">
          <Title order={4}>Basket choices</Title>
          <NumberInput
            label="Maximum pack shortfall"
            description="How much less than the recipes need a cheaper pack may contain"
            min={0}
            max={25}
            step={1}
            decimalScale={1}
            suffix="%"
            value={draft.pack_shortfall_tolerance_pct}
            onChange={(value) => set('pack_shortfall_tolerance_pct', Number(value) || 0)}
          />
          <Text size="xs" c="dimmed">
            This permits buying slightly less to avoid another pack. Set it to 0% to require
            every suggestion to cover the full quantity.
          </Text>
        </Stack>
      </Paper>

      <Group justify="flex-end" gap="sm">
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

      <Paper withBorder radius="md" p={{ base: 'md', sm: 'lg' }}>
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
