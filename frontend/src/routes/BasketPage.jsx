import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Center,
  Checkbox,
  Divider,
  Group,
  Loader,
  Modal,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
  Tooltip,
  UnstyledButton,
} from '@mantine/core'
import {
  IconAlertCircle,
  IconAlertTriangle,
  IconBasket,
  IconBuildingStore,
  IconCalendarWeek,
  IconArrowRight,
  IconChevronDown,
  IconChevronRight,
  IconClock,
  IconHome,
  IconPackages,
  IconRefresh,
  IconStarFilled,
  IconToolsKitchen2,
} from '@tabler/icons-react'

import RecipeCard from '../components/RecipeCard.jsx'
import { useOcadoStockRefresh } from '../hooks/useOcadoQueries.js'
import { useOwnedBasketItems } from '../hooks/useOwnedBasketItems.js'
import { useWeekPackChoices } from '../hooks/useWeekPackChoices.js'
import { usePackPreference, usePlannerBasket } from '../hooks/useRecipeQueries.js'
import {
  formatWeekLabel,
  MAX_RECIPES_PER_WEEK,
  toPlannerSelections,
  useWeeklyPlan,
} from '../hooks/useWeeklyPlan.js'
import classes from './BasketPage.module.css'

const money = new Intl.NumberFormat('en-GB', {
  style: 'currency',
  currency: 'GBP',
})

function formatMoney(value) {
  return money.format(value ?? 0)
}

function formatGrams(value) {
  if (value == null) return '-'
  return `${Math.round(value).toLocaleString()}g`
}

function formatQuantity(value, unit = 'g') {
  if (value == null) return '-'
  if (unit === 'unit') {
    const rounded = Math.round(value * 100) / 100
    return `${rounded.toLocaleString()} ${rounded === 1 ? 'unit' : 'units'}`
  }
  return formatGrams(value)
}

function formatDelta(value) {
  const rounded = Math.round((value ?? 0) * 100) / 100
  if (rounded === 0) return 'same price'
  return `${rounded > 0 ? '+' : '-'}${formatMoney(Math.abs(rounded))}`
}

function formatSignedCapacity(value, unit) {
  const rounded = unit === 'unit' ? Math.round((value ?? 0) * 100) / 100 : Math.round(value ?? 0)
  if (rounded === 0) return 'same'
  const magnitude = unit === 'unit' ? formatQuantity(Math.abs(rounded), unit) : formatGrams(Math.abs(rounded))
  return `${rounded > 0 ? '+' : '-'}${magnitude}`
}

