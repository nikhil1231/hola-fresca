import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Collapse,
  Group,
  Loader,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
} from '@mantine/core'
import {
  IconAlertCircle,
  IconBasketCheck,
  IconChevronDown,
  IconCircleCheck,
  IconCircleDashed,
  IconCloudUpload,
  IconPlugOff,
  IconPlus,
  IconRefresh,
  IconTrash,
} from '@tabler/icons-react'

import {
  useCartClearPersonal,
  useCartPush,
  useCartPushPlan,
} from '../hooks/useCartQueries.js'
import { useCartConnection } from '../hooks/useCartConnection.js'
import { useActiveRetailer } from '../hooks/useRetailer.js'
import RetailerLoginPanel, { RetailerAccountStatus } from './RetailerLoginPanel.jsx'
import classes from './CheckoutPanel.module.css'

const money = new Intl.NumberFormat('en-GB', { style: 'currency', currency: 'GBP' })
const ACTIONABLE_STATUSES = new Set(['not_synced', 'changed', 'deleted'])

// The shop's name is a parameter, not a constant: this panel serves whichever
// retailer is active, and telling someone their Sainsbury's trolley has "extra
// on Ocado" is worse than saying nothing. `shop` falls back to a neutral word
// for the beat before the active retailer has loaded.
const STATUS = {
  unverified: {
    label: () => `Connect to verify`,
    color: 'grape',
    icon: IconPlugOff,
    description: (shop) => `Connect to ${shop} to verify whether this product is synced.`,
  },
  not_synced: {
    label: () => 'Not synced',
    color: 'gray',
    icon: IconCircleDashed,
    description: (shop) => `This ${shop} account has not been synced yet.`,
  },
  changed: {
    label: () => 'Changed',
    color: 'yellow',
    icon: IconRefresh,
    description: () => 'The planned product or quantity changed since the last sync.',
  },
  deleted: {
    label: () => 'Deleted or reduced',
    color: 'red',
    icon: IconTrash,
    description: (shop) =>
      `This managed quantity was deleted or reduced on ${shop} and will be restored.`,
  },
  extra: {
    label: (shop) => `Extra on ${shop}`,
    color: 'blue',
    icon: IconPlus,
    description: (shop) => `Extra quantity was added on ${shop}; sync leaves it untouched.`,
  },
  synced: {
    label: () => 'Synced',
    color: 'green',
    icon: IconCircleCheck,
    description: () => 'The planned, synced, and live managed quantities agree.',
  },
}

const DEFAULT_SHOP = 'the shop'

function formatMoney(value) {
  return money.format(value ?? 0)
}

function plannedCheckoutItems(lines, ownedItemKeySet) {
  const items = new Map()
  for (const line of lines ?? []) {
    if (line.external || ownedItemKeySet.has(line.key)) continue
    for (const choice of line.choices ?? []) {
      const current = items.get(choice.sku)
      if (current) {
        current.desired_quantity += choice.count ?? 0
        if (choice.cost != null) current.cost = (current.cost ?? 0) + choice.cost
        continue
      }
      items.set(choice.sku, {
        sku: choice.sku,
        name: choice.product_name,
        url: choice.url,
        pack_size_raw: choice.pack_size_raw,
        desired_quantity: choice.count ?? 0,
        synced_quantity: 0,
        cart_quantity: 0,
        cost: choice.cost ?? null,
        cost_source: 'planned',
        status: 'unverified',
      })
    }
  }
  return [...items.values()].sort((a, b) => a.name.localeCompare(b.name))
}

function StatusIcon({ item, shop = DEFAULT_SHOP }) {
  const meta = STATUS[item.status] ?? STATUS.unverified
  const Icon = meta.icon
  let description = meta.description(shop)
  if (item.status === 'changed') {
    description = `Changed since sync: ${item.synced_quantity} synced, ${item.desired_quantity} planned.`
  } else if (item.status === 'deleted') {
    description = `${item.synced_quantity} synced, ${item.cart_quantity} currently on ${shop}. Sync restores the planned quantity.`
  } else if (item.status === 'extra') {
    description = `${item.synced_quantity} managed, ${item.cart_quantity} currently on ${shop}. The extra quantity stays yours.`
  }
  return (
    <Tooltip label={description} withArrow multiline w={280}>
      <span className={classes.statusIcon} aria-label={meta.label(shop)} tabIndex={0}>
        <Icon size={20} color={`var(--mantine-color-${meta.color}-6)`} />
      </span>
    </Tooltip>
  )
}

