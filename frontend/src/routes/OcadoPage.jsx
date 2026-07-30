import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Loader,
  PasswordInput,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core'
import {
  IconAlertCircle,
  IconBasketUp,
  IconCalendarClock,
  IconCheck,
  IconClock,
  IconLogin,
  IconRefresh,
} from '@tabler/icons-react'

import { usePlannerBasket } from '../hooks/useRecipeQueries.js'
import { useOwnedBasketItems } from '../hooks/useOwnedBasketItems.js'
import { formatWeekLabel, toPlannerSelections, useWeeklyPlan } from '../hooks/useWeeklyPlan.js'
import {
  useOcadoBasket,
  useOcadoLogin,
  useOcadoOtp,
  useOcadoPush,
  useOcadoReserve,
  useOcadoSlots,
  useOcadoStatus,
} from '../hooks/useOcadoQueries.js'
import classes from './OcadoPage.module.css'

const money = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' })

function formatMoney(value) {
  return value == null ? '-' : money.format(value)
}

function statusLabel(status) {
  if (status === 'ready') return 'ready'
  if (status === 'awaiting_otp') return 'awaiting OTP'
  return 'logged out'
}

function groupSlots(slots) {
  return slots.reduce((groups, slot) => {
    const day = slot.day ?? 'Later'
    groups[day] = groups[day] ?? []
    groups[day].push(slot)
    return groups
  }, {})
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
  const minutes = Math.floor(seconds / 60)
  return `${minutes}:${String(seconds % 60).padStart(2, '0')}`
}

function PushSummary({ result }) {
  if (!result) return null
  return (
    <Alert color={result.dropped.length ? 'yellow' : 'teal'} variant="light" icon={<IconCheck size={18} />}>
      <Group gap="xs">
        <Badge color="teal" variant="light">{result.applied.length} applied</Badge>
        <Badge color={result.dropped.length ? 'yellow' : 'gray'} variant="light">
          {result.dropped.length} dropped
        </Badge>
        <Badge color={result.unmapped.length ? 'red' : 'gray'} variant="light">
          {result.unmapped.length} unmapped
        </Badge>
      </Group>
      {result.dropped.length > 0 && (
        <Text size="sm" mt="xs">
          Dropped: {result.dropped.map((line) => line.name ?? line.sku).join(', ')}
        </Text>
      )}
      {result.unmapped.length > 0 && (
        <Text size="sm" mt="xs">
          Not sent to Ocado: {result.unmapped.join(', ')}
        </Text>
      )}
    </Alert>
  )
}

