import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Group,
  Loader,
  Pagination,
  Paper,
  Progress,
  Select,
  SegmentedControl,
  Stack,
  Table,
  Text,
  TextInput,
  Title,
} from '@mantine/core'

import {
  useBulkApprove,
  useGenerateMappings,
  useMappingList,
  useMappingStats,
} from '../hooks/useMappingQueries.js'
import { useDebouncedSearch } from '../hooks/useDebouncedSearch.js'

const STATUS_COLORS = {
  proposed: 'blue',
  approved: 'teal',
  rejected: 'red',
  needs_review: 'yellow',
  no_match: 'gray',
  alias: 'violet',
}

const BATCH_SIZES = [
  { value: '5', label: '5' },
  { value: '10', label: '10' },
  { value: '25', label: '25' },
  { value: '50', label: '50' },
]

const FILTERS = [
  { label: 'All', value: 'all' },
  { label: 'Proposed', value: 'proposed' },
  { label: 'Needs review', value: 'needs_review' },
  { label: 'Approved', value: 'approved' },
  { label: 'No match', value: 'no_match' },
  { label: 'Rejected', value: 'rejected' },
]

const PAGE_SIZE = 100

function Stat({ label, value, sub }) {
  return (
    <div>
      <Text size="xs" c="dimmed" tt="uppercase" fw={700}>
        {label}
      </Text>
      <Text fw={700} style={{ fontSize: 22, lineHeight: 1.2 }}>
        {value ?? '-'}
      </Text>
      <Text size="xs" c="dimmed">
        {sub}
      </Text>
    </div>
  )
}

function money(value) {
  if (value == null) return '-'
  return `\u00a3${value.toLocaleString('en-GB', { maximumFractionDigits: 0 })}`
}

function Rating({ value, count }) {
  if (value == null) return null
  return (
    <Text component="span" size="xs" c="dimmed" style={{ whiteSpace: 'nowrap' }}>
      {' '}
      <Text component="span" c="yellow.7">
        {'\u2605'}
      </Text>
      {value.toFixed(1)}
      {count != null && count > 0 ? ` (${count.toLocaleString('en-GB')})` : ''}
    </Text>
  )
}

// Keep the live input state in this small component. If it lives in
// MappingPage, every keystroke re-renders the entire 100-row results table even
// though the debounced query has not changed yet.
function MappingSearch({ value, onCommit }) {
  const [searchValue, setSearchValue] = useDebouncedSearch(value, onCommit)

  return (
    <TextInput
      value={searchValue}
      onChange={(e) => setSearchValue(e.currentTarget.value)}
      placeholder="Search ingredients"
      aria-label="Search ingredients"
      size="sm"
      w={{ base: '100%', sm: 280 }}
    />
  )
}

