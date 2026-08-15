import { useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Collapse,
  Group,
  Loader,
  Stack,
  Table,
  Text,
} from '@mantine/core'

import ManualProductForm from '../components/ManualProductForm.jsx'
import PageHeader from '../components/PageHeader.jsx'
import {
  useDeleteManualProduct,
  useManualProducts,
  useSaveManualProduct,
} from '../hooks/useMappingQueries.js'

function ProductRow({ row }) {
  const remove = useDeleteManualProduct()
  const [editing, setEditing] = useState(false)
  const save = useSaveManualProduct()

  return (
    <>
      <Table.Tr>
        <Table.Td>
          <Text fw={500}>{row.name}</Text>
          {row.source_note ? (
            <Text fz="xs" c="dimmed">
              {row.source_note}
            </Text>
          ) : null}
        </Table.Td>
        <Table.Td>{row.pack_size_raw}</Table.Td>
        <Table.Td>£{row.price?.toFixed(2)}</Table.Td>
        <Table.Td>{row.shelf_life_days ? `${row.shelf_life_days} d` : '—'}</Table.Td>
        <Table.Td>
          {row.used_by.length === 0 ? (
            <Text fz="sm" c="dimmed">
              unused
            </Text>
          ) : (
            <Group gap={4}>
              {row.used_by.map((u) => (
                <Anchor
                  key={u.ingredient_key}
                  component={Link}
                  to={`/mapping/${encodeURIComponent(u.ingredient_key)}`}
                  fz="sm"
                >
                  <Badge variant="light" size="sm">
                    {u.name}
                  </Badge>
                </Anchor>
              ))}
            </Group>
          )}
        </Table.Td>
        <Table.Td ta="right">
          <Group gap="xs" justify="flex-end">
            <Button size="xs" variant="default" onClick={() => setEditing((v) => !v)}>
              {editing ? 'Cancel' : 'Edit'}
            </Button>
            <Button
              size="xs"
              variant="light"
              color="red"
              loading={remove.isPending}
              onClick={() => remove.mutate(row.sku)}
            >
              Delete
            </Button>
          </Group>
        </Table.Td>
      </Table.Tr>
      {editing || remove.error ? (
        <Table.Tr>
          <Table.Td colSpan={6}>
            {remove.error ? (
              <Alert color="red" variant="light" mb={editing ? 'sm' : 0}>
                {remove.error.message}
              </Alert>
            ) : null}
            <Collapse in={editing}>
              <ManualProductForm
                initial={{
                  name: row.name,
                  price: row.price ?? '',
                  pack_size_value: row.pack_size_value ?? '',
                  pack_size_unit: row.pack_size_unit ?? 'g',
                  brand: row.brand ?? '',
                  shelf_life_days: row.shelf_life_days ?? '',
                  source_note: row.source_note ?? '',
                  url: row.url ?? '',
                }}
                submitLabel="Save changes"
                pending={save.isPending}
                error={save.error?.message}
                onSubmit={(body) =>
                  save.mutate(body, { onSuccess: () => setEditing(false) })
                }
              />
            </Collapse>
          </Table.Td>
        </Table.Tr>
      ) : null}
    </>
  )
}

export default function MappingManualPage() {
  const { data, isLoading } = useManualProducts()
  const save = useSaveManualProduct()
  const [adding, setAdding] = useState(false)
  const items = data?.items ?? []

  return (
    <Stack gap="xl">
      <PageHeader
        backLink={{ to: '/mapping', label: 'Back to mappings' }}
        title="Manually sourced products"
        description={(
          <span>
          For ingredients no supermarket sells — HelloFresh's own spice blends, specialist items.
          Recording a price and pack size keeps them in the basket maths: an unmapped ingredient
          costs nothing, which would quietly make the planner favour the recipes using it. These
          are listed separately from the online order.
          </span>
        )}
      />

      <Group>
        <Button variant={adding ? 'default' : 'filled'} onClick={() => setAdding((v) => !v)}>
          {adding ? 'Cancel' : 'Add a product'}
        </Button>
      </Group>

      <Collapse in={adding}>
        <ManualProductForm
          pending={save.isPending}
          error={save.error?.message}
          onSubmit={(body) => save.mutate(body, { onSuccess: () => setAdding(false) })}
        />
      </Collapse>

      {isLoading ? (
        <Group justify="center" py="xl">
          <Loader />
        </Group>
      ) : items.length === 0 ? (
        <Text c="dimmed" ta="center" py="xl">
          Nothing here yet. Add one above, or open an ingredient the shop can't cover and use
          “Source this manually”.
        </Text>
      ) : (
        <Table.ScrollContainer minWidth={760}>
          <Table striped highlightOnHover>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Product</Table.Th>
                <Table.Th>Pack</Table.Th>
                <Table.Th>Price</Table.Th>
                <Table.Th>Shelf life</Table.Th>
                <Table.Th>Used by</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {items.map((row) => (
                <ProductRow key={row.sku} row={row} />
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </Stack>
  )
}