export default function OcadoPage() {
  const { upcomingWeekStart, getWeekRecipes } = useWeeklyPlan()
  const { ownedItemKeys, ownedItemKeySet } = useOwnedBasketItems(upcomingWeekStart)
  const entries = getWeekRecipes(upcomingWeekStart)
  const selections = useMemo(() => toPlannerSelections(entries), [entries])
  const planner = usePlannerBasket(selections)
  const status = useOcadoStatus()
  const login = useOcadoLogin()
  const otp = useOcadoOtp()
  const push = useOcadoPush()
  const basket = useOcadoBasket({ enabled: status.data?.status === 'ready' })
  const [slotParams, setSlotParams] = useState({ ddid: '', region: '' })
  const [otpCode, setOtpCode] = useState('')
  const [reservation, setReservation] = useState(null)
  const slots = useOcadoSlots(slotParams, { enabled: status.data?.status === 'ready' })
  const reserve = useOcadoReserve()
  const countdown = useCountdown(extractExpiry(reservation?.raw))
  const groupedSlots = useMemo(() => groupSlots(slots.data?.items ?? []), [slots.data?.items])
  const onlineLines =
    planner.data?.lines?.filter((line) => !line.external && !ownedItemKeySet.has(line.key)) ?? []
  const orderCost = useMemo(
    () =>
      (planner.data?.lines ?? [])
        .filter((line) => !ownedItemKeySet.has(line.key))
        .reduce((total, line) => total + (line.cost ?? 0), 0),
    [planner.data?.lines, ownedItemKeySet],
  )

  return (
    <Stack gap="lg" className={classes.pageStack}>
      <Group justify="space-between" align="flex-end">
        <div>
          <Group gap="xs">
            <IconBasketUp size={28} className={classes.titleIcon} />
            <Title order={2}>Ocado</Title>
          </Group>
          <Text c="dimmed">
            Push {entries.length} recipes for {formatWeekLabel(upcomingWeekStart)}, then reserve a slot.
          </Text>
        </div>
        <Badge color={status.data?.status === 'ready' ? 'teal' : 'gray'} variant="light" size="lg">
          {statusLabel(status.data?.status)}
        </Badge>
      </Group>

      <Box className={classes.layout}>
        <Stack gap="lg">
          <Box className={classes.panel}>
            <Stack gap="sm">
              <Group justify="space-between">
                <Title order={3}>Login</Title>
                {status.isLoading && <Loader size="sm" />}
              </Group>
              <Button
                leftSection={<IconLogin size={16} />}
                loading={login.isPending}
                onClick={() => login.mutate()}
              >
                Start or refresh login
              </Button>
              {status.data?.status === 'awaiting_otp' && (
                <Group align="flex-end">
                  <PasswordInput
                    label="OTP"
                    value={otpCode}
                    onChange={(event) => setOtpCode(event.currentTarget.value)}
                    flex={1}
                  />
                  <Button loading={otp.isPending} onClick={() => otp.mutate(otpCode)}>
                    Submit
                  </Button>
                </Group>
              )}
              {(login.error || otp.error || status.error) && (
                <Alert color="red" icon={<IconAlertCircle size={18} />}>
                  {login.error?.message ?? otp.error?.message ?? status.error?.message}
                </Alert>
              )}
            </Stack>
          </Box>

          <Box className={classes.panel}>
            <Stack gap="sm">
              <Title order={3}>Week Basket</Title>
              {planner.isLoading ? (
                <Loader />
              ) : planner.isError ? (
                <Alert color="red" icon={<IconAlertCircle size={18} />}>
                  {planner.error?.message}
                </Alert>
              ) : (
                <>
                  <Box className={classes.statsGrid}>
                    <div className={classes.stat}>
                      <Text size="xs" c="dimmed" fw={700} tt="uppercase">Ocado lines</Text>
                      <Text fw={800}>{onlineLines.length}</Text>
                    </div>
                    <div className={classes.stat}>
                      <Text size="xs" c="dimmed" fw={700} tt="uppercase">Spend</Text>
                      <Text fw={800}>{formatMoney(orderCost)}</Text>
                    </div>
                    <div className={classes.stat}>
                      <Text size="xs" c="dimmed" fw={700} tt="uppercase">Unmapped</Text>
                      <Text fw={800}>{planner.data?.unmapped?.length ?? 0}</Text>
                    </div>
                  </Box>
                  <Button
                    leftSection={<IconBasketUp size={16} />}
                    disabled={!selections.length || status.data?.status !== 'ready'}
                    loading={push.isPending}
                    onClick={() => push.mutate({ selections, ownedItemKeys })}
                  >
                    Push basket to Ocado
                  </Button>
                  {push.error && (
                    <Alert color="red" icon={<IconAlertCircle size={18} />}>
                      {push.error.message}
                    </Alert>
                  )}
                  <PushSummary result={push.data} />
                </>
              )}
            </Stack>
          </Box>
        </Stack>

        <Stack gap="lg">
          <Box className={classes.panel}>
            <Group justify="space-between">
              <Title order={3}>Ocado Basket</Title>
              <Button
                variant="subtle"
                leftSection={<IconRefresh size={16} />}
                onClick={() => basket.refetch()}
                loading={basket.isFetching}
                disabled={status.data?.status !== 'ready'}
              >
                Refresh
              </Button>
            </Group>
            {basket.error && (
              <Alert color="red" mt="sm" icon={<IconAlertCircle size={18} />}>
                {basket.error.message}
              </Alert>
            )}
            <Text size="sm" c="dimmed" mt="sm">
              Current Ocado cart data is available after login. The final order still happens on Ocado.
            </Text>
          </Box>

          <Box className={classes.panel}>
            <Stack gap="md">
              <Group justify="space-between" align="flex-end">
                <div>
                  <Group gap="xs">
                    <IconCalendarClock size={22} className={classes.titleIcon} />
                    <Title order={3}>Delivery Slots</Title>
                  </Group>
                  {countdown && (
                    <Text size="sm" c="dimmed">
                      Held slot expires in {countdown}
                    </Text>
                  )}
                </div>
                <Button
                  variant="subtle"
                  leftSection={<IconRefresh size={16} />}
                  onClick={() => slots.refetch()}
                  loading={slots.isFetching}
                  disabled={status.data?.status !== 'ready'}
                >
                  Refresh
                </Button>
              </Group>
              <Group grow>
                <TextInput
                  label="Delivery address id"
                  value={slotParams.ddid}
                  onChange={(event) =>
                    setSlotParams((current) => ({ ...current, ddid: event.currentTarget.value }))
                  }
                />
                <TextInput
                  label="Region"
                  value={slotParams.region}
                  onChange={(event) =>
                    setSlotParams((current) => ({ ...current, region: event.currentTarget.value }))
                  }
                />
              </Group>
              {slots.error && (
                <Alert color="red" icon={<IconAlertCircle size={18} />}>
                  {slots.error.message}
                </Alert>
              )}
              {reserve.error && (
                <Alert color="red" icon={<IconAlertCircle size={18} />}>
                  {reserve.error.message}
                </Alert>
              )}
              {reservation && (
                <Alert color="teal" icon={<IconClock size={18} />}>
                  Slot reserved. Confirm the order on Ocado before the hold expires.
                </Alert>
              )}
              {Object.entries(groupedSlots).map(([day, daySlots]) => (
                <Stack key={day} gap="sm">
                  <Title order={4} className={classes.dayHeading}>{day}</Title>
                  <div className={classes.slotGrid}>
                    {daySlots.map((slot) => (
                      <Button
                        key={slot.slot_id}
                        className={classes.slotButton}
                        variant={slot.available ? 'light' : 'default'}
                        color={slot.eco ? 'green' : 'blue'}
                        disabled={!slot.available}
                        loading={reserve.isPending && reserve.variables?.slotId === slot.slot_id}
                        onClick={() =>
                          reserve.mutate(
                            { slotId: slot.slot_id, ...slotParams },
                            { onSuccess: setReservation },
                          )
                        }
                      >
                        <Stack gap={2} align="center">
                          <Text fw={700}>{slot.start ?? 'Slot'} - {slot.end ?? ''}</Text>
                          <Text size="xs">{formatMoney(slot.price)}{slot.eco ? ' / eco' : ''}</Text>
                        </Stack>
                      </Button>
                    ))}
                  </div>
                </Stack>
              ))}
              {!slots.isLoading && !slots.error && (slots.data?.items ?? []).length === 0 && (
                <Text c="dimmed">No slots loaded yet.</Text>
              )}
            </Stack>
          </Box>
        </Stack>
      </Box>
    </Stack>
  )
}
