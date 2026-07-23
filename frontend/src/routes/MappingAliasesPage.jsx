import { Link } from 'react-router-dom'
import { Anchor, Button, Group, Loader, Stack, Table, Text, Title } from '@mantine/core'

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
    <Stack gap="lg">
      <Anchor component={Link} to="/mapping">
        ← Back to mappings
      </Anchor>

      <div>
        <Title order={2}>Ingredient aliases</Title>
        <Text c="dimmed">
          Near-duplicate ingredients linked onto one mapping. Aliases inherit the canonical
          ingredient's products, and recipe demand for both is summed onto the same pack instead of
          being bought twice.
        </Text>
      </div>

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
