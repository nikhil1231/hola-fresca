import { Fragment, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Group,
  Loader,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Text,
  Title,
} from '@mantine/core'
import {
  IconAlertCircle,
  IconAlertTriangle,
  IconBasket,
  IconBuildingStore,
  IconCalendarWeek,
  IconChevronRight,
  IconHome,
} from '@tabler/icons-react'

import RecipeCard from '../components/RecipeCard.jsx'
import { usePlannerBasket } from '../hooks/useRecipeQueries.js'
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

function packsText(line) {
  if (!line.choices?.length) return line.note ?? '-'
  return line.choices
    .map((choice) => `${choice.count}x ${choice.pack_size_raw || formatGrams(choice.capacity_g)}`)
    .join(' + ')
}

function contributionIds(line) {
  return new Set((line?.contributions ?? []).map((contribution) => contribution.recipe_id))
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

function LineTable({
  title,
  icon,
  lines,
  openLineKey,
  setOpenLineKey,
  tableId,
  hoverRecipeId,
  selectedLineKey,
  setActiveLineKey,
  setHoverLineKey,
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
            <col className={classes.colIngredient} />
            <col className={classes.colNeed} />
            <col className={classes.colPacks} />
            <col className={classes.colLeft} />
            <col className={classes.colCost} />
            <col className={classes.colWaste} />
          </colgroup>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Ingredient</Table.Th>
              <Table.Th>Need</Table.Th>
              <Table.Th>Packs</Table.Th>
              <Table.Th>Left</Table.Th>
              <Table.Th>Cost</Table.Th>
              <Table.Th>Waste</Table.Th>
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

              return (
                <Fragment key={rowKey}>
                  <Table.Tr
                    className={[
                      canExpand ? classes.expandableRow : '',
                      highlighted ? classes.highlightedRow : '',
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
                    <Table.Td>{formatGrams(line.need_g)}</Table.Td>
                    <Table.Td>{packsText(line)}</Table.Td>
                    <Table.Td>{formatGrams(line.leftover_g)}</Table.Td>
                    <Table.Td>{formatMoney(line.cost)}</Table.Td>
                    <Table.Td>{formatMoney(line.waste_gbp)}</Table.Td>
                  </Table.Tr>

                  {canExpand && (
                    <Table.Tr className={classes.expansionRow} aria-hidden={!expanded}>
                      <Table.Td colSpan={6} className={classes.expansionCell}>
                        <div
                          className={`${classes.expansionShell} ${
                            expanded ? classes.expansionShellOpen : ''
                          }`}
                        >
                          <div className={classes.choiceList}>
                            {line.choices.map((choice) => (
                              <div key={`${rowKey}:${choice.sku}`} className={classes.choiceItem}>
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

  useEffect(() => {
    if (!weekStarts.includes(weekStart)) setWeekStart(upcomingWeekStart)
  }, [upcomingWeekStart, weekStart, weekStarts])

  const entries = getWeekRecipes(weekStart)
  const selections = useMemo(() => toPlannerSelections(entries), [entries])
  const { data, isLoading, isError, error } = usePlannerBasket(selections)
  const onlineLines = data?.lines?.filter((line) => !line.external) ?? []
  const externalLines = data?.lines?.filter((line) => line.external) ?? []
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

  return (
    <Stack gap="xl">
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
          <Group justify="space-between" mb="md">
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
            <SimpleGrid cols={{ base: 1, xs: 2, md: 1 }} spacing="md">
              {entries.map((entry) => (
                <Box
                  key={entry.recipe.id}
                  onMouseEnter={() => setHoverRecipeId(entry.recipe.id)}
                  onMouseLeave={() => setHoverRecipeId(null)}
                >
                  <RecipeCard
                    recipe={entry.recipe}
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
          )}
        </Box>

        <Box className={classes.basketPanel}>
          <Group gap="xs" mb="md">
            <IconBasket size={22} className={classes.titleIcon} />
            <Title order={3} className={classes.sectionTitle}>
              Basket
            </Title>
          </Group>

          {isError ? (
            <Alert color="red" title="Couldn't price basket" icon={<IconAlertCircle size={18} />}>
              {error?.message ?? 'Please check the backend is running and try again.'}
            </Alert>
          ) : isLoading ? (
            <Group justify="center" py="xl">
              <Loader color="fresh" />
            </Group>
          ) : (
            <Stack gap="lg">
              <Box className={classes.statsGrid}>
                <Stat label="Spend" value={formatMoney(data.cost)} tone="spend" />
                <Stat label="Waste" value={formatMoney(data.waste_gbp)} />
                <Stat label="Score" value={formatMoney(data.score)} />
                <Stat label="Untracked" value={data.untracked_lines.toLocaleString()} />
              </Box>

              {data.unmapped.length > 0 && (
                <Alert
                  color="yellow"
                  variant="light"
                  title="Unmapped ingredients"
                  icon={<IconAlertTriangle size={18} />}
                >
                  <Group gap={6}>
                    {data.unmapped.map((name) => (
                      <Badge key={name} color="yellow" variant="light" radius="sm">
                        {name}
                      </Badge>
                    ))}
                  </Group>
                </Alert>
              )}

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
                  />
                  <Box className={classes.bucketGrid}>
                    <NameList title="Pantry staples" names={data.staples} muted />
                    <NameList title="Mapped, not priceable" names={data.unpriceable} />
                  </Box>
                </>
              )}
            </Stack>
          )}
        </Box>
      </Box>
    </Stack>
  )
}
