import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Badge,
  Box,
  Group,
  Loader,
  Select,
  Stack,
  Table,
  Text,
  Title,
} from '@mantine/core'
import { IconAlertCircle, IconBasket, IconBuildingStore, IconHome } from '@tabler/icons-react'

import { usePlannerBasket } from '../hooks/useRecipeQueries.js'
import {
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

function packsText(line) {
  if (!line.choices?.length) return line.note ?? '-'
  return line.choices
    .map((choice) => `${choice.count}x ${choice.pack_size_raw || formatGrams(choice.capacity_g)}`)
    .join(' + ')
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

function LineTable({ title, icon, lines }) {
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
        <Table striped highlightOnHover verticalSpacing="sm">
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
            {lines.map((line) => (
              <Table.Tr key={line.key}>
                <Table.Td>
                  <Group gap={6}>
                    <Text fw={600}>{line.name}</Text>
                    {line.trace && (
                      <Badge size="xs" color="yellow" variant="light">
                        trace
                      </Badge>
                    )}
                  </Group>
                  {line.choices?.[0]?.product_name && (
                    <Text size="xs" c="dimmed">
                      {line.choices[0].product_name}
                    </Text>
                  )}
                </Table.Td>
                <Table.Td>{formatGrams(line.need_g)}</Table.Td>
                <Table.Td>{packsText(line)}</Table.Td>
                <Table.Td>{formatGrams(line.leftover_g)}</Table.Td>
                <Table.Td>{formatMoney(line.cost)}</Table.Td>
                <Table.Td>{formatMoney(line.waste_gbp)}</Table.Td>
              </Table.Tr>
            ))}
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
  const { upcomingWeekStart, weekStarts, getWeekRecipes } = useWeeklyPlan()
  const [weekStart, setWeekStart] = useState(upcomingWeekStart)

  useEffect(() => {
    if (!weekStarts.includes(weekStart)) setWeekStart(upcomingWeekStart)
  }, [upcomingWeekStart, weekStart, weekStarts])

  const entries = getWeekRecipes(weekStart)
  const selections = useMemo(() => toPlannerSelections(entries), [entries])
  const { data, isLoading, isError, error } = usePlannerBasket(selections)
  const onlineLines = data?.lines?.filter((line) => !line.external) ?? []
  const externalLines = data?.lines?.filter((line) => line.external) ?? []

  return (
    <Stack gap="xl">
      <Group justify="space-between" align="flex-end">
        <div>
          <Group gap="xs">
            <IconBasket size={28} className={classes.titleIcon} />
            <Title order={2}>Basket</Title>
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
            <Stat label="Spend" value={formatMoney(data.cost)} tone="spend" />
            <Stat label="Waste" value={formatMoney(data.waste_gbp)} />
            <Stat label="Score" value={formatMoney(data.score)} />
            <Stat label="Untracked" value={data.untracked_lines.toLocaleString()} />
          </Box>

          {entries.length === 0 ? (
            <Box className={classes.emptyState}>
              <Text fw={700}>No recipes selected</Text>
              <Text size="sm" c="dimmed">
                Upcoming week selections appear here.
              </Text>
            </Box>
          ) : (
            <Stack gap="lg">
              <LineTable
                title="Online order"
                icon={<IconBuildingStore size={20} className={classes.sectionIcon} />}
                lines={onlineLines}
              />
              <LineTable
                title="Source elsewhere"
                icon={<IconHome size={20} className={classes.sectionIcon} />}
                lines={externalLines}
              />
              <Box className={classes.bucketGrid}>
                <NameList title="Pantry staples" names={data.staples} muted />
                <NameList title="Unmapped" names={data.unmapped} />
                <NameList title="Mapped, not priceable" names={data.unpriceable} />
              </Box>
            </Stack>
          )}
        </>
      )}
    </Stack>
  )
}