export default function MappingPage() {
  const navigate = useNavigate()
  // The filter lives in the URL so it survives navigating into an ingredient and
  // back, and so a filtered view can be linked to directly.
  const [searchParams, setSearchParams] = useSearchParams()
  const location = useLocation()
  const filter = searchParams.get('status') ?? 'all'
  const status = filter === 'all' ? undefined : filter
  const page = Math.max(1, Number(searchParams.get('page') ?? '1') || 1)
  const q = searchParams.get('q') ?? ''
  const { data, isLoading, isFetching, isError, error } = useMappingList(status, {
    page,
    pageSize: PAGE_SIZE,
    q,
  })
  const bulk = useBulkApprove()
  const generate = useGenerateMappings()
  const { data: stats, isError: statsIsError, error: statsError } = useMappingStats()
  const [batchSize, setBatchSize] = useState('10')
  const pageCount = Math.max(1, Math.ceil((data?.total ?? 0) / PAGE_SIZE))

  useEffect(() => {
    // Wait for the first response: until then total is 0, so pageCount is 1 and
    // this would bounce any deep link past page 1 back to the start.
    if (data && page > pageCount) {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        next.set('page', String(pageCount))
        return next
      }, { replace: true })
    }
  }, [data, page, pageCount, setSearchParams])

  const setFilter = (value) =>
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value && value !== 'all') next.set('status', value)
      else next.delete('status')
      next.delete('page')
      return next
    }, { replace: true })

  const setSearch = (value) =>
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.delete('page')
      return next
    }, { replace: true })

  const setPage = (value) =>
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value > 1) next.set('page', String(value))
      else next.delete('page')
      return next
    })

  const counts = data?.counts ?? {}
  const totalQueued = Object.values(counts).reduce((a, b) => a + b, 0)
  const approved = counts.approved ?? 0

  const proposedKeys = useMemo(
    () => (data?.items ?? []).filter((i) => i.status === 'proposed').map((i) => i.ingredient_key),
    [data],
  )

  return (
    <Stack gap="lg">
      <div>
        <Title order={2}>Ingredient {'\u2192'} product mapping</Title>
        <Text c="dimmed">
          Confirm which Ocado products each ingredient maps to. Sorted by spend impact - review the
          top rows carefully; the obvious tail can be bulk-approved.
        </Text>
        <Group gap="lg">
          <Anchor component={Link} to="/mapping/aliases" size="sm">
            Manage aliases {'\u2192'}
          </Anchor>
          <Anchor component={Link} to="/mapping/manual" size="sm">
            Manually sourced products {'\u2192'}
          </Anchor>
        </Group>
      </div>

      <Paper withBorder radius="md" p="md">
        {statsIsError && (
          <Alert color="red" variant="light" mb="sm" title="Couldn't load coverage stats">
            {statsError?.message}
          </Alert>
        )}
        <Group align="flex-end" gap="xl" wrap="wrap">
          <div style={{ minWidth: 220, flex: 1 }}>
            <Text size="xs" c="dimmed" tt="uppercase" fw={700}>
              Recipe coverage
            </Text>
            <Group align="baseline" gap="xs">
              <Text fw={700} style={{ fontSize: 30, lineHeight: 1.1 }}>
                {stats ? `${stats.lines_pct}%` : '-'}
              </Text>
              <Text size="sm" c="dimmed">
                of ingredient uses
              </Text>
            </Group>
            <Progress value={stats?.lines_pct ?? 0} color="teal" mt={6} />
            <Text size="xs" c="dimmed" mt={4}>
              {stats
                ? `${stats.lines_resolved.toLocaleString('en-GB')} of ${stats.lines_total.toLocaleString('en-GB')} ingredient lines across the curated library`
                : 'loading...'}
            </Text>
          </div>

          <Stat label="Ingredients mapped" value={stats?.resolved_keys} sub={`of ${stats?.distinct_keys ?? '-'} used in recipes`} />
          <Stat label="Approved" value={approved} sub={`of ${totalQueued} in the queue`} />
          <Stat
            label="Not yet added"
            value={stats?.remaining_to_add}
            sub="available to load"
          />
        </Group>
        {(counts.proposed || counts.needs_review) && (
          <Text size="sm" c="dimmed" mt="sm">
            {counts.proposed ?? 0} proposed / {counts.needs_review ?? 0} needs review /{' '}
            {counts.no_match ?? 0} no match
          </Text>
        )}
      </Paper>

      <Group justify="space-between" align="flex-end">
        <Stack gap="xs">
          <SegmentedControl value={filter} onChange={setFilter} data={FILTERS} size="sm" />
          <MappingSearch value={q} onCommit={setSearch} />
        </Stack>
        {filter === 'proposed' && proposedKeys.length > 0 && (
          <Button
            variant="light"
            color="teal"
            loading={bulk.isPending}
            onClick={() => {
              if (window.confirm(`Approve all ${proposedKeys.length} proposed mappings on this page as-is?`)) {
                bulk.mutate(proposedKeys)
              }
            }}
          >
            Approve all {proposedKeys.length} shown
          </Button>
        )}
      </Group>

      {isError && (
        <Alert color="red" variant="light" title="Couldn't load mappings">
          {error?.message}
        </Alert>
      )}

      {isLoading ? (
        <Group justify="center" py="xl">
          <Loader />
        </Group>
      ) : !isError ? (
        <>
          <Group justify="space-between" align="center">
            <Text size="sm" c="dimmed">
              Showing {(data?.items ?? []).length.toLocaleString('en-GB')} of{' '}
              {(data?.total ?? 0).toLocaleString('en-GB')}
              {isFetching ? ' / refreshing...' : ''}
            </Text>
            {pageCount > 1 && <Pagination value={page} onChange={setPage} total={pageCount} size="sm" />}
          </Group>
          <Table.ScrollContainer minWidth={720}>
            <Table striped highlightOnHover>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Ingredient</Table.Th>
                  <Table.Th>Lines</Table.Th>
                  <Table.Th>Spend</Table.Th>
                  <Table.Th>Picked</Table.Th>
                  <Table.Th>Top product</Table.Th>
                  <Table.Th>Status</Table.Th>
                  <Table.Th />
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {(data?.items ?? []).map((item) => (
                  <Table.Tr
                    key={item.ingredient_key}
                    style={{ cursor: 'pointer' }}
                    onClick={() =>
                      // Carry the filter through so the detail page's arrows walk
                      // the same list and "back" returns to this view.
                      navigate(
                        `/mapping/${encodeURIComponent(item.ingredient_key)}${location.search}`,
                      )
                    }
                  >
                    <Table.Td>
                      <Text fw={600}>{item.name}</Text>
                      {item.needs_substitution && (
                        <Badge size="xs" color="orange" variant="light">
                          needs substitution
                        </Badge>
                      )}
                      {item.pantry_staple && (
                        <Badge size="xs" color="gray" variant="light">
                          pantry staple
                        </Badge>
                      )}
                    </Table.Td>
                    <Table.Td>{item.line_count.toLocaleString('en-GB')}</Table.Td>
                    <Table.Td>{money(item.spend_score)}</Table.Td>
                    <Table.Td>
                      {item.num_accepted}/{item.num_candidates}
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" c="dimmed" lineClamp={1}>
                        {item.top_product_name ?? '-'}
                        {item.top_product_name && (
                          <Rating
                            value={item.top_product_rating}
                            count={item.top_product_ratings_count}
                          />
                        )}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Badge color={STATUS_COLORS[item.status] ?? 'gray'} variant="light">
                        {item.status.replace('_', ' ')}
                      </Badge>
                    </Table.Td>
                    <Table.Td onClick={(e) => e.stopPropagation()}>
                      {item.status === 'proposed' && item.num_accepted > 0 && (
                        <Button
                          size="compact-xs"
                          variant="light"
                          color="teal"
                          loading={bulk.isPending && bulk.variables?.[0] === item.ingredient_key}
                          onClick={() => bulk.mutate([item.ingredient_key])}
                        >
                          Approve
                        </Button>
                      )}
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
          {pageCount > 1 && (
            <Group justify="center">
              <Pagination value={page} onChange={setPage} total={pageCount} size="sm" />
            </Group>
          )}
        </>
      ) : null}

      {!isLoading && !isError && (data?.items ?? []).length === 0 && (
        <Text c="dimmed" ta="center" py="xl">
          No mappings yet - use "Load more ingredients" below to start, or run{' '}
          <code>python -m app.mapping propose</code>.
        </Text>
      )}

      {generate.error && (
        <Alert color="red" variant="light" title="Couldn't add ingredients">
          {generate.error}
        </Alert>
      )}

      <Group justify="center" gap="sm" pb={4}>
        <Select
          value={batchSize}
          onChange={(v) => setBatchSize(v ?? '10')}
          data={BATCH_SIZES}
          disabled={generate.running}
          w={110}
          allowDeselect={false}
          aria-label="How many ingredients to add"
        />
        {/* The label must not change while loading: Mantine hides the children
            but keeps the button sized to them, so a live-updating label makes
            the spinner jitter. Progress goes underneath instead. */}
        <Button
          variant="light"
          loading={generate.running}
          onClick={() => generate.start(Number(batchSize))}
        >
          Load more ingredients
        </Button>
      </Group>

      {generate.running && (
        <Text size="sm" c="dimmed" ta="center" pb="xl">
          {generate.job
            ? `Adding ingredients... ${generate.job.processed}/${generate.job.total}${
                generate.job.current ? ` / ${generate.job.current}` : ''
              }`
            : 'Starting...'}
        </Text>
      )}

      {generate.job?.status === 'done' && (
        <Text size="sm" c="dimmed" ta="center">
          Added {generate.job.added} for review, {generate.job.staples} pantry staples,{' '}
          {generate.job.no_match} with no products found
          {generate.job.errors > 0 && `, ${generate.job.errors} failed`}.
        </Text>
      )}
    </Stack>
  )
}