// "checked 4 min ago". Deliberately coarse: the useful question is whether the
// stock behind these prices is minutes or days old, never which minute it was.
function formatAge(value) {
  if (!value) return null
  const then = new Date(value).getTime()
  if (Number.isNaN(then)) return null
  const minutes = Math.round((Date.now() - then) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

function packsText(line) {
  if (!line.choices?.length) return line.note ?? '-'
  return line.choices
    .map((choice) => `${choice.count}x ${choice.pack_size_raw || formatGrams(choice.capacity_g)}`)
    .join(' + ')
}

function contributionIds(line) {
  return new Set((line?.contributions ?? []).map((contribution) => contribution.recipe_id))
}

function recipePortionPrices(lines, entries) {
  const prices = new Map(entries.map((entry) => [entry.recipe.id, 0]))
  for (const line of lines) {
    const lineNeed = line.quantity_unit === 'unit' ? line.need_qty : line.need_g
    if (!lineNeed || !line.cost || !line.contributions?.length) continue
    for (const contribution of line.contributions) {
      const contributionNeed =
        line.quantity_unit === 'unit' ? contribution.quantity : contribution.grams
      if (!contributionNeed) continue
      prices.set(
        contribution.recipe_id,
        (prices.get(contribution.recipe_id) ?? 0) + line.cost * (contributionNeed / lineNeed),
      )
    }
  }
  return new Map(
    entries.map((entry) => [
      entry.recipe.id,
      (prices.get(entry.recipe.id) ?? 0) / Math.max(entry.portions, 1),
    ]),
  )
}

function Stat({ label, value, tone = 'default' }) {
  return (
    <Box className={`${classes.stat} ${classes[tone] ?? ''}`}>
      <Text size="xs" c="dimmed" fw={700} tt="uppercase">
        {label}
      </Text>
      <Text fw={800} className={classes.statValue}>
        {value}
      </Text>
    </Box>
  )
}

function formatUnitCost(option) {
  return option.quantity_unit === 'unit'
    ? `${formatMoney(option.unit_cost)}/unit`
    : `${formatMoney(option.unit_cost)}/kg`
}

function formatCapacity(value, unit) {
  return unit === 'unit' ? formatQuantity(value, unit) : formatGrams(value)
}

// Deliberately coarse, and deliberately capped at "2+ years". The estimate comes
// from how often the whole library cooks the ingredient, not from what you
// actually make, and at one gram a recipe it will happily compute seventy years
// of chilli flakes — which is true, useless, and makes a sane 30p upsize look
// deranged. The figure is only good to an order of magnitude, so it says so.
function formatSupply(weeks) {
  if (weeks == null) return null
  if (weeks < 2) return 'about a week'
  if (weeks < 9) return `about ${Math.round(weeks)} weeks`
  if (weeks < 78) return `about ${Math.round(weeks / 4.3)} months`
  return '2+ years'
}

function ratingColor(rating) {
  if (rating == null) return 'gray'
  if (rating >= 4.5) return 'teal'
  if (rating >= 4) return 'green'
  if (rating >= 3.5) return 'yellow'
  return 'orange'
}

function Rating({ option }) {
  if (option.rating == null) return <Text size="sm" c="dimmed">unrated</Text>
  return (
    <Group gap={4} wrap="nowrap">
      <IconStarFilled size={13} className={classes.star} />
      <Text size="sm" fw={700} c={ratingColor(option.rating)}>
        {option.rating.toFixed(1)}
      </Text>
      <Text size="xs" c="dimmed">({option.ratings_count ?? 0})</Text>
    </Group>
  )
}

// Which of the two clocks ran out first. "About four weeks" means one thing for
// halloumi and quite another for cumin, and the difference decides whether the
// leftover is stock or a slow bin.
function SupplyIcon({ option }) {
  const expiry = option.supply_limited_by === 'expiry'
  return (
    <Tooltip
      label={
        expiry
          ? 'Limited by the use-by date, not by how fast you get through it'
          : 'Limited by how often this library cooks it, not by a date'
      }
    >
      {expiry ? (
        <IconClock size={13} className={classes.supplyExpiry} />
      ) : (
        <IconToolsKitchen2 size={13} className={classes.supplyEaten} />
      )}
    </Tooltip>
  )
}

// One size, as a card you can pick. Only the three things that decide it: how
// big, what it costs per kilo, and whether anyone rates it. The brand name is
// not one of them.
function PackCard({ option, active, cheaper, onPick, disabled }) {
  return (
    <UnstyledButton
      className={`${classes.packCard} ${active ? classes.packCardActive : ''}`}
      onClick={onPick}
      disabled={disabled}
    >
      <Group justify="space-between" wrap="nowrap" mb={2}>
        <Text fw={800} size="lg">
          {option.pack_size_raw || formatCapacity(option.capacity / option.count, option.quantity_unit)}
        </Text>
        {active && <Badge size="xs" variant="filled" color="fresh">in basket</Badge>}
        {!active && cheaper && <Badge size="xs" variant="light" color="green">better value</Badge>}
      </Group>

      <Text fw={800} size="xl" c={cheaper ? 'green' : undefined} className={classes.unitCost}>
        {formatUnitCost(option)}
      </Text>

      <Rating option={option} />

      <Group gap={4} wrap="nowrap" mt={6}>
        <Text size="xs" c="dimmed">
          {option.count > 1 ? `${option.count} packs · ` : ''}
          {formatMoney(option.cost)}
          {option.weeks_of_supply != null ? ` · ${formatSupply(option.weeks_of_supply)}` : ''}
        </Text>
        {option.weeks_of_supply != null && <SupplyIcon option={option} />}
      </Group>
    </UnstyledButton>
  )
}

function PackSizeModal({ line, opened, onClose, scope, setScope, onPick, onReset, pending }) {
  const [showAll, setShowAll] = useState(false)
  const options = line?.options ?? []
  const current = options.find((option) => option.chosen)
  const alternative =
    options.find((option) => option.better_value && !option.chosen) ??
    options.find((option) => option !== current && option.unit_cost < (current?.unit_cost ?? 0))
  const held = options.find((option) => option.pinned || option.this_week)
  const rest = options.filter((option) => option !== current && option !== alternative)

  useEffect(() => {
    if (!opened) setShowAll(false)
  }, [opened])

  if (!line) return null

  return (
    <Modal.Root opened={opened} onClose={onClose} size="lg" centered>
      <Modal.Overlay />
      <Modal.Content>
        <Modal.Header>
          <Modal.Title fw={700}>{line.name}</Modal.Title>
          <Group gap="sm" wrap="nowrap">
            <SegmentedControl
              size="xs"
              value={scope}
              onChange={setScope}
              data={[
                { label: 'This week', value: 'week' },
                { label: 'Always', value: 'always' },
              ]}
            />
            <Modal.CloseButton />
          </Group>
        </Modal.Header>

        <Modal.Body>
          <Group align="stretch" justify="center" gap="sm" wrap="nowrap" className={classes.packRow}>
            {current && (
              <PackCard
                option={current}
                active
                cheaper={false}
                disabled={pending}
                onPick={() => onPick(current.sku)}
              />
            )}
            {alternative && (
              <>
                <Stack className={classes.packArrow} gap={0} align="center" justify="center">
                  <Text size="sm" fw={700} c={alternative.cost_delta > 0 ? 'orange' : 'green'}>
                    {formatDelta(alternative.cost_delta)}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {formatSignedCapacity(alternative.leftover_delta, alternative.quantity_unit)}
                  </Text>
                  <IconArrowRight size={22} className={classes.packArrowIcon} />
                </Stack>
                <PackCard
                  option={alternative}
                  active={false}
                  cheaper={alternative.unit_cost < (current?.unit_cost ?? Infinity)}
                  disabled={pending}
                  onPick={() => onPick(alternative.sku)}
                />
              </>
            )}
          </Group>

          <Divider my="md" />

          <Group justify="flex-end" gap="xs" wrap="nowrap">
            {held && (
              <Button size="compact-xs" variant="subtle" color="gray" onClick={onReset}>
                Let the planner choose
              </Button>
            )}
            {rest.length > 0 && (
              <Button
                size="compact-xs"
                variant="subtle"
                rightSection={
                  <IconChevronDown
                    size={14}
                    className={`${classes.chevron} ${showAll ? classes.chevronFlip : ''}`}
                  />
                }
                onClick={() => setShowAll((value) => !value)}
              >
                {rest.length} other {rest.length === 1 ? 'size' : 'sizes'}
              </Button>
            )}
          </Group>

          {/* Rendered outright rather than inside a Collapse: that measures its
              child's height, and a child carrying a margin measures as nothing,
              which is exactly how this list came to open onto an empty gap. */}
          {showAll && (
            <Stack gap={4} className={classes.otherSizes}>
              {rest.map((option) => (
                <Group
                  key={option.sku}
                  justify="space-between"
                  wrap="nowrap"
                  className={classes.otherSize}
                >
                  <Group gap="sm" wrap="nowrap">
                    <Text size="sm" fw={700} w={72}>
                      {option.pack_size_raw ||
                        formatCapacity(option.capacity / option.count, option.quantity_unit)}
                    </Text>
                    <Text size="sm" c="dimmed" w={88}>{formatUnitCost(option)}</Text>
                    <Rating option={option} />
                  </Group>
                  <Group gap="sm" wrap="nowrap">
                    <Text size="sm">{formatMoney(option.cost)}</Text>
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      disabled={pending}
                      onClick={() => onPick(option.sku)}
                    >
                      Pick
                    </Button>
                  </Group>
                </Group>
              ))}
            </Stack>
          )}
        </Modal.Body>
      </Modal.Content>
    </Modal.Root>
  )
}


function LineTable({
  title,
  icon,
  lines,
  onOpenPacks,
  busyPackKey,
  openLineKey,
  setOpenLineKey,
  tableId,
  hoverRecipeId,
  selectedLineKey,
  setActiveLineKey,
  setHoverLineKey,
  ownedItemKeySet,
  setItemOwned,
}) {
  if (!lines.length) return null

  return (
    <Box className={classes.section}>
      <Group gap="xs" mb="xs">
        {icon}
        <Title order={3} className={classes.sectionTitle}>
          {title}
        </Title>
      </Group>
      <Table.ScrollContainer minWidth={720}>
        <Table highlightOnHover verticalSpacing="sm" className={classes.lineTable}>
          <colgroup>
            <col className={classes.colOwned} />
            <col className={classes.colIngredient} />
            <col className={classes.colNeed} />
            <col className={classes.colPacks} />
            <col className={classes.colLeft} />
            <col className={classes.colCost} />
            <col className={classes.colWaste} />
            <col className={classes.colPackSwap} />
          </colgroup>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Owned</Table.Th>
              <Table.Th>Ingredient</Table.Th>
              <Table.Th>Need</Table.Th>
              <Table.Th>Packs</Table.Th>
              <Table.Th>Left</Table.Th>
              <Table.Th>Cost</Table.Th>
              <Table.Th>Waste</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {lines.map((line) => {
              const rowKey = `${tableId}:${line.key}`
              const expanded = openLineKey === rowKey
              const canExpand = line.choices?.length > 0
              const lineRecipeIds = contributionIds(line)
              const highlighted =
                selectedLineKey === rowKey || (hoverRecipeId && lineRecipeIds.has(hoverRecipeId))
              const owned = ownedItemKeySet.has(line.key)
              const upsize = (line.options ?? []).find((option) => option.better_value)
              const heldOption = (line.options ?? []).find(
                (option) => option.pinned || option.this_week,
              )

              return (
                <Fragment key={rowKey}>
                  <Table.Tr
                    className={[
                      canExpand ? classes.expandableRow : '',
                      highlighted ? classes.highlightedRow : '',
                      owned ? classes.ownedRow : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                    tabIndex={canExpand ? 0 : undefined}
                    role={canExpand ? 'button' : undefined}
                    aria-expanded={canExpand ? expanded : undefined}
                    onMouseEnter={() => setHoverLineKey(rowKey)}
                    onMouseLeave={() => setHoverLineKey(null)}
                    onClick={() => {
                      setActiveLineKey(expanded ? null : rowKey)
                      if (canExpand) setOpenLineKey(expanded ? null : rowKey)
                    }}
                    onKeyDown={(event) => {
                      if (!canExpand || (event.key !== 'Enter' && event.key !== ' ')) return
                      event.preventDefault()
                      setActiveLineKey(expanded ? null : rowKey)
                      setOpenLineKey(expanded ? null : rowKey)
                    }}
                  >
                    <Table.Td className={classes.ownedCell}>
                      <Checkbox
                        checked={owned}
                        onChange={(event) => setItemOwned(line.key, event.currentTarget.checked)}
                        onClick={(event) => event.stopPropagation()}
                        onKeyDown={(event) => event.stopPropagation()}
                        aria-label={`Mark ${line.name} as already owned`}
                      />
                    </Table.Td>
                    <Table.Td>
                      <Group gap={6} wrap="nowrap">
                        {canExpand && (
                          <IconChevronRight
                            size={16}
                            className={`${classes.chevron} ${expanded ? classes.chevronOpen : ''}`}
                          />
                        )}
                        <div>
                          <Group gap={6}>
                            <Text fw={600}>{line.name}</Text>
                            {line.trace && (
                              <Badge size="xs" color="yellow" variant="light">
                                trace
                              </Badge>
                            )}
                            {line.substitution && (
                              <Tooltip
                                multiline
                                w={260}
                                label={`Out of stock: ${line.substitution.displaced.join(', ')}. Covered instead by ${line.choices
                                  .map((choice) => choice.product_name)
                                  .join(', ')}.`}
                              >
                                <Badge
                                  size="xs"
                                  color={line.substitution.tier_changed ? 'orange' : 'blue'}
                                  variant="light"
                                >
                                  swapped {formatDelta(line.substitution.cost_delta)}
                                </Badge>
                              </Tooltip>
                            )}
                            {heldOption && (
                              <Badge size="xs" color="grape" variant="light">
                                {heldOption.pack_size_raw || 'chosen size'}
                              </Badge>
                            )}
                          </Group>
                          {line.contributions?.length > 0 && (
                            <Text size="xs" c="dimmed">
                              {line.contributions
                                .map((contribution) => contribution.recipe_name)
                                .join(', ')}
                            </Text>
                          )}
                        </div>
                      </Group>
                    </Table.Td>
                    <Table.Td>{formatQuantity(line.need_qty ?? line.need_g, line.quantity_unit)}</Table.Td>
                    <Table.Td>{packsText(line)}</Table.Td>
                    <Table.Td>{formatQuantity(line.leftover_qty ?? line.leftover_g, line.quantity_unit)}</Table.Td>
                    <Table.Td>{formatMoney(line.cost)}</Table.Td>
                    <Table.Td>{formatMoney(line.waste_gbp)}</Table.Td>
                    <Table.Td className={classes.packSwapCell}>
                      {(line.options?.length ?? 0) > 1 && (
                        <Tooltip label={upsize ? 'A bigger pack is cheaper per kilo' : 'Change pack size'}>
                          <ActionIcon
                            variant="subtle"
                            size="md"
                            className={`${classes.packSwapButton} ${
                              upsize ? classes.packSwapDeal : ''
                            }`}
                            loading={busyPackKey === line.key}
                            aria-label={`Change pack size for ${line.name}`}
                            onClick={(event) => {
                              event.stopPropagation()
                              onOpenPacks(line.key)
                            }}
                          >
                            <IconPackages size={17} />
                          </ActionIcon>
                        </Tooltip>
                      )}
                    </Table.Td>
                  </Table.Tr>

                  {canExpand && (
                    <Table.Tr className={classes.expansionRow} aria-hidden={!expanded}>
                      <Table.Td colSpan={8} className={classes.expansionCell}>
                        <div
                          className={`${classes.expansionShell} ${
                            expanded ? classes.expansionShellOpen : ''
                          }`}
                        >
                          {line.substitution && (
                            <Text size="xs" c="dimmed" px="xs" pb={6}>
                              {line.substitution.tier_changed
                                ? 'Nothing matching exactly is in stock. '
                                : ''}
                              Out of stock: {line.substitution.displaced.join(', ')} —{' '}
                              {formatDelta(line.substitution.cost_delta)} against{' '}
                              {formatMoney(line.substitution.baseline_cost)}.
                            </Text>
                          )}
                          <div className={classes.choiceList}>
                            {line.choices.map((choice) => (
                              <div key={`${rowKey}:${choice.sku}`} className={classes.choiceItem}>
                                <div />
                                <div className={classes.choiceProduct}>
                                  {choice.url ? (
                                    <a
                                      href={choice.url}
                                      target="_blank"
                                      rel="noreferrer"
                                      className={classes.productLink}
                                      onClick={(event) => event.stopPropagation()}
                                    >
                                      {choice.product_name}
                                    </a>
                                  ) : (
                                    <Text size="sm">{choice.product_name}</Text>
                                  )}
                                </div>
                                <div />
                                <div>
                                  {choice.count}x{' '}
                                  {choice.pack_size_raw || formatGrams(choice.capacity_g)}
                                </div>
                                <div />
                                <div>{formatMoney(choice.cost)}</div>
                                <div />
                              </div>
                            ))}
                          </div>
                        </div>
                      </Table.Td>
                    </Table.Tr>
                  )}
                </Fragment>
              )
            })}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Box>
  )
}

function NameList({ title, names, muted = false }) {
  if (!names?.length) return null
  return (
    <Box className={classes.bucket}>
      <Text fw={700}>{title}</Text>
      <Group gap={6} mt="xs">
        {names.map((name) => (
          <Badge key={name} color={muted ? 'gray' : 'fresh'} variant="light" radius="sm">
            {name}
          </Badge>
        ))}
      </Group>
    </Box>
  )
}

export default function BasketPage() {
  const {
    upcomingWeekStart,
    weekStarts,
    getWeekRecipes,
    removeRecipeFromWeek,
    setRecipePortions,
  } = useWeeklyPlan()
  const [weekStart, setWeekStart] = useState(upcomingWeekStart)
  const [openLineKey, setOpenLineKey] = useState(null)
  const [activeLineKey, setActiveLineKey] = useState(null)
  const [hoverLineKey, setHoverLineKey] = useState(null)
  const [hoverRecipeId, setHoverRecipeId] = useState(null)
  const { ownedItemKeySet, setItemOwned } = useOwnedBasketItems(weekStart)
  const { packOverrides, setWeekPack } = useWeekPackChoices(weekStart)
  const stockRefresh = useOcadoStockRefresh()
  const packPreference = usePackPreference()
  const [packLineKey, setPackLineKey] = useState(null)
  const [packScope, setPackScope] = useState('week')
  const recipesScrollRef = useRef(null)
  const recipeRefs = useRef(new Map())

  useEffect(() => {
    if (!weekStarts.includes(weekStart)) setWeekStart(upcomingWeekStart)
  }, [upcomingWeekStart, weekStart, weekStarts])

  const entries = getWeekRecipes(weekStart)
  const selections = useMemo(() => toPlannerSelections(entries), [entries])
  const { data, isLoading, isError, error } = usePlannerBasket(selections, packOverrides)
  const onlineLines = useMemo(
    () => data?.lines?.filter((line) => !line.external) ?? [],
    [data?.lines],
  )
  const externalLines = useMemo(
    () => data?.lines?.filter((line) => line.external) ?? [],
    [data?.lines],
  )
  const totalPortions = useMemo(
    () => entries.reduce((total, entry) => total + entry.portions, 0),
    [entries],
  )
  const buyLines = useMemo(
    () => (data?.lines ?? []).filter((line) => !ownedItemKeySet.has(line.key)),
    [data?.lines, ownedItemKeySet],
  )
  const orderCost = useMemo(
    () => buyLines.reduce((total, line) => total + (line.cost ?? 0), 0),
    [buyLines],
  )
  const orderWaste = useMemo(
    () => buyLines.reduce((total, line) => total + (line.waste_gbp ?? 0), 0),
    [buyLines],
  )
  const recipePrices = useMemo(() => recipePortionPrices(buyLines, entries), [buyLines, entries])
  const stockAge = formatAge(data?.stock_checked_at)
  const packLine = useMemo(
    () => (data?.lines ?? []).find((line) => line.key === packLineKey) ?? null,
    [data?.lines, packLineKey],
  )

  // "This week" is local state, so it lands immediately; "Always" is a write and
  // shows a pending state until the re-priced basket comes back. Either way the
  // modal closes on the click rather than sitting there looking broken.
  const pickPack = useCallback(
    (sku) => {
      if (!packLine) return
      if (packScope === 'always') {
        packPreference.mutate({ ingredientKey: packLine.key, sku })
      } else {
        setWeekPack(packLine.key, sku)
      }
      setPackLineKey(null)
    },
    [packLine, packScope, packPreference, setWeekPack],
  )

  const resetPack = useCallback(() => {
    if (!packLine) return
    setWeekPack(packLine.key, null)
    if (packLine.options?.some((option) => option.pinned)) {
      packPreference.mutate({ ingredientKey: packLine.key, sku: null })
    }
    setPackLineKey(null)
  }, [packLine, packPreference, setWeekPack])
  const basketPortionPrice = totalPortions > 0 ? orderCost / totalPortions : 0
  const selectedLineKey = hoverLineKey ?? activeLineKey
  const allLines = useMemo(
    () => [
      ...onlineLines.map((line) => [`online:${line.key}`, line]),
      ...externalLines.map((line) => [`external:${line.key}`, line]),
    ],
    [onlineLines, externalLines],
  )
  const selectedRecipeIds = useMemo(() => {
    const line = allLines.find(([key]) => key === selectedLineKey)?.[1]
    return contributionIds(line)
  }, [allLines, selectedLineKey])

  useEffect(() => {
    setOpenLineKey(null)
    setActiveLineKey(null)
    setHoverLineKey(null)
    setHoverRecipeId(null)
  }, [weekStart, selections])

  useEffect(() => {
    if (!hoverLineKey || selectedRecipeIds.size === 0) return undefined
    const recipeId = selectedRecipeIds.values().next().value
    const handle = window.setTimeout(() => {
      const container = recipesScrollRef.current
      const node = recipeRefs.current.get(recipeId)
      if (!container || !node) return

      const containerRect = container.getBoundingClientRect()
      const nodeRect = node.getBoundingClientRect()
      const buffer = 32
      const topDelta = nodeRect.top - containerRect.top
      const bottomDelta = nodeRect.bottom - containerRect.bottom
      if (topDelta < buffer) {
        container.scrollTo({
          top: container.scrollTop + topDelta - buffer,
          behavior: 'smooth',
        })
      } else if (bottomDelta > -buffer) {
        container.scrollTo({
          top: container.scrollTop + bottomDelta + buffer,
          behavior: 'smooth',
        })
      }
    }, 280)
    return () => window.clearTimeout(handle)
  }, [hoverLineKey, selectedRecipeIds])

  return (
    <Stack gap="xl" className={classes.pageStack}>
      <Group justify="space-between" align="flex-end">
        <div>
          <Group gap="xs">
            <IconCalendarWeek size={28} className={classes.titleIcon} />
            <Title order={2}>Week</Title>
          </Group>
          <Text c="dimmed">{entries.length} recipes for {formatWeekLabel(weekStart)}</Text>
        </div>
        <Select
          value={weekStart}
          onChange={(value) => value && setWeekStart(value)}
          data={weekStarts.map((start) => ({ value: start, label: formatWeekLabel(start) }))}
          allowDeselect={false}
          radius="md"
          w={{ base: 220, sm: 300 }}
          aria-label="Week"
        />
      </Group>

      <Box className={classes.weekLayout}>
        <Box className={classes.recipesPanel}>
          <Group justify="space-between" mb="lg">
            <Title order={3} className={classes.sectionTitle}>
              Recipes
            </Title>
            <Text size="sm" c="dimmed">
              {entries.length}/{MAX_RECIPES_PER_WEEK}
            </Text>
          </Group>
          {entries.length === 0 ? (
            <Box className={classes.emptyState}>
              <Text fw={700}>No recipes selected</Text>
              <Text size="sm" c="dimmed">
                Add recipes from Browse to build this week.
              </Text>
            </Box>
          ) : (
            <Box className={classes.recipesScroll} ref={recipesScrollRef}>
              <SimpleGrid cols={{ base: 1, xs: 2, md: 1 }} spacing="lg">
                {entries.map((entry) => (
                  <Box
                    key={entry.recipe.id}
                    ref={(node) => {
                      if (node) recipeRefs.current.set(entry.recipe.id, node)
                      else recipeRefs.current.delete(entry.recipe.id)
                    }}
                    className={classes.compactRecipeTile}
                    onMouseEnter={() => setHoverRecipeId(entry.recipe.id)}
                    onMouseLeave={() => setHoverRecipeId(null)}
                  >
                    <RecipeCard
                      recipe={entry.recipe}
                      basketBadgeLabel={`${formatMoney(recipePrices.get(entry.recipe.id))} pp share`}
                      highlighted={selectedRecipeIds.has(entry.recipe.id)}
                      plannerEntry={entry}
                      plannerControlsVisible
                      onRemoveFromPlan={() => removeRecipeFromWeek(weekStart, entry.recipe.id)}
                      onPortionsChange={(portions) =>
                        setRecipePortions(weekStart, entry.recipe.id, portions)
                      }
                    />
                  </Box>
                ))}
              </SimpleGrid>
            </Box>
          )}
        </Box>

        <Box className={classes.basketPanel}>
          <Group justify="space-between" align="center" mb="md" wrap="nowrap">
            <Group gap="xs">
              <IconBasket size={22} className={classes.titleIcon} />
              <Title order={3} className={classes.sectionTitle}>
                Basket
              </Title>
            </Group>
            <Group gap="xs" wrap="nowrap">
              <Text size="xs" c="dimmed">
                {stockRefresh.isPending
                  ? 'Checking Ocado…'
                  : stockAge
                    ? `Stock checked ${stockAge}`
                    : 'Stock not checked'}
              </Text>
              <Button
                variant="subtle"
                size="compact-sm"
                leftSection={<IconRefresh size={16} />}
                loading={stockRefresh.isPending}
                disabled={!selections.length}
                onClick={() => stockRefresh.mutate({ selections, packOverrides })}
              >
                Refresh stock
              </Button>
            </Group>
          </Group>

          {stockRefresh.error && (
            <Alert color="red" mb="md" icon={<IconAlertCircle size={18} />}>
              {stockRefresh.error.message}
            </Alert>
          )}

          {stockRefresh.isSuccess && !stockRefresh.isPending && (
            <Alert
              color={stockRefresh.data.sold_out.length ? 'yellow' : 'teal'}
              variant="light"
              mb="md"
              icon={<IconRefresh size={18} />}
            >
              Checked {stockRefresh.data.checked} products: {stockRefresh.data.available}{' '}
              available, {stockRefresh.data.sold_out.length} sold out,{' '}
              {stockRefresh.data.restocked.length} back in stock,{' '}
              {stockRefresh.data.repriced.length} repriced.
            </Alert>
          )}

          {isError ? (
            <Alert color="red" title="Couldn't price basket" icon={<IconAlertCircle size={18} />}>
              {error?.message ?? 'Please check the backend is running and try again.'}
            </Alert>
          ) : isLoading ? (
            <Group justify="center" py="xl">
              <Loader color="fresh" />
            </Group>
          ) : (
            <>
              <Box className={classes.statsGrid}>
                <Stat label="Spend" value={formatMoney(orderCost)} tone="spend" />
                <Stat label="Waste" value={formatMoney(orderWaste)} />
                <Stat label="Score" value={formatMoney(orderCost + orderWaste)} />
                <Stat label="Portion price" value={formatMoney(basketPortionPrice)} />
              </Box>

              {data.unmapped.length > 0 && (
                <Alert
                  color="yellow"
                  variant="outline"
                  title="Unmapped ingredients"
                  icon={<IconAlertTriangle size={18} />}
                  className={classes.unmappedAlert}
                >
                  <Group gap={6}>
                    {data.unmapped.map((name) => (
                      <Badge
                        key={name}
                        color="yellow"
                        variant="outline"
                        radius="sm"
                        className={classes.warningBadge}
                      >
                        {name}
                      </Badge>
                    ))}
                  </Group>
                </Alert>
              )}

              <Stack gap="lg" className={classes.basketScroll}>
                {entries.length === 0 ? (
                  <Box className={classes.emptyState}>
                    <Text fw={700}>No basket yet</Text>
                    <Text size="sm" c="dimmed">
                      Upcoming week selections appear here.
                    </Text>
                  </Box>
                ) : (
                  <>
                    <LineTable
                      title="Online order"
                      icon={<IconBuildingStore size={20} className={classes.sectionIcon} />}
                      lines={onlineLines}
                      openLineKey={openLineKey}
                      setOpenLineKey={setOpenLineKey}
                      tableId="online"
                      hoverRecipeId={hoverRecipeId}
                      selectedLineKey={selectedLineKey}
                      setActiveLineKey={setActiveLineKey}
                      setHoverLineKey={setHoverLineKey}
                      ownedItemKeySet={ownedItemKeySet}
                      setItemOwned={setItemOwned}
                      onOpenPacks={setPackLineKey}
                      busyPackKey={
                        packPreference.isPending ? packPreference.variables?.ingredientKey : null
                      }
                    />
                    <LineTable
                      title="Source elsewhere"
                      icon={<IconHome size={20} className={classes.sectionIcon} />}
                      lines={externalLines}
                      openLineKey={openLineKey}
                      setOpenLineKey={setOpenLineKey}
                      tableId="external"
                      hoverRecipeId={hoverRecipeId}
                      selectedLineKey={selectedLineKey}
                      setActiveLineKey={setActiveLineKey}
                      setHoverLineKey={setHoverLineKey}
                      ownedItemKeySet={ownedItemKeySet}
                      setItemOwned={setItemOwned}
                      onOpenPacks={setPackLineKey}
                      busyPackKey={
                        packPreference.isPending ? packPreference.variables?.ingredientKey : null
                      }
                    />
                    <Box className={classes.bucketGrid}>
                      <NameList title="Pantry staples" names={data.staples} muted />
                      <NameList title="Mapped, not priceable" names={data.unpriceable} />
                      <NameList title="Out of stock at Ocado" names={data.sold_out} />
                    </Box>
                  </>
                )}
              </Stack>
            </>
          )}
        </Box>
      </Box>

      <PackSizeModal
        line={packLine}
        opened={packLine != null}
        onClose={() => setPackLineKey(null)}
        scope={packScope}
        setScope={setPackScope}
        onPick={pickPack}
        onReset={resetPack}
        pending={packPreference.isPending}
      />
    </Stack>
  )
}
