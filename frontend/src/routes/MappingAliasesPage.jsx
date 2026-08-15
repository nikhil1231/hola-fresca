import { Link } from 'react-router-dom'
import { Anchor, Button, Group, Loader, Stack, Table, Text } from '@mantine/core'

import PageHeader from '../components/PageHeader.jsx'
import { useAliases, useSetAlias } from '../hooks/useMappingQueries.js'

function AliasRow({ row }) {
  const alias = useSetAlias(row.ingredient_key)
  return (
    <Table.Tr>
      <Table.Td>
        <Anchor component={Link} to={`/mapping/${encodeURIComponent(row.ingredient_key)}`}>
          {row.name}
        </Anchor>
      </Table.Td>
      <Table.Td>
        <Anchor component={Link} to={`/mapping/${encodeURIComponent(row.alias_of)}`} fw={600}>
          {row.alias_of_name}
        </Anchor>
      </Table.Td>
      <Table.Td ta="right">
        <Button
          size="xs"
          variant="default"
          loading={alias.isPending}
          onClick={() => alias.mutate(null)}
        >
          Unlink
        </Button>
      </Table.Td>
    </Table.Tr>
  )
}

export default function MappingAliasesPage() {
  const { data, isLoading } = useAliases()
  const items = data?.items ?? []

  return (
    <Stack gap="xl">
      <PageHeader
        backLink={{ to: '/mapping', label: 'Back to mappings' }}
        title="Ingredient aliases"
        description={(
          <span>
          Near-duplicate ingredients linked onto one mapping. Aliases inherit the canonical
          ingredient's products, and recipe demand for both is summed onto the same pack instead of
          being bought twice.
          </span>
        )}
      />

      {isLoading ? (
        <Group justify="center" py="xl">
          <Loader />
        </Group>
      ) : items.length === 0 ? (
        <Text c="dimmed" ta="center" py="xl">
          No aliases yet. Open an ingredient and use “Same as another ingredient” to link it.
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={560}>
          <Table striped highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Alias</Table.Th>
                <Table.Th>Maps to</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((row) => (
                <AliasRow key={row.ingredient_key} row={row} />
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </Stack>
  )
}
