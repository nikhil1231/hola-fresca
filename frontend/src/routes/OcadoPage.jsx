import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Loader,
  PasswordInput,
  Stack,
  Tabs,
  Text,
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
import { useWeekPackChoices } from '../hooks/useWeekPackChoices.js'
import { formatWeekLabel, toPlannerSelections, useWeeklyPlan } from '../hooks/useWeeklyPlan.js'
import {
  useOcadoBasket,
  useOcadoLogin,
  useOcadoOtp,
  useOcadoPush,
  useOcadoReserve,
  useOcadoSessionRefresh,
  useOcadoSlots,
  useOcadoStatus,
} from '../hooks/useOcadoQueries.js'
import classes from './OcadoPage.module.css'

const money = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' })

function formatMoney(value) {
  return value == null ? '-' : money.format(value)
}

// Slot times arrive as ISO strings carrying the delivery region's own UTC offset
// ("2026-07-31T16:00:00+01:00"), so the wall-clock time is already correct in
// the string. Reading it out directly beats parsing to a Date, which would
// re-render it in whatever timezone the browser happens to be in.
function formatTime(value) {
  if (!value) return null
  const match = /T(\d{2}:\d{2})/.exec(value)
  return match ? match[1] : value
}

function formatSlotWindow(slot) {
  const start = formatTime(slot.start)
  if (!start) return 'Slot'
  const end = formatTime(slot.end)
  return end ? `${start} – ${end}` : start
}

// cart-view nests its lines two levels down, under the checkout group that owns
// them - the same path app/ocado/sync.py reads.
function summariseCart(raw) {
  if (!raw || typeof raw !== 'object') return null
  const items = (raw.checkoutGroups?.assignedCheckoutGroups ?? []).flatMap((group) =>
    (group.itemGroups ?? []).flatMap((itemGroup) => itemGroup.items ?? []),
  )
  const checkout = raw.activeCheckoutGroup ?? {}
  const spend = raw.totals?.itemPriceAfterPromos
  return {
    lines: items.length,
    units: items.reduce((total, item) => total + (item.quantity ?? 0), 0),
    spend: spend ? Number(spend.amount) : null,
    threshold: checkout.minimumCheckoutThreshold
      ? Number(checkout.minimumCheckoutThreshold.amount)
      : null,
    canCheckout: checkout.canCheckout ?? false,
    restrictions: checkout.checkoutRestrictions ?? [],
  }
}

const RESTRICTION_LABELS = {
  NOT_REACHED_THRESHOLD: 'below minimum',
  MISSING_SLOT: 'no slot booked',
}

function restrictionLabel(code) {
  return RESTRICTION_LABELS[code] ?? code.toLowerCase().replace(/_/g, ' ')
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

function statusLabel(status) {
  if (status === 'ready') return 'ready'
  if (status === 'awaiting_otp') return 'awaiting OTP'
  return 'logged out'
}

function parseSlotDate(value) {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function slotSortValue(slot) {
  return parseSlotDate(slot.start)?.getTime() ?? Number.MAX_SAFE_INTEGER
}

function groupSlotsByDay(slots) {
  const sortedSlots = [...slots].sort((a, b) => slotSortValue(a) - slotSortValue(b))
  const groups = sortedSlots.reduce((acc, slot) => {
    const day = slot.day ?? 'Later'
    acc.set(day, [...(acc.get(day) ?? []), slot])
    return acc
  }, new Map())
  return [...groups.entries()].map(([day, daySlots]) => ({
    day,
    label: formatDay(day),
    slots: daySlots,
  }))
}

function SlotTabs({ days, reserve, onReserved }) {
  const [activeDay, setActiveDay] = useState(null)

  useEffect(() => {
    if (!days.length) {
      setActiveDay(null)
      return
    }
    setActiveDay((current) => (current && days.some((day) => day.day === current) ? current : days[0].day))
  }, [days])

  if (!days.length) {
    return null
  }

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
                  <Text size="xs" lh={1.1}>{formatMoney(slot.price)}{slot.eco ? ' eco' : ''}</Text>
                </Stack>
              </Button>
            ))}
          </div>
        </Tabs.Panel>
      ))}
    </Tabs>
  )
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

