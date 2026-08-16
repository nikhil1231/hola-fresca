import { useEffect, useMemo, useState } from 'react'
import { Alert, Box, Button, Group, Stack, Tabs, Text, Title } from '@mantine/core'
import { IconAlertCircle, IconCalendarClock, IconClock, IconRefresh } from '@tabler/icons-react'

import { useOcadoReserve, useOcadoSlots } from '../hooks/useOcadoQueries.js'
import classes from '../routes/OcadoPage.module.css'

const money = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' })

function formatMoney(value) {
  return value == null ? '-' : money.format(value)
}

function formatTime(value) {
  const match = /T(\d{2}:\d{2})/.exec(value ?? '')
  return match ? match[1] : value
}

function formatSlotWindow(slot) {
  const start = formatTime(slot.start)
  if (!start) return 'Slot'
  const end = formatTime(slot.end)
  return end ? `${start} – ${end}` : start
}

function formatDay(day) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day ?? '')) return day
  const parsed = new Date(`${day}T12:00:00`)
  if (Number.isNaN(parsed.getTime())) return day
  return parsed.toLocaleDateString(undefined, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  })
}

function groupSlotsByDay(slots) {
  const sorted = [...slots].sort((a, b) =>
    (new Date(a.start).getTime() || Number.MAX_SAFE_INTEGER)
      - (new Date(b.start).getTime() || Number.MAX_SAFE_INTEGER),
  )
  const groups = new Map()
  for (const slot of sorted) {
    const day = slot.day ?? 'Later'
    groups.set(day, [...(groups.get(day) ?? []), slot])
  }
  return [...groups.entries()].map(([day, daySlots]) => ({
    day,
    label: formatDay(day),
    slots: daySlots,
  }))
}

function extractExpiry(raw) {
  const queue = [raw]
  while (queue.length) {
    const node = queue.shift()
    if (!node || typeof node !== 'object') continue
    for (const [key, value] of Object.entries(node)) {
      if (/expiry|expire/i.test(key) && typeof value === 'string') return value
      if (value && typeof value === 'object') queue.push(value)
    }
  }
  return null
}

function useCountdown(expiry) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!expiry) return undefined
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [expiry])
  if (!expiry) return null
  const target = new Date(expiry).getTime()
  if (Number.isNaN(target)) return null
  const seconds = Math.max(0, Math.floor((target - now) / 1000))
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

function SlotTabs({ days, reserve, onReserved }) {
  const [activeDay, setActiveDay] = useState(null)

  useEffect(() => {
    if (!days.length) {
      setActiveDay(null)
      return
    }
    setActiveDay((current) =>
      current && days.some((day) => day.day === current) ? current : days[0].day,
    )
  }, [days])

  if (!days.length) return null
  return (
    <Tabs value={activeDay} onChange={setActiveDay} classNames={{ list: classes.slotTabsList }}>
      <Tabs.List>
        {days.map((day) => (
          <Tabs.Tab key={day.day} value={day.day}>
            <Stack gap={0} align="center">
              <Text span fw={700} size="sm">{day.label}</Text>
              <Text span size="xs" c="dimmed">{day.slots.length} slots</Text>
            </Stack>
          </Tabs.Tab>
        ))}
      </Tabs.List>
      {days.map((day) => (
        <Tabs.Panel key={day.day} value={day.day} pt="sm">
          <div className={classes.slotGrid}>
            {day.slots.map((slot) => (
              <Button
                key={slot.slot_id}
                className={classes.slotButton}
                variant={slot.available ? 'light' : 'default'}
                color={slot.eco ? 'green' : 'blue'}
                disabled={!slot.available}
                loading={reserve.isPending && reserve.variables?.slotId === slot.slot_id}
                onClick={() =>
                  reserve.mutate(
                    { slotId: slot.slot_id },
                    { onSuccess: onReserved },
                  )
                }
              >
                <Stack gap={0} align="center" className={classes.slotContent}>
                  <Text fw={800} size="sm" lh={1.1}>{formatSlotWindow(slot)}</Text>
                  <Text size="xs" lh={1.1}>
                    {formatMoney(slot.price)}{slot.eco ? ' eco' : ''}
                  </Text>
                </Stack>
              </Button>
            ))}
          </div>
        </Tabs.Panel>
      ))}
    </Tabs>
  )
}

// Preserved for the next checkout iteration. Deliberately not mounted by the
// combined Basket/Checkout page yet.
export default function OcadoDeliverySlots({ connected }) {
  const [reservation, setReservation] = useState(null)
  const slots = useOcadoSlots({}, { enabled: connected })
  const reserve = useOcadoReserve()
  const days = useMemo(() => groupSlotsByDay(slots.data?.items ?? []), [slots.data?.items])
  const countdown = useCountdown(extractExpiry(reservation?.raw))

  return (
    <Box className={classes.panel}>
      <Stack gap="md">
        <Group justify="space-between" align="flex-end">
          <div>
            <Group gap="xs">
              <IconCalendarClock size={22} className={classes.titleIcon} />
              <Title order={3}>Delivery Slots</Title>
            </Group>
            {countdown && <Text size="sm" c="dimmed">Held slot expires in {countdown}</Text>}
          </div>
          <Button
            variant="subtle"
            leftSection={<IconRefresh size={16} />}
            onClick={() => slots.refetch()}
            loading={slots.isFetching}
            disabled={!connected}
          >
            Refresh
          </Button>
        </Group>
        {(slots.error || reserve.error) && (
          <Alert color="red" icon={<IconAlertCircle size={18} />}>
            {slots.error?.message ?? reserve.error?.message}
          </Alert>
        )}
        {reservation && (
          <Alert color="teal" icon={<IconClock size={18} />}>
            Slot reserved. Confirm the order on Ocado before the hold expires.
          </Alert>
        )}
        <SlotTabs days={days} reserve={reserve} onReserved={setReservation} />
        {!slots.isLoading && !slots.error && (slots.data?.items ?? []).length === 0 && (
          <Text c="dimmed">No slots loaded yet.</Text>
        )}
      </Stack>
    </Box>
  )
}
