import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Loader,
  PasswordInput,
  Select,
  Stack,
  Table,
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
  IconEye,
  IconLogin,
  IconRefresh,
} from '@tabler/icons-react'

import { usePlannerBasket } from '../hooks/useRecipeQueries.js'
import { useOwnedBasketItems } from '../hooks/useOwnedBasketItems.js'
import { useWeekPackChoices } from '../hooks/useWeekPackChoices.js'
import { formatWeekLabel, toPlannerSelections, useWeeklyPlan } from '../hooks/useWeeklyPlan.js'
import {
  useOcadoAccounts,
  useOcadoBasket,
  useOcadoLogin,
  useOcadoOtp,
  useOcadoPush,
  useOcadoPushPlan,
  useOcadoReserve,
  useOcadoSessionRefresh,
  useOcadoSlots,
  useOcadoStatus,
} from '../hooks/useOcadoQueries.js'
import classes from './OcadoPage.module.css'

const money = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' })
const ACCOUNT_STORAGE_KEY = 'holafresca:ocado-account-id'

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

// A full login is slow enough - browser launch, reCAPTCHA, then the wait for
// the emailed code - that a bare spinner tells you nothing about whether it is
// progressing or stuck. These mirror AuthStage in app/ocado/auth.py.
const STAGE_LABELS = {
  checking_session: 'Checking your saved session…',
  signing_in: 'Signing in…',
  waiting_for_code: 'Waiting for the emailed code…',
  entering_code: 'Entering the code…',
}

function stageLabel(stage, fallback) {
  return STAGE_LABELS[stage] ?? fallback
}