function quantityDescription(item, shop = DEFAULT_SHOP) {
  const parts = []
  if (item.pack_size_raw) parts.push(item.pack_size_raw)
  if (item.cart_quantity > 0) parts.push(`${item.cart_quantity} in ${shop}`)
  else if (item.desired_quantity > 0) parts.push(`${item.desired_quantity} planned`)
  else parts.push('pending removal')
  if (item.status === 'deleted' && item.desired_quantity > 0) {
    parts.push(`${item.desired_quantity} planned`)
  }
  return parts.join(' · ')
}

function StatusLegend({ shop = DEFAULT_SHOP }) {
  return (
    <Group gap="md" className={classes.legend}>
      {Object.entries(STATUS).map(([key, meta]) => {
        const Icon = meta.icon
        return (
          <Group key={key} gap={4} wrap="nowrap">
            <Icon size={14} color={`var(--mantine-color-${meta.color}-6)`} />
            <Text size="xs" c="dimmed">{meta.label(shop)}</Text>
          </Group>
        )
      })}
    </Group>
  )
}

export default function CheckoutPanel({
  lines,
  ownedItemKeys,
  ownedItemKeySet,
  packOverrides,
  selections,
  snapOverrides,
  unmapped,
  soldOut,
  weekStart,
}) {
  const { id: retailer, label: retailerLabel } = useActiveRetailer()
  const connection = useCartConnection(retailer)
  const { connected } = connection
  const push = useCartPush(retailer)
  const plan = useCartPushPlan(
    { retailer, selections, ownedItemKeys, packOverrides, snapOverrides },
    { enabled: connected },
  )
  const [personalItemsOpen, setPersonalItemsOpen] = useState(false)
  const clearPersonal = useCartClearPersonal(retailer)
  const resetPush = push.reset
  useEffect(() => {
    resetPush()
    setPersonalItemsOpen(false)
  }, [retailer, resetPush, weekStart])

  const fallbackItems = useMemo(
    () => plannedCheckoutItems(lines, ownedItemKeySet),
    [lines, ownedItemKeySet],
  )
  const items = connected && plan.data ? plan.data.checkout_items : fallbackItems
  const personalItems = useMemo(
    () => connected && plan.data
      ? [...(plan.data.yours ?? [])].sort((a, b) =>
          (a.name ?? a.sku).localeCompare(b.name ?? b.sku),
        )
      : [],
    [connected, plan.data],
  )
  const actionable = items.some((item) => ACTIONABLE_STATUSES.has(item.status))
  const total = items.reduce((sum, item) => sum + (item.cost ?? 0), 0)
  const hasUnknownCosts = items.some((item) => item.cost == null)
  const shop = retailerLabel ?? DEFAULT_SHOP

  return (
    <Stack gap="md" className={classes.checkoutPanel}>
      <Group justify="space-between" align="center" wrap="wrap" className={classes.header}>
        <Group gap="sm">
          <Title order={3} className={classes.title}>Checkout</Title>
          <RetailerAccountStatus connection={connection} shop={shop} />
        </Group>
        <Button
          color="green"
          leftSection={<IconCloudUpload size={17} />}
          loading={push.isPending}
          disabled={!connected || plan.isFetching || plan.isError || !actionable}
          onClick={() =>
            push.mutate({
              selections,
              ownedItemKeys,
              packOverrides,
              snapOverrides,
              weekStart,
            })
          }
        >
          {connected && !actionable && plan.data ? 'Synced' : `Sync to ${shop}`}
        </Button>
      </Group>

      {!connected && (
        <Box className={classes.connectionWrap}>
          <RetailerLoginPanel connection={connection} shop={shop} />
        </Box>
      )}

      {plan.error && (
        <Alert color="red" icon={<IconAlertCircle size={18} />} className={classes.notice}>
          Could not compare with {shop}: {plan.error.message}
        </Alert>
      )}
      {push.error && (
        <Alert color="red" icon={<IconAlertCircle size={18} />} className={classes.notice}>
          {push.error.message}
        </Alert>
      )}
      {push.data && (
        <Alert
          color={(push.data.dropped?.length ?? 0) > 0 ? 'yellow' : 'green'}
          icon={<IconCircleCheck size={18} />}
          className={classes.notice}
        >
          Checkout synced to {shop}.
          {(push.data.dropped?.length ?? 0) > 0
            ? ` ${push.data.dropped.length} product lines could not be fully added.`
            : ''}
          {(push.data.swaps?.length ?? 0) > 0
            ? ` ${push.data.swaps.length} products were substituted.`
            : ''}
        </Alert>
      )}
      {(unmapped?.length ?? 0) > 0 && (
        <Alert
          color="yellow"
          variant="light"
          icon={<IconAlertCircle size={18} />}
          className={classes.notice}
        >
          Not sent to {shop}: {unmapped.join(', ')}
        </Alert>
      )}
      {(soldOut?.length ?? 0) > 0 && (
        <Alert
          color="yellow"
          variant="light"
          icon={<IconAlertCircle size={18} />}
          className={classes.notice}
        >
          Nothing in stock for: {soldOut.join(', ')}
        </Alert>
      )}

      {connected && plan.isLoading ? (
        <Group justify="center" py="xl"><Loader color="green" /></Group>
      ) : items.length === 0 && personalItems.length === 0 ? (
        <Box className={classes.emptyState}>
          <Text fw={700}>No retailer products</Text>
          <Text size="sm" c="dimmed">Add recipes to build a checkout basket.</Text>
        </Box>
      ) : (
        <Box className={classes.itemList}>
          {items.length > 0 ? (
            <Table.ScrollContainer minWidth={460} type="native" className={classes.tableScroll}>
              <Table verticalSpacing="sm" className={classes.table}>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th w={50}><span className={classes.visuallyHidden}>Status</span></Table.Th>
                    <Table.Th>Product</Table.Th>
                    <Table.Th w={120} ta="right">Cost</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {items.map((item) => (
                    <Table.Tr key={item.sku}>
                      <Table.Td><StatusIcon item={item} shop={shop} /></Table.Td>
                      <Table.Td>
                        {item.url ? (
                          <a href={item.url} target="_blank" rel="noreferrer" className={classes.productLink}>
                            {item.name}
                          </a>
                        ) : (
                          <Text className={classes.productName}>{item.name}</Text>
                        )}
                        <Text className={classes.productMeta}>{quantityDescription(item, shop)}</Text>
                      </Table.Td>
                      <Table.Td ta="right">
                        <Tooltip
                          label={item.cost == null
                            ? 'Price unavailable'
                            : item.cost_source === 'live'
                              ? `Live ${shop} basket total`
                              : 'Planned price until synced'}
                          withArrow
                        >
                          <Text className={classes.cost}>
                            {item.cost == null ? '—' : formatMoney(item.cost)}
                          </Text>
                        </Tooltip>
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
                <Table.Tfoot>
                  <Table.Tr>
                    <Table.Th />
                    <Table.Th>{hasUnknownCosts ? 'Known total' : 'Total'}</Table.Th>
                    <Table.Th ta="right">{formatMoney(total)}</Table.Th>
                  </Table.Tr>
                </Table.Tfoot>
              </Table>
            </Table.ScrollContainer>
          ) : (
            <Box className={classes.emptyManaged}>
              <Text fw={600}>Nothing from this week to sync</Text>
            </Box>
          )}

          {personalItems.length > 0 && (
            <Box className={classes.personalSection}>
              <button
                type="button"
                className={classes.personalToggle}
                aria-expanded={personalItemsOpen}
                aria-controls="personal-basket-items"
                onClick={() => setPersonalItemsOpen((open) => !open)}
              >
                <span className={classes.personalHeadingIcon} aria-hidden="true">
                  <IconBasketCheck size={20} />
                </span>
                <span className={classes.personalHeadingText}>
                  <span className={classes.personalTitle}>Already in your basket</span>
                  <span className={classes.personalDescription}>
                    {personalItems.length} {personalItems.length === 1 ? 'item' : 'items'} left out of the Hola Fresca shop
                  </span>
                </span>
                <IconChevronDown
                  size={18}
                  className={`${classes.personalChevron} ${personalItemsOpen ? classes.personalChevronOpen : ''}`}
                />
              </button>
              <Collapse expanded={personalItemsOpen} id="personal-basket-items">
                <div className={classes.personalList}>
                  {personalItems.map((item) => (
                    <div key={item.sku} className={classes.personalRow}>
                      <span className={classes.personalItemIcon} aria-label="Already in your basket">
                        <IconBasketCheck size={20} />
                      </span>
                      <span className={classes.personalName}>{item.name ?? item.sku}</span>
                      <span className={classes.personalQuantity}>
                        {item.quantity} in {shop}
                      </span>
                    </div>
                  ))}
                  <div className={classes.personalClearRow}>
                    <Button
                      variant="subtle"
                      color="red"
                      size="compact-sm"
                      leftSection={<IconTrash size={14} />}
                      loading={clearPersonal.isPending}
                      onClick={() => {
                        if (window.confirm('Are you sure you want to clear all non-HolaFresca items?')) {
                          clearPersonal.mutate()
                        }
                      }}
                    >
                      Clear
                    </Button>
                  </div>
                </div>
              </Collapse>
            </Box>
          )}
        </Box>
      )}

      <StatusLegend shop={shop} />
    </Stack>
  )
}
