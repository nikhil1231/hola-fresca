import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Divider,
  Group,
  Loader,
  Modal,
  SegmentedControl,
  Select,
  Stack,
  Table,
  Text,
  Tooltip,
  Title,
  UnstyledButton,
} from '@mantine/core'
import {
  IconAlertCircle,
  IconAlertTriangle,
  IconBuildingStore,
  IconArrowRight,
  IconArrowDown,
  IconCircleCheck,
  IconChevronDown,
  IconChevronRight,
  IconClock,
  IconHome,
  IconInfoCircle,
  IconLock,
  IconPackages,
  IconRefresh,
  IconStarFilled,
  IconToolsKitchen2,
} from '@tabler/icons-react'

import CheckoutPanel from '../components/CheckoutPanel.jsx'
import RecipeCard from '../components/RecipeCard.jsx'
import { useOcadoStockRefresh } from '../hooks/useOcadoQueries.js'
import { useActiveRetailer } from '../hooks/useRetailer.js'
import RetailerChip from '../components/RetailerChip.jsx'
import { useOwnedBasketItems } from '../hooks/useOwnedBasketItems.js'
import { useWeekPackChoices } from '../hooks/useWeekPackChoices.js'
import { usePackPreference, usePlannerBasket } from '../hooks/useRecipeQueries.js'
import {
  isPastWeekStart,
  resolveTargetWeek,
  useScheduleWithHistory,
} from '../hooks/useSchedule.js'
import {
  DEFAULT_RECIPES_PER_WEEK,
  formatWeekLabel,
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

function Stat({ label, value, description, support, tone = 'default' }) {
  return (
    <Box className={`${classes.stat} ${classes[tone] ?? ''}`}>
      <Group gap={5} align="center" wrap="nowrap">
        <Text className={classes.statLabel}>
          {label}
        </Text>
        {description && (
          <Tooltip label={description} withArrow position="top">
            <span className={classes.infoIcon} aria-label={`${label} explanation`} tabIndex={0}>
              <IconInfoCircle size={14} stroke={2} />
            </span>
          </Tooltip>
        )}
      </Group>
      <Text className={classes.statValue}>
        {value}
      </Text>
      {support && <Text className={classes.statDescription}>{support}</Text>}
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
        {!active && option.recommended && option.cost_delta < 0 ? (
          <Badge size="xs" variant="light" color="green">
            save {formatMoney(-option.cost_delta)}
          </Badge>
        ) : !active && cheaper ? (
          <Badge size="xs" variant="light" color="green">better value</Badge>
        ) : null}
      </Group>

      <Text size="xs" c="dimmed" lineClamp={2} mb={6}>
        {option.product_name}
      </Text>

      <Group gap={4} mb={6}>
        {option.form_differs && (
          <Badge size="xs" variant="light" color="blue">different form</Badge>
        )}
        {option.shortfall > 0 && (
          <Badge size="xs" variant="light" color="orange">
            {formatQuantity(option.shortfall, option.quantity_unit)} short · {option.shortfall_pct}%
            {option.cost_delta < 0 ? ` · save ${formatMoney(-option.cost_delta)}` : ''}
          </Badge>
        )}
      </Group>

      <Text
        fw={800}
        size="xl"
        c={cheaper || option.cost_delta < 0 ? 'green' : undefined}
        className={classes.unitCost}
      >
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
    options.find((option) => option.recommended && !option.chosen) ??
    options.find((option) => option.better_value && !option.chosen) ??
    options.find((option) => option !== current && option.unit_cost < (current?.unit_cost ?? 0))
  const held = options.find((option) => option.pinned || option.this_week)
  const rest = options.filter((option) => option !== current && option !== alternative)

  useEffect(() => {
    if (!opened) setShowAll(false)
  }, [opened])

  if (!line) return null
  const alwaysBlocksShortfall = scope === 'always'

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
                onPick={() => onPick(current)}
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
                  disabled={pending || (alwaysBlocksShortfall && alternative.shortfall > 0)}
                  onPick={() => onPick(alternative)}
                />
              </>
            )}
          </Group>

          {alwaysBlocksShortfall && options.some((option) => option.shortfall > 0) && (
            <Text size="xs" c="dimmed" mt="sm">
              Packs that leave a shortfall are available for this week only.
            </Text>
          )}

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
                  key={`${option.sku}:${option.count}:${option.shortfall}`}
                  justify="space-between"
                  wrap="nowrap"
                  className={classes.otherSize}
                >
                  <Group gap="sm" wrap="nowrap">
                    <Stack gap={0} w={150}>
                      <Text size="sm" fw={700}>
                        {option.pack_size_raw ||
                          formatCapacity(option.capacity / option.count, option.quantity_unit)}
                      </Text>
                      <Text size="xs" c="dimmed" lineClamp={1}>{option.product_name}</Text>
                    </Stack>
                    {option.form_differs && (
                      <Badge size="xs" variant="light" color="blue">different form</Badge>
                    )}
                    {option.shortfall > 0 && (
                      <Badge size="xs" variant="light" color="orange">
                        {formatQuantity(option.shortfall, option.quantity_unit)} short ·{' '}
                        {option.shortfall_pct}%
                        {option.cost_delta < 0 ? ` · save ${formatMoney(-option.cost_delta)}` : ''}
                      </Badge>
                    )}
                    <Text size="sm" c="dimmed" w={88}>{formatUnitCost(option)}</Text>
                    <Rating option={option} />
                  </Group>
                  <Group gap="sm" wrap="nowrap">
                    <Text size="sm">{formatMoney(option.cost)}</Text>
                    <Button
                      size="compact-xs"
                      variant="subtle"
                      disabled={pending || (alwaysBlocksShortfall && option.shortfall > 0)}
                      onClick={() => onPick(option)}
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
  onToggleSnap,
  readOnly = false,
  compactHeader = false,
}) {
  if (!lines.length) return null

  return (
    <Box className={classes.section}>
      {!compactHeader && (
        <Group gap="xs" mb="xs">
          {icon}
          <Title order={3} className={classes.sectionTitle}>
            {title}
          </Title>
        </Group>
      )}
      <Table.ScrollContainer minWidth={720}>
        <Table verticalSpacing="sm" className={classes.lineTable}>
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
              const packRecommendation = (line.options ?? []).find(
                (option) => option.recommended && !option.chosen,
              )
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
                        readOnly={readOnly}
                        disabled={readOnly}
                        onChange={(event) =>
                          !readOnly && setItemOwned(line.key, event.currentTarget.checked)
                        }
                        onClick={(event) => event.stopPropagation()}
                        onKeyDown={(event) => event.stopPropagation()}
                        aria-label={
                          readOnly
                            ? `${line.name} was already owned`
                            : `Mark ${line.name} as already owned`
                        }
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
                      {/* Read-only keeps the snap mark and drops the buttons:
                          that this line was cooked short is part of the record,
                          undoing it is not on offer. The pack actually bought is
                          already spelled out in the Packs column. */}
                      {readOnly && line.snapped && (
                        <Tooltip
                          label={`Cooked short, snapped to ${formatGrams(
                            line.snap?.snapped_need_g ?? line.need_g,
                          )}`}
                        >
                          <ActionIcon
                            component="span"
                            variant="subtle"
                            size="md"
                            className={`${classes.packSwapButton} ${classes.packSnapEnabled}`}
                            aria-label={`${line.name} was snapped to a smaller amount`}
                          >
                            <IconCircleCheck size={18} />
                          </ActionIcon>
                        </Tooltip>
                      )}
                      {!readOnly && line.snap && (
                        <Tooltip label={line.snapped ? `Snapped to ${formatGrams(line.snap.snapped_need_g)} — click to undo` : `Snap to ${formatGrams(line.snap.snapped_need_g)} and save ${formatMoney(line.snap.saving_gbp)}`}>
                          <ActionIcon
                            variant="subtle"
                            size="md"
                            className={`${classes.packSwapButton} ${
                              line.snapped ? classes.packSnapEnabled : classes.packSnapAvailable
                            }`}
                            aria-label={
                              line.snapped
                                ? `Snapping enabled for ${line.name}; click to undo`
                                : `Snap ${line.name} to ${formatGrams(line.snap.snapped_need_g)}`
                            }
                            onClick={(event) => { event.stopPropagation(); onToggleSnap(line.key, !line.snapped) }}
                          >
                            {line.snapped ? <IconCircleCheck size={18} /> : <IconArrowDown size={17} />}
                          </ActionIcon>
                        </Tooltip>
                      )}
                      {!readOnly && (line.options?.length ?? 0) > 1 && (
                        <Tooltip label={packRecommendation ? 'A cheaper pack option is available' : 'Change pack size'}>
                          <ActionIcon
                            variant="subtle"
                            size="md"
                            className={`${classes.packSwapButton} ${
                              packRecommendation ? classes.packSwapDeal : ''
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

function shortWeekLabel(value) {
  const date = new Date(`${value}T00:00:00`)
  if (Number.isNaN(date.getTime())) return formatWeekLabel(value)
  return new Intl.DateTimeFormat('en-GB', {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
  }).format(date)
}

function stockStatusText(stockRefresh, stockAge, retailerLabel) {
  if (stockRefresh.isPending) return `Checking ${retailerLabel ?? 'the shop'}...`
  if (stockAge) return `Stock checked ${stockAge}`
  return 'Stock not checked'
}

function BasketSummary({
  entries,
  orderCost,
  orderWaste,
  basketPortionPrice,
  totalPortions,
  weekStart,
  buyLines,
}) {
  const score = orderCost + orderWaste
  const recipeWord = entries.length === 1 ? 'recipe' : 'recipes'
  const portionWord = totalPortions === 1 ? 'portion' : 'portions'

  return (
    <Box className={classes.summaryCard}>
      <div className={classes.summaryTop}>
        <div>
          <Group gap="xs" align="center">
            <Title order={1} className={classes.summaryTitle}>Basket</Title>
            <RetailerChip />
          </Group>
          <Text className={classes.summarySubtitle}>
            {entries.length} {recipeWord} · {totalPortions} {portionWord} · {formatWeekLabel(weekStart)}
          </Text>
        </div>
        <Text className={classes.summaryMeta}>
          {entries.length} {recipeWord} · {buyLines.length} items · {totalPortions} {portionWord}
        </Text>
      </div>
      <Box className={classes.statsGrid}>
        <Stat
          label="Spend"
          value={formatMoney(orderCost)}
          support={`${buyLines.length} items, online order`}
          description="Estimated total cost of basket items not marked as owned."
          tone="spend"
        />
        <Stat
          label="Waste"
          value={formatMoney(orderWaste)}
          support={`${Math.round((orderWaste / Math.max(orderCost, 0.01)) * 100)}% of spend`}
          description="Estimated value of unused pack leftovers after this week's recipes."
        />
        <Stat
          label="Score"
          value={formatMoney(score)}
          support="spend + waste"
          description="Spend plus waste, used to compare basket efficiency."
        />
        <Stat
          label="Per portion"
          value={formatMoney(basketPortionPrice)}
          support={`${formatMoney(orderCost)} ÷ ${Math.max(totalPortions, 1)}`}
          description="Estimated spend divided by the total planned portions."
          tone="portion"
        />
      </Box>
    </Box>
  )
}

function BasketControls({
  pageView,
  setPageView,
  weekStart,
  setWeekStart,
  weekOptions,
  canCheckout,
}) {
  return (
    <Group gap="sm" wrap="nowrap" className={classes.controls}>
      {/* Checkout drives the retailer's own trolley, which only a shoppable
          retailer has. Hiding the tab beats showing one that leads nowhere. */}
      {canCheckout && (
        <SegmentedControl
          size="md"
          value={pageView}
          onChange={setPageView}
          data={[
            { label: 'Basket', value: 'basket' },
            { label: 'Checkout', value: 'checkout' },
          ]}
          className={classes.viewToggle}
          aria-label="Basket view"
        />
      )}
      <Select
        value={weekStart}
        onChange={(value) => value && setWeekStart(value)}
        data={weekOptions.map((start) => ({ value: start, label: shortWeekLabel(start) }))}
        allowDeselect={false}
        radius="xl"
        className={classes.weekSelect}
        aria-label="Week"
      />
    </Group>
  )
}

function RecipeRail({
  entries,
  recipesPerWeek,
  recipesScrollRef,
  recipeRefs,
  recipePrices,
  glowRecipeIds,
  setHoverRecipeId,
  removeRecipeFromWeek,
  setRecipePortions,
  weekStart,
}) {
  return (
    <Box className={classes.recipesPanel}>
      <Group justify="space-between" className={classes.panelHeading}>
        <Title order={3} className={classes.panelTitle}>Recipes</Title>
        <Text className={classes.panelCount}>{entries.length}/{recipesPerWeek}</Text>
      </Group>
      {entries.length === 0 ? (
        <Box className={classes.emptyState}>
          <Text fw={800}>No recipes selected</Text>
          <Text size="sm" c="dimmed">Add recipes from Browse to build this week.</Text>
        </Box>
      ) : (
        <Box className={classes.recipesScroll} ref={recipesScrollRef}>
          <div className={classes.recipeRail}>
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
                  basketBadgeLabel={`${formatMoney(recipePrices.get(entry.recipe.id))} pp`}
                  highlighted={glowRecipeIds.has(entry.recipe.id)}
                  plannerEntry={entry}
                  plannerControlsVisible
                  onRemoveFromPlan={() => removeRecipeFromWeek(weekStart, entry.recipe.id)}
                  onPortionsChange={(portions) =>
                    setRecipePortions(weekStart, entry.recipe.id, portions)
                  }
                  showStats
                />
              </Box>
            ))}
            <Link to="/browse" className={classes.addRecipeTile}>
              Add a recipe
            </Link>
          </div>
        </Box>
      )}
    </Box>
  )
}

function MobileLineCard({
  line,
  rowKey,
  onOpenPacks,
  busyPackKey,
  owned,
  setItemOwned,
  setActiveLineKey,
  setHoverLineKey,
  onToggleSnap,
}) {
  const canExpand = line.choices?.length > 0
  const packRecommendation = (line.options ?? []).find((option) => option.recommended && !option.chosen)
  const heldOption = (line.options ?? []).find((option) => option.pinned || option.this_week)
  const canSwap = (line.options?.length ?? 0) > 1
  const summary = `${formatQuantity(line.need_qty ?? line.need_g, line.quantity_unit)} · ${packsText(
    line,
  )} → ${formatQuantity(line.leftover_qty ?? line.leftover_g, line.quantity_unit)} left`

  return (
    <div
      className={`${classes.mobileLineCard} ${owned ? classes.mobileLineOwned : ''}`}
      onMouseEnter={() => setHoverLineKey(rowKey)}
      onMouseLeave={() => setHoverLineKey(null)}
      onClick={() => setActiveLineKey(rowKey)}
      role={canExpand ? 'button' : undefined}
      tabIndex={canExpand ? 0 : undefined}
    >
      <Checkbox
        checked={owned}
        className={classes.mobileLineCheck}
        onChange={(event) => setItemOwned(line.key, event.currentTarget.checked)}
        onClick={(event) => event.stopPropagation()}
        aria-label={`Mark ${line.name} as already owned`}
      />
      <div className={classes.mobileLineMain}>
        <Group gap={6} align="center">
          <Text className={classes.mobileLineName}>{line.name}</Text>
          {heldOption && (
            <span className={classes.packTag}>{heldOption.pack_size_raw || 'chosen'}</span>
          )}
          {line.trace && <span className={classes.traceTag}>trace</span>}
        </Group>
        <Text className={classes.mobileLineSummary}>{summary}</Text>
        <Group gap={8} className={classes.mobileLineActions}>
          {line.snap && (
            <UnstyledButton
              className={classes.mobileSwapAction}
              onClick={(event) => {
                event.stopPropagation()
                onToggleSnap(line.key, !line.snapped)
              }}
            >
              {line.snapped ? 'Snap owned' : 'Snap available'}
            </UnstyledButton>
          )}
          {canSwap && (
            <UnstyledButton
              className={classes.mobileSwapAction}
              disabled={busyPackKey === line.key}
              onClick={(event) => {
                event.stopPropagation()
                onOpenPacks(line.key)
              }}
            >
              {packRecommendation ? 'Swap available' : 'Change pack'}
            </UnstyledButton>
          )}
        </Group>
      </div>
      <div className={classes.mobileLinePrice}>
        {owned ? (
          <Text className={classes.mobileOwnedText}>Owned</Text>
        ) : (
          <>
            <Text className={classes.mobileCost}>{formatMoney(line.cost)}</Text>
            <Text className={line.waste_gbp > 1 ? classes.mobileWasteHot : classes.mobileWaste}>
              {formatMoney(line.waste_gbp)} waste
            </Text>
          </>
        )}
      </div>
    </div>
  )
}

function MobileLineList({
  lines,
  tableId,
  onOpenPacks,
  busyPackKey,
  ownedItemKeySet,
  setItemOwned,
  setActiveLineKey,
  setHoverLineKey,
  onToggleSnap,
}) {
  if (!lines.length) return null

  return (
    <div className={classes.mobileLineList}>
      {lines.map((line) => {
        const rowKey = `${tableId}:${line.key}`
        return (
          <MobileLineCard
            key={rowKey}
            line={line}
            rowKey={rowKey}
            onOpenPacks={onOpenPacks}
            busyPackKey={busyPackKey}
            owned={ownedItemKeySet.has(line.key)}
            setItemOwned={setItemOwned}
            setActiveLineKey={setActiveLineKey}
            setHoverLineKey={setHoverLineKey}
            onToggleSnap={onToggleSnap}
          />
        )
      })}
    </div>
  )
}

function OrderPanelHeader({
  title,
  subtitle,
  itemCount,
  stockText,
  stockRefresh,
  selections,
  packOverrides,
  snapOverrides,
}) {
  return (
    <Group justify="space-between" align="flex-start" className={classes.orderHeader}>
      <div>
        <Group gap="xs" align="baseline">
          <Title order={3} className={classes.orderTitle}>{title}</Title>
          <Text className={classes.orderCount}>{itemCount} items</Text>
        </Group>
        {subtitle && <Text className={classes.orderSubtitle}>{subtitle}</Text>}
      </div>
      <Group gap="md" align="center" wrap="nowrap" className={classes.stockControls}>
        <Text className={classes.stockText}>{stockText}</Text>
        <Button
          variant="outline"
          size="sm"
          radius="lg"
          className={classes.refreshButton}
          loading={stockRefresh.isPending}
          disabled={!selections.length}
          onClick={() => stockRefresh.mutate({ selections, packOverrides, snapOverrides })}
        >
          Refresh stock
        </Button>
      </Group>
    </Group>
  )
}

export default function BasketPage() {
  const {
    upcomingWeekStart,
    weekStarts,
    plannedWeekStarts,
    getWeekRecipes,
    removeRecipeFromWeek,
    setRecipePortions,
  } = useWeeklyPlan()
  const [searchParams, setSearchParams] = useSearchParams()
  // With history, because the week in the URL may be a shop that has already
  // happened — that is the point of a "Basket" link on a past week.
  const { data: schedule } = useScheduleWithHistory()
  // The week lives in the URL, so a "Basket" link from Home opens the week it
  // was clicked for and the page stays linkable.
  const targetWeek = resolveTargetWeek(schedule, searchParams.get('week'))
  const weekStart = targetWeek?.week_start ?? upcomingWeekStart
  const requestedView = searchParams.get('view') === 'checkout' ? 'checkout' : 'basket'
  // A basket that has been shopped for is a record of what was bought: the packs
  // chosen, the demands snapped, the lines skipped as already owned. Changing any
  // of it now would only lose the account of the shop, so the page shows it and
  // touches nothing. The server agrees, and refuses the writes outright.
  const readOnly = isPastWeekStart(weekStart)
  const recipesPerWeek = schedule?.settings?.recipes_per_week ?? DEFAULT_RECIPES_PER_WEEK
  const setWeekStart = useCallback(
    (value) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        next.set('week', value)
        return next
      })
    },
    [setSearchParams],
  )
  const setPageView = useCallback(
    (value) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        if (value === 'checkout') next.set('view', 'checkout')
        else next.delete('view')
        return next
      })
    },
    [setSearchParams],
  )
  // Scheduled weeks first, then any week still holding recipes that the current
  // cadence no longer lands on — those baskets are real and still need opening.
  // Finished weeks are in there too: their shop is done, but looking up what was
  // in it is exactly what the picker is for.
  const weekOptions = useMemo(() => {
    const scheduled = (schedule?.weeks ?? []).map((week) => week.week_start)
    return [
      ...new Set([...scheduled, ...weekStarts, ...plannedWeekStarts, weekStart]),
    ].sort()
  }, [schedule?.weeks, weekStarts, plannedWeekStarts, weekStart])
  const [openLineKey, setOpenLineKey] = useState(null)
  const [activeLineKey, setActiveLineKey] = useState(null)
  const [hoverLineKey, setHoverLineKey] = useState(null)
  const [glowLineKey, setGlowLineKey] = useState(null)
  const [hoverRecipeId, setHoverRecipeId] = useState(null)
  const { ownedItemKeys, ownedItemKeySet, setItemOwned } = useOwnedBasketItems(weekStart)
  const {
    packOverrides,
    snapOverrides,
    setWeekSnap,
    setWeekPackAndSnap,
  } = useWeekPackChoices(weekStart)
  const stockRefresh = useOcadoStockRefresh()
  const { label: retailerLabel, shoppable: retailerShoppable } = useActiveRetailer()
  // A ?view=checkout link survives a switch to a shop with no trolley, so the
  // view falls back rather than leaving an empty panel with no way out of it.
  const pageView = retailerShoppable ? requestedView : 'basket'
  const packPreference = usePackPreference()
  const [packLineKey, setPackLineKey] = useState(null)
  const [packScope, setPackScope] = useState('week')
  const recipesScrollRef = useRef(null)
  const recipeRefs = useRef(new Map())

  const entries = getWeekRecipes(weekStart)
  const selections = useMemo(() => toPlannerSelections(entries), [entries])
  const { data, isLoading, isError, error } = usePlannerBasket(selections, packOverrides, snapOverrides)
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
    (option) => {
      if (!packLine) return
      if (packScope === 'always') {
        if (option.shortfall > 0) return
        setWeekPackAndSnap(packLine.key, null, false)
        packPreference.mutate({ ingredientKey: packLine.key, sku: option.sku })
      } else {
        setWeekPackAndSnap(packLine.key, option.sku, option.shortfall > 0)
      }
      setPackLineKey(null)
    },
    [packLine, packScope, packPreference, setWeekPackAndSnap],
  )

  const resetPack = useCallback(() => {
    if (!packLine) return
    setWeekPackAndSnap(packLine.key, null, false)
    if (packLine.options?.some((option) => option.pinned)) {
      packPreference.mutate({ ingredientKey: packLine.key, sku: null })
    }
    setPackLineKey(null)
  }, [packLine, packPreference, setWeekPackAndSnap])
  const basketPortionPrice = totalPortions > 0 ? orderCost / totalPortions : 0
  const selectedLineKey = hoverLineKey ?? activeLineKey
  const allLines = useMemo(
    () => [
      ...onlineLines.map((line) => [`online:${line.key}`, line]),
      ...externalLines.map((line) => [`external:${line.key}`, line]),
    ],
    [onlineLines, externalLines],
  )
  const glowRecipeIds = useMemo(() => {
    const line = allLines.find(([key]) => key === glowLineKey)?.[1]
    return contributionIds(line)
  }, [allLines, glowLineKey])

  useEffect(() => {
    setOpenLineKey(null)
    setActiveLineKey(null)
    setHoverLineKey(null)
    setGlowLineKey(null)
    setHoverRecipeId(null)
  }, [weekStart, selections])

  useEffect(() => {
    if (!hoverLineKey) {
      setGlowLineKey(activeLineKey)
      return undefined
    }
    const handle = window.setTimeout(() => {
      setGlowLineKey(hoverLineKey)
    }, 280)
    return () => window.clearTimeout(handle)
  }, [activeLineKey, hoverLineKey])

  useEffect(() => {
    if (!hoverLineKey || glowRecipeIds.size === 0) return undefined
    const recipeId = glowRecipeIds.values().next().value
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
    }, 40)
    return () => window.clearTimeout(handle)
  }, [hoverLineKey, glowRecipeIds])

  const orderStockText = stockStatusText(stockRefresh, stockAge, retailerLabel)
  const busyPackKey = packPreference.isPending ? packPreference.variables?.ingredientKey : null

  return (
    <main className={classes.pageStack}>
      <div className={classes.mobileStatusBar} aria-hidden="true">
        <span>9:41</span>
        <span className={classes.statusPills}>
          <span />
          <span />
          <span />
        </span>
      </div>

      <div className={classes.pageFrame}>
        <RecipeRail
          entries={entries}
          recipesPerWeek={recipesPerWeek}
          recipesScrollRef={recipesScrollRef}
          recipeRefs={recipeRefs}
          recipePrices={recipePrices}
          glowRecipeIds={glowRecipeIds}
          setHoverRecipeId={setHoverRecipeId}
          removeRecipeFromWeek={removeRecipeFromWeek}
          setRecipePortions={setRecipePortions}
          weekStart={weekStart}
          readOnly={readOnly}
        />

        <section className={classes.mainColumn}>
          <div className={classes.mobileTopRow}>
            <Group gap="xs" align="center">
              <Title order={1} className={classes.mobilePageTitle}>Basket</Title>
              <RetailerChip size="xs" />
              {readOnly && (
                <Badge color="gray" variant="light" radius="sm" leftSection={<IconLock size={12} />}>
                  Past
                </Badge>
              )}
            </Group>
            <Select
              value={weekStart}
              onChange={(value) => value && setWeekStart(value)}
              data={weekOptions.map((start) => ({
                value: start,
                label: isPastWeekStart(start) ? `${shortWeekLabel(start)} · past` : shortWeekLabel(start),
              }))}
              allowDeselect={false}
              radius="xl"
              className={classes.mobileWeekSelect}
              aria-label="Week"
            />
          </div>

          <BasketControls
            pageView={pageView}
            canCheckout={retailerShoppable}
            setPageView={setPageView}
            weekStart={weekStart}
            setWeekStart={setWeekStart}
            weekOptions={weekOptions}
          />

          <BasketSummary
            entries={entries}
            orderCost={orderCost}
            orderWaste={orderWaste}
            basketPortionPrice={basketPortionPrice}
            totalPortions={totalPortions}
            weekStart={weekStart}
            buyLines={buyLines}
          />

          {stockRefresh.error && (
            <Alert color="red" icon={<IconAlertCircle size={18} />} className={classes.alertCard}>
              {stockRefresh.error.message}
            </Alert>
          )}

          {stockRefresh.isSuccess && !stockRefresh.isPending && (
            <Alert
              color={stockRefresh.data.sold_out.length ? 'yellow' : 'teal'}
              variant="light"
              icon={<IconRefresh size={18} />}
              className={classes.alertCard}
            >
              Checked {stockRefresh.data.checked} products: {stockRefresh.data.available}{' '}
              available, {stockRefresh.data.sold_out.length} sold out,{' '}
              {stockRefresh.data.restocked.length} back in stock,{' '}
              {stockRefresh.data.repriced.length} repriced.
            </Alert>
          )}

          <Box className={classes.basketPanel}>
            {pageView === 'checkout' ? (
              <CheckoutPanel
                lines={data?.lines ?? []}
                ownedItemKeys={ownedItemKeys}
                ownedItemKeySet={ownedItemKeySet}
                packOverrides={packOverrides}
                selections={selections}
                snapOverrides={snapOverrides}
                unmapped={data?.unmapped ?? []}
                soldOut={data?.sold_out ?? []}
                weekStart={weekStart}
              />
            ) : isError ? (
              <Alert color="red" title="Couldn't price basket" icon={<IconAlertCircle size={18} />}>
                {error?.message ?? 'Please check the backend is running and try again.'}
              </Alert>
            ) : isLoading ? (
              <Group justify="center" py="xl">
                <Loader color="fresh" />
              </Group>
            ) : entries.length === 0 ? (
              <Box className={classes.emptyState}>
                <Text fw={800}>No basket yet</Text>
                <Text size="sm" c="dimmed">Upcoming week selections appear here.</Text>
              </Box>
            ) : (
              <div className={classes.orderPanel}>
                <OrderPanelHeader
                  title="Online order"
                  subtitle={entries.map((entry) => entry.recipe.name).join(', ')}
                  itemCount={onlineLines.length}
                  stockText={orderStockText}
                  stockRefresh={stockRefresh}
                  selections={selections}
                  packOverrides={packOverrides}
                  snapOverrides={snapOverrides}
                />

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

                <div className={classes.desktopTables}>
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
                    onToggleSnap={setWeekSnap}
                    onOpenPacks={setPackLineKey}
                    busyPackKey={busyPackKey}
                    readOnly={readOnly}
                    compactHeader
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
                    onToggleSnap={setWeekSnap}
                    onOpenPacks={setPackLineKey}
                    busyPackKey={busyPackKey}
                    readOnly={readOnly}
                  />
                </div>

                <div className={classes.mobileOrderLists}>
                  <MobileLineList
                    lines={onlineLines}
                    tableId="online"
                    onOpenPacks={setPackLineKey}
                    busyPackKey={busyPackKey}
                    ownedItemKeySet={ownedItemKeySet}
                    setItemOwned={setItemOwned}
                    setActiveLineKey={setActiveLineKey}
                    setHoverLineKey={setHoverLineKey}
                    onToggleSnap={setWeekSnap}
                  />
                  <MobileLineList
                    lines={externalLines}
                    tableId="external"
                    onOpenPacks={setPackLineKey}
                    busyPackKey={busyPackKey}
                    ownedItemKeySet={ownedItemKeySet}
                    setItemOwned={setItemOwned}
                    setActiveLineKey={setActiveLineKey}
                    setHoverLineKey={setHoverLineKey}
                    onToggleSnap={setWeekSnap}
                  />
                </div>

                <Group justify="flex-end" className={classes.mobileTotalRow}>
                  <Text>Total</Text>
                  <Text>{formatMoney(orderWaste)} waste</Text>
                  <Text>{formatMoney(orderCost)}</Text>
                </Group>

                <Box className={classes.bucketGrid}>
                  <NameList title="Pantry staples" names={data.staples} muted />
                  <NameList title="Mapped, not priceable" names={data.unpriceable} />
                  <NameList title={`Out of stock at ${retailerLabel ?? 'the shop'}`} names={data.sold_out} />
                </Box>
              </div>
            )}
          </Box>
        </section>
      </div>

      {pageView === 'basket' && !isError && !isLoading && entries.length > 0 && (
        <button className={classes.mobileCheckoutBar} onClick={() => setPageView('checkout')}>
          <span>Checkout</span>
          <strong>{formatMoney(orderCost)}</strong>
        </button>
      )}

      <PackSizeModal
        line={packLine}
        opened={packLine != null && !readOnly}
        onClose={() => setPackLineKey(null)}
        scope={packScope}
        setScope={setPackScope}
        onPick={pickPack}
        onReset={resetPack}
        pending={packPreference.isPending}
      />
    </main>
  )
}
