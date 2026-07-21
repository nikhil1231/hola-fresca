import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Badge,
  Button,
  Group,
  Loader,
  Progress,
  SegmentedControl,
  Stack,
  Table,
  Text,
  Title,
} from '@mantine/core'

import { useBulkApprove, useMappingList } from '../hooks/useMappingQueries.js'

const STATUS_COLORS = {
  proposed: 'blue',
  approved: 'teal',
  rejected: 'red',
  needs_review: 'yellow',
  no_match: 'gray',
}

const FILTERS = [
  { label: 'All', value: 'all' },
  { label: 'Proposed', value: 'proposed' },
  { label: 'Needs review', value: 'needs_review' },
  { label: 'Approved', value: 'approved' },
  { label: 'No match', value: 'no_match' },
  { label: 'Rejected', value: 'rejected' },
]

function money(value) {
  if (value == null) return '—'
  return `£${value.toLocaleString('en-GB', { maximumFractionDigits: 0 })}`
}

export default function MappingPage() {
  const navigate = useNavigate()
  const [filter, setFilter] = useState('all')
  const status = filter === 'all' ? undefined : filter
  const { data, isLoading } = useMappingList(status)
  const bulk = useBulkApprove()

  const counts = data?.counts ?? {}
  const total = Object.values(counts).reduce((a, b) => a + b, 0)
  const approved = counts.approved ?? 0

  const proposedKeys = useMemo(
    () => (data?.items ?? []).filter((i) => i.status === 'proposed').map((i) => i.ingredient_key),
    [data],
  )

  return (
    <Stack gap="lg">
      <div>
        <Title order={2}>Ingredient → product mapping</Title>
        <Text c="dimmed">
          Confirm which Ocado products each ingredient maps to. Sorted by spend impact — review the
          top rows carefully; the obvious tail can be bulk-approved.
        </Text>
      </div>

      {total > 0 && (
        <div>
          <Group justify="space-between" mb={4}>
            <Text size="sm" c="dimmed">
              {approved} of {total} approved
            </Text>
            <Text size="sm" c="dimmed">
              {counts.proposed ?? 0} proposed · {counts.needs_review ?? 0} needs review
            </Text>
          </Group>
          <Progress value={total ? (approved / total) * 100 : 0} color="teal" />
        </div>
      )}

      <Group justify="space-between">
        <SegmentedControl value={filter} onChange={setFilter} data={FILTERS} size="sm" />
        {filter === 'proposed' && proposedKeys.length > 0 && (
          <Button
            variant="light"
            color="teal"
            loading={bulk.isPending}
            onClick={() => {
              if (window.confirm(`Approve all ${proposedKeys.length} proposed mappings as-is?`)) {
                bulk.mutate(proposedKeys)
              }
            }}
          >
            Approve all {proposedKeys.length} shown
          </Button>
        )}
      </Group>

      {isLoading ? (
        <Group justify="center" py="xl">
          <Loader />
        </Group>
      ) : (
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
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {(data?.items ?? []).map((item) => (
                <Table.Tr
                  key={item.ingredient_key}
                  style={{ cursor: 'pointer' }}
                  onClick={() =>
                    navigate(`/mapping/${encodeURIComponent(item.ingredient_key)}`)
                  }
                >
                  <Table.Td>
                    <Text fw={600}>{item.name}</Text>
                    {item.needs_substitution && (
                      <Badge size="xs" color="orange" variant="light">
                        needs substitution
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
                      {item.top_product_name ?? '—'}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge color={STATUS_COLORS[item.status] ?? 'gray'} variant="light">
                      {item.status.replace('_', ' ')}
                    </Badge>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}

      {!isLoading && (data?.items ?? []).length === 0 && (
        <Text c="dimmed" ta="center" py="xl">
          No mappings yet. Run <code>python -m app.mapping propose</code> to generate proposals.
        </Text>
      )}
    </Stack>
  )
}