function formatDelta(value) {
  const rounded = Math.round((value ?? 0) * 100) / 100
  if (rounded === 0) return 'same price'
  return `${rounded > 0 ? '+' : '-'}${money.format(Math.abs(rounded))}`
}

// A drop is only useful in the ingredient's terms: "Sesame seeds" says what the
// week is now missing, where the brand name of the pack Ocado refused does not.
function dropText(line) {
  const product = line.name ?? line.sku
  const short =
    line.wanted != null && line.got != null && line.got > 0
      ? ` (${line.got} of ${line.wanted})`
      : ''
  const why = line.reason ? ` - ${line.reason}` : ''
  return line.ingredient ? `${line.ingredient}: ${product}${short}${why}` : `${product}${short}${why}`
}

function PushSummary({ result }) {
  if (!result) return null
  const swaps = result.swaps ?? []
  const soldOut = result.sold_out ?? []
  return (
    <Alert color={result.dropped.length ? 'yellow' : 'teal'} variant="light" icon={<IconCheck size={18} />}>
      <Group gap="xs">
        <Badge color="teal" variant="light">{result.applied.length} applied</Badge>
        <Badge color={swaps.length ? 'blue' : 'gray'} variant="light">
          {swaps.length} swapped
        </Badge>
        <Badge color={result.dropped.length ? 'yellow' : 'gray'} variant="light">
          {result.dropped.length} dropped
        </Badge>
        <Badge color={result.unmapped.length ? 'red' : 'gray'} variant="light">
          {result.unmapped.length} unmapped
        </Badge>
      </Group>
      {swaps.length > 0 && (
        <Stack gap={2} mt="xs">
          {swaps.map((swap) => (
            <Text size="sm" key={swap.ingredient_key}>
              {swap.ingredient}: {swap.from_products.join(', ')} out of stock →{' '}
              {swap.to_products.join(', ')} ({formatDelta(swap.cost_delta)})
              {swap.tier_changed ? ', closest match unavailable' : ''}
            </Text>
          ))}
        </Stack>
      )}
      {result.dropped.length > 0 && (
        <Text size="sm" mt="xs">
          Ocado would not take: {result.dropped.map(dropText).join('; ')}
        </Text>
      )}
      {soldOut.length > 0 && (
        <Text size="sm" mt="xs">
          Nothing in stock for: {soldOut.join(', ')}
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
  const { packOverrides } = useWeekPackChoices(upcomingWeekStart)
  const entries = getWeekRecipes(upcomingWeekStart)
  const selections = useMemo(() => toPlannerSelections(entries), [entries])
  const planner = usePlannerBasket(selections, packOverrides)
  const status = useOcadoStatus()
  const login = useOcadoLogin()
  const sessionRefresh = useOcadoSessionRefresh()
  const reconnectAttempted = useRef(false)
  const otp = useOcadoOtp()
  const push = useOcadoPush()
  const basket = useOcadoBasket({ enabled: status.data?.status === 'ready' })
  const [otpCode, setOtpCode] = useState('')
  const [reservation, setReservation] = useState(null)
  const slots = useOcadoSlots(undefined, { enabled: status.data?.status === 'ready' })
  const reserve = useOcadoReserve()
  const countdown = useCountdown(extractExpiry(reservation?.raw))
  const cartSummary = useMemo(() => summariseCart(basket.data?.raw), [basket.data?.raw])
  const slotDays = useMemo(() => groupSlotsByDay(slots.data?.items ?? []), [slots.data?.items])
  const onlineLines =
    planner.data?.lines?.filter((line) => !line.external && !ownedItemKeySet.has(line.key)) ?? []
  const orderCost = useMemo(
    () =>
      (planner.data?.lines ?? [])
        .filter((line) => !ownedItemKeySet.has(line.key))
        .reduce((total, line) => total + (line.cost ?? 0), 0),
    [planner.data?.lines, ownedItemKeySet],
  )

  const connected = status.data?.status === 'ready'
  const awaitingOtp = status.data?.status === 'awaiting_otp'
  const reconnecting = sessionRefresh.isPending

  // A saved session usually just needs waking up, so try that on arrival rather
  // than making you press a button for it. Once per mount, and only the rungs
  // that need nothing from you - /session/refresh stops before the password
  // step, so this can never trigger an OTP email on its own.
  useEffect(() => {
    if (status.data?.status !== 'logged_out') return
    if (reconnectAttempted.current) return
    reconnectAttempted.current = true
    sessionRefresh.mutate()
  }, [status.data?.status, sessionRefresh])

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
          {/* Nothing to say while the session is healthy - the header badge
              already carries the state, and a login button there just invites
              you to fix something that is not broken. */}
          {!connected && (
            <Box className={classes.panel}>
              <Stack gap="sm">
                <Group justify="space-between">
                  <Title order={3}>{awaitingOtp ? 'Check your email' : 'Connect to Ocado'}</Title>
                  {(status.isLoading || reconnecting) && <Loader size="sm" />}
                </Group>

                {awaitingOtp ? (
                  <>
                    <Text size="sm" c="dimmed">
                      Ocado sent a verification code. Enter it to finish signing in.
                    </Text>
                    <Group align="flex-end">
                      <PasswordInput
                        label="Verification code"
                        value={otpCode}
                        onChange={(event) => setOtpCode(event.currentTarget.value)}
                        onKeyDown={(event) => {
                          if (event.key === 'Enter' && otpCode.trim()) otp.mutate(otpCode)
                        }}
                        flex={1}
                      />
                      <Button
                        loading={otp.isPending}
                        disabled={!otpCode.trim()}
                        onClick={() => otp.mutate(otpCode)}
                      >
                        Submit
                      </Button>
                    </Group>
                  </>
                ) : (
                  <>
                    <Text size="sm" c="dimmed">
                      {reconnecting
                        ? 'Reusing your saved session…'
                        : 'Signing in will email you a verification code.'}
                    </Text>
                    <Button
                      leftSection={<IconLogin size={16} />}
                      loading={login.isPending || reconnecting}
                      onClick={() => login.mutate()}
                    >
                      Sign in to Ocado
                    </Button>
                  </>
                )}

                {(login.error || otp.error || status.error) && (
                  <Alert color="red" icon={<IconAlertCircle size={18} />}>
                    {login.error?.message ?? otp.error?.message ?? status.error?.message}
                  </Alert>
                )}
              </Stack>
            </Box>
          )}

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
                    onClick={() => push.mutate({ selections, ownedItemKeys, packOverrides })}
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
            {cartSummary ? (
              <Stack gap="sm" mt="sm">
                <Group gap="lg">
                  <div>
                    <Text size="xs" c="dimmed" tt="uppercase">Lines</Text>
                    <Text fw={700}>{cartSummary.lines}</Text>
                  </div>
                  <div>
                    <Text size="xs" c="dimmed" tt="uppercase">Items</Text>
                    <Text fw={700}>{cartSummary.units}</Text>
                  </div>
                  <div>
                    <Text size="xs" c="dimmed" tt="uppercase">Spend</Text>
                    <Text fw={700}>{formatMoney(cartSummary.spend)}</Text>
                  </div>
                  {cartSummary.threshold != null && (
                    <div>
                      <Text size="xs" c="dimmed" tt="uppercase">Minimum</Text>
                      <Text fw={700}>{formatMoney(cartSummary.threshold)}</Text>
                    </div>
                  )}
                </Group>
                <Group gap="xs">
                  <Badge color={cartSummary.canCheckout ? 'teal' : 'gray'} variant="light">
                    {cartSummary.canCheckout ? 'ready to check out' : 'not ready'}
                  </Badge>
                  {cartSummary.restrictions.map((code) => (
                    <Badge key={code} color="yellow" variant="light">
                      {restrictionLabel(code)}
                    </Badge>
                  ))}
                </Group>
              </Stack>
            ) : (
              <Text size="sm" c="dimmed" mt="sm">
                Current Ocado cart data is available after login.
              </Text>
            )}
            <Text size="xs" c="dimmed" mt="sm">
              The final order still happens on Ocado.
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
              <SlotTabs days={slotDays} reserve={reserve} onReserved={setReservation} />
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