// The server parks at awaiting_otp for the whole time it is fetching *and*
// submitting the code on your behalf, so the panel has to read from the stage
// rather than the state - otherwise it spends that minute telling you to go and
// type a code the app has already sent. The input stays on screen throughout:
// every one of these steps can give up and hand back to you.
function otpPanelCopy(stage) {
  if (stage === 'waiting_for_code') {
    return {
      title: 'Fetching your code',
      body: 'Reading it from the app mailbox. Enter it yourself if this stalls.',
    }
  }
  if (stage === 'entering_code') {
    return {
      title: 'Signing you in',
      body: 'Submitting the code Ocado sent. This can take up to a minute.',
    }
  }
  return {
    title: 'Check your email',
    body: 'Ocado sent a verification code. Enter it to finish signing in.',
  }
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

function SlotTabs({ accountId, days, reserve, onReserved }) {
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
                    { accountId, slotId: slot.slot_id },
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

// Your own items have no ingredient behind them and may not be in the catalogue
// at all, so the SKU is the last resort rather than the first thing tried.
function productName(line) {
  return line.name ?? line.sku
}

const CHANGES = {
  add: { label: 'Add', color: 'teal', sign: '+' },
  cart: { label: 'In cart', color: 'teal', sign: '' },
  restore: { label: 'Put back', color: 'orange', sign: '+' },
  remove: { label: 'Remove', color: 'yellow', sign: '−' },
  short: { label: 'Short', color: 'red', sign: '' },
  keep: { label: 'Yours', color: 'grape', sign: '' },
}

// One row per product per fate. A SKU can legitimately appear twice - as HF's
// two packs and your own one - and collapsing that would hide the very thing
// the table exists to show. Only the HF-owned fates are deduped against each
// other, first group winning, so a restored line is not also listed as bought.
function diffRows(groups) {
  const claimed = new Set()
  const rows = []
  for (const { kind, lines } of groups) {
    for (const line of lines ?? []) {
      if (kind !== 'keep') {
        if (claimed.has(line.sku)) continue
        claimed.add(line.sku)
      }
      rows.push({ kind, line })
    }
  }
  return rows
}

function quantityCell({ kind, line }) {
  // A shortfall is the one case where the bare number lies: "1" reads as a
  // success until you know three were wanted.
  if (kind === 'short' && line.wanted != null) return `${line.got ?? 0} of ${line.wanted}`
  return `${CHANGES[kind].sign}${line.quantity}`
}

function DiffTable({ groups }) {
  const rows = diffRows(groups)
  if (!rows.length) return null
  const showNote = rows.some(({ line }) => line.reason)
  return (
    <Table.ScrollContainer minWidth={420}>
      <Table verticalSpacing={6} className={classes.diffTable}>
        <Table.Thead>
          <Table.Tr>
            <Table.Th w={96}>Change</Table.Th>
            <Table.Th>Item</Table.Th>
            <Table.Th w={150}>For</Table.Th>
            <Table.Th w={64} ta="right">Qty</Table.Th>
            {showNote && <Table.Th w={140}>Note</Table.Th>}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => (
            <Table.Tr key={`${row.kind}:${row.line.sku}`}>
              <Table.Td>
                <Badge color={CHANGES[row.kind].color} variant="light" size="sm">
                  {CHANGES[row.kind].label}
                </Badge>
              </Table.Td>
              <Table.Td>
                <Text size="sm" fw={500} lineClamp={2}>{productName(row.line)}</Text>
              </Table.Td>
              {/* The ingredient, not the brand: "Sesame seeds" is what tells you
                  which part of the week a removal or a shortfall touches. */}
              <Table.Td>
                <Text size="sm" c="dimmed" lineClamp={2}>{row.line.ingredient ?? '—'}</Text>
              </Table.Td>
              <Table.Td ta="right">
                <Text size="sm" fw={700} ff="monospace">{quantityCell(row)}</Text>
              </Table.Td>
              {showNote && (
                <Table.Td>
                  <Text size="xs" c="dimmed">{row.line.reason ?? ''}</Text>
                </Table.Td>
              )}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </Table.ScrollContainer>
  )
}

// Written by the server as UTC without an offset, so it needs marking as such
// before it is read back in local time.
function formatSyncedAt(value) {
  if (!value) return 'earlier'
  const parsed = new Date(/(Z|[+-]\d{2}:?\d{2})$/.test(value) ? value : `${value}Z`)
  if (Number.isNaN(parsed.getTime())) return 'earlier'
  return parsed.toLocaleString(undefined, {
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function countUnits(lines) {
  return (lines ?? []).reduce((total, line) => total + (line.quantity ?? 0), 0)
}

function DiffPanel({ icon, title, aside, badges, children }) {
  return (
    <Box className={classes.diffPanel}>
      <Group justify="space-between" align="center" wrap="nowrap" mb="xs">
        <Group gap={6}>
          {icon}
          <Text fw={700} size="sm">{title}</Text>
        </Group>
        {aside}
      </Group>
      <Group gap="xs" mb="sm">{badges}</Group>
      {children}
    </Box>
  )
}

// The cart is shared with the rest of your week's shopping, so a sync that can
// remove things has to say what it will touch before it touches it.
function PlanSummary({ plan, loading }) {
  if (loading) return <Loader size="sm" />
  if (!plan) return null
  const yours = countUnits(plan.yours)
  const nothingToDo = !plan.added.length && !plan.removed.length && !plan.restored.length
  return (
    <DiffPanel
      icon={<IconEye size={16} />}
      title="What this sync will change"
      aside={
        plan.synced ? (
          <Text size="xs" c="dimmed">
            last synced {formatSyncedAt(plan.synced_at)}
            {plan.synced_week_start ? ` for ${formatWeekLabel(plan.synced_week_start)}` : ''}
          </Text>
        ) : (
          <Text size="xs" c="dimmed">first sync</Text>
        )
      }
      badges={
        <>
          <Badge color={plan.added.length ? 'teal' : 'gray'} variant="light">
            {countUnits(plan.added)} to add
          </Badge>
          <Badge color={plan.removed.length ? 'yellow' : 'gray'} variant="light">
            {countUnits(plan.removed)} to remove
          </Badge>
          {plan.restored.length > 0 && (
            <Badge color="orange" variant="light">{countUnits(plan.restored)} to put back</Badge>
          )}
          <Badge color={yours ? 'grape' : 'gray'} variant="light">{yours} of yours untouched</Badge>
        </>
      }
    >
      {nothingToDo && (
        <Text size="sm" c="dimmed" mb="xs">The cart already matches this week.</Text>
      )}
      {/* Removals first: they are the only thing here that takes something out
          of a cart you may have filled yourself. */}
      <DiffTable
        groups={[
          { kind: 'remove', lines: plan.removed },
          { kind: 'restore', lines: plan.restored },
          { kind: 'add', lines: plan.added },
          { kind: 'keep', lines: plan.yours },
        ]}
      />
      {!plan.synced && (
        <Text size="xs" c="dimmed" mt="xs">
          Anything already in the cart that this week also needs is treated as
          HF's own from a previous push, rather than bought again.
        </Text>
      )}
    </DiffPanel>
  )
}

function PushSummary({ result }) {
  if (!result) return null
  const swaps = result.swaps ?? []
  const soldOut = result.sold_out ?? []
  const yours = countUnits(result.yours)
  return (
    <DiffPanel
      icon={<IconCheck size={16} />}
      title="Sync result"
      badges={
        <>
          <Badge color="teal" variant="light">{result.applied.length} applied</Badge>
          <Badge color={swaps.length ? 'blue' : 'gray'} variant="light">
            {swaps.length} swapped
          </Badge>
          <Badge color={result.removed?.length ? 'yellow' : 'gray'} variant="light">
            {countUnits(result.removed)} removed
          </Badge>
          <Badge color={yours ? 'grape' : 'gray'} variant="light">{yours} of yours kept</Badge>
          <Badge color={result.dropped.length ? 'red' : 'gray'} variant="light">
            {result.dropped.length} short
          </Badge>
          <Badge color={result.unmapped.length ? 'red' : 'gray'} variant="light">
            {result.unmapped.length} unmapped
          </Badge>
        </>
      }
    >
      {/* Shortfalls first, then the two moves that touched what was already in
          the cart, then the ordinary week's shopping. */}
      <DiffTable
        groups={[
          { kind: 'short', lines: result.dropped },
          { kind: 'remove', lines: result.removed },
          { kind: 'restore', lines: result.restored },
          { kind: 'cart', lines: result.applied },
          { kind: 'keep', lines: result.yours },
        ]}
      />
      {swaps.length > 0 && (
        <Stack gap={2} mt="sm">
          {swaps.map((swap) => (
            <Text size="sm" key={swap.ingredient_key}>
              {swap.ingredient}: {swap.from_products.join(', ')} out of stock →{' '}
              {swap.to_products.join(', ')} ({formatDelta(swap.cost_delta)})
              {swap.tier_changed ? ', closest match unavailable' : ''}
            </Text>
          ))}
        </Stack>
      )}
      {/* Ingredient-level facts, with no cart line to sit against. */}
      {soldOut.length > 0 && (
        <Text size="sm" mt="sm">Nothing in stock for: {soldOut.join(', ')}</Text>
      )}
      {result.unmapped.length > 0 && (
        <Text size="sm" mt="xs">Not sent to Ocado: {result.unmapped.join(', ')}</Text>
      )}
    </DiffPanel>
  )
}

export default function OcadoPage() {
  const { upcomingWeekStart, getWeekRecipes } = useWeeklyPlan()
  const { ownedItemKeys, ownedItemKeySet } = useOwnedBasketItems(upcomingWeekStart)
  const { packOverrides } = useWeekPackChoices(upcomingWeekStart)
  const entries = getWeekRecipes(upcomingWeekStart)
  const selections = useMemo(() => toPlannerSelections(entries), [entries])
  const planner = usePlannerBasket(selections, packOverrides)
  const accounts = useOcadoAccounts()
  const [accountId, setAccountId] = useState(() =>
    window.localStorage.getItem(ACCOUNT_STORAGE_KEY) || null,
  )
  const selectedAccount = useMemo(
    () => (accounts.data?.items ?? []).find((account) => account.id === accountId) ?? null,
    [accounts.data?.items, accountId],
  )
  const login = useOcadoLogin(accountId)
  const sessionRefresh = useOcadoSessionRefresh(accountId)
  const status = useOcadoStatus(accountId, {
    enabled: Boolean(selectedAccount),
    active: login.isPending || sessionRefresh.isPending,
  })
  const reconnectAttempted = useRef(new Set())
  const otp = useOcadoOtp(accountId)
  const push = useOcadoPush(accountId)
  const basket = useOcadoBasket(accountId, { enabled: status.data?.status === 'ready' })
  const plan = useOcadoPushPlan(
    { accountId, selections, ownedItemKeys, packOverrides },
    { enabled: status.data?.status === 'ready' },
  )
  const [otpCode, setOtpCode] = useState('')
  const [reservation, setReservation] = useState(null)
  const slots = useOcadoSlots({ accountId }, { enabled: status.data?.status === 'ready' })
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
  const stage = status.data?.stage ?? 'idle'
  const signingIn = login.isPending || reconnecting
  // The server reads the code out of the app mailbox and submits it itself, and
  // is parked at awaiting_otp for both steps.
  const handlingCode = stage === 'waiting_for_code' || stage === 'entering_code'
  const otpCopy = otpPanelCopy(stage)

  useEffect(() => {
    const items = accounts.data?.items ?? []
    if (!items.length) return
    if (accountId && items.some((account) => account.id === accountId)) return
    const next = accounts.data?.default_account_id ?? items[0].id
    setAccountId(next)
    window.localStorage.setItem(ACCOUNT_STORAGE_KEY, next)
  }, [accountId, accounts.data])

  useEffect(() => {
    if (!accountId) return
    window.localStorage.setItem(ACCOUNT_STORAGE_KEY, accountId)
    setOtpCode('')
    setReservation(null)
    login.reset()
    otp.reset()
    push.reset()
  }, [accountId])

  // A saved session usually just needs waking up, so try that on arrival rather
  // than making you press a button for it. Once per mount, and only the rungs
  // that need nothing from you - /session/refresh stops before the password
  // step, so this can never trigger an OTP email on its own.
  useEffect(() => {
    if (!accountId) return
    if (status.data?.status !== 'logged_out') return
    if (reconnectAttempted.current.has(accountId)) return
    reconnectAttempted.current.add(accountId)
    sessionRefresh.mutate()
  }, [accountId, status.data?.status, sessionRefresh])

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
        <Group gap="sm" align="center">
          <Select
            aria-label="Ocado account"
            value={accountId}
            onChange={(value) => value && setAccountId(value)}
            data={(accounts.data?.items ?? []).map((account) => ({
              value: account.id,
              label: account.label,
            }))}
            disabled={accounts.isLoading || (accounts.data?.items ?? []).length <= 1}
            allowDeselect={false}
            w={{ base: 180, sm: 220 }}
          />
          <Badge color={status.data?.status === 'ready' ? 'teal' : 'gray'} variant="light" size="lg">
            {statusLabel(status.data?.status)}
          </Badge>
        </Group>
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
                  <Title order={3}>{awaitingOtp ? otpCopy.title : 'Connect to Ocado'}</Title>
                  {(status.isLoading || reconnecting || handlingCode) && <Loader size="sm" />}
                </Group>

                {awaitingOtp ? (
                  <>
                    <Text size="sm" c="dimmed">{otpCopy.body}</Text>
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
                        leftSection={
                          otp.isPending ? (
                            <Loader size={16} color="var(--mantine-color-white)" />
                          ) : null
                        }
                        disabled={!otpCode.trim() || otp.isPending}
                        onClick={() => otp.mutate(otpCode)}
                      >
                        {otp.isPending ? 'Entering the code…' : 'Submit'}
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
                    {/* Not Mantine's `loading` prop: it fades the label out to
                        make room for its own spinner, and the label is the
                        whole point here. A Loader in the left slot keeps both. */}
                    <Button
                      leftSection={
                        signingIn ? (
                          <Loader size={16} color="var(--mantine-color-white)" />
                        ) : (
                          <IconLogin size={16} />
                        )
                      }
                      disabled={!accountId || signingIn}
                      onClick={() => login.mutate()}
                    >
                      {signingIn ? stageLabel(stage, 'Signing in…') : 'Sign in to Ocado'}
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
                  {/* What the sync will touch, before it touches it - the cart
                      holds the rest of the week's shopping too. */}
                  {connected && !push.data && (
                    <PlanSummary plan={plan.data} loading={plan.isLoading} />
                  )}
                  {/* Worth saying rather than swallowing: without the preview
                      you are pressing a button that can remove things. */}
                  {connected && plan.error && (
                    <Alert color="yellow" icon={<IconAlertCircle size={18} />}>
                      Could not preview the sync: {plan.error.message}
                    </Alert>
                  )}
                  <Button
                    leftSection={<IconBasketUp size={16} />}
                    disabled={!selections.length || status.data?.status !== 'ready'}
                    loading={push.isPending}
                    onClick={() =>
                      push.mutate({
                        accountId,
                        selections,
                        ownedItemKeys,
                        packOverrides,
                        weekStart: upcomingWeekStart,
                      })
                    }
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
              <SlotTabs
                accountId={accountId}
                days={slotDays}
                reserve={reserve}
                onReserved={setReservation}
              />
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
