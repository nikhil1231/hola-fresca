import { useState } from 'react'
import {
  Button,
  Group,
  NumberInput,
  Select,
  Stack,
  TextInput,
  Textarea,
} from '@mantine/core'

const UNITS = [
  { value: 'g', label: 'g' },
  { value: 'ml', label: 'ml' },
  { value: 'each', label: 'per pack' },
]

const EMPTY = {
  name: '',
  price: '',
  pack_size_value: '',
  pack_size_unit: 'g',
  brand: '',
  shelf_life_days: '',
  source_note: '',
  url: '',
}

// Shared by the manual-products page and the "Ocado doesn't sell this" panel on
// the review page, so both collect exactly the fields the pack maths needs.
export default function ManualProductForm({
  initial,
  submitLabel = 'Save product',
  onSubmit,
  pending,
  error,
  children,
}) {
  const [form, setForm] = useState({ ...EMPTY, ...(initial ?? {}) })
  const set = (field) => (value) => setForm((f) => ({ ...f, [field]: value }))

  const priceOk = form.price !== '' && Number(form.price) > 0
  const packOk = form.pack_size_value !== '' && Number(form.pack_size_value) > 0
  const valid = form.name.trim() !== '' && priceOk && packOk

  function submit() {
    if (!valid) return
    onSubmit({
      name: form.name.trim(),
      price: Number(form.price),
      pack_size_value: Number(form.pack_size_value),
      pack_size_unit: form.pack_size_unit,
      brand: form.brand.trim() || null,
      shelf_life_days: form.shelf_life_days === '' ? null : Number(form.shelf_life_days),
      source_note: form.source_note.trim() || null,
      url: form.url.trim() || null,
    })
  }

  return (
    <Stack gap="sm">
      <TextInput
        label="Product name"
        placeholder="HelloFresh Thai Style Spice Mix"
        value={form.name}
        onChange={(e) => set('name')(e.currentTarget.value)}
        required
      />
      <Group grow align="flex-start">
        <NumberInput
          label="Price (£)"
          placeholder="3.50"
          value={form.price}
          onChange={set('price')}
          min={0}
          step={0.05}
          decimalScale={2}
          required
        />
        <NumberInput
          label="Pack size"
          placeholder="50"
          value={form.pack_size_value}
          onChange={set('pack_size_value')}
          min={0}
          required
        />
        <Select
          label="Unit"
          data={UNITS}
          value={form.pack_size_unit}
          onChange={(v) => set('pack_size_unit')(v ?? 'g')}
          allowDeselect={false}
        />
      </Group>
      <Group grow align="flex-start">
        <TextInput
          label="Brand"
          placeholder="optional"
          value={form.brand}
          onChange={(e) => set('brand')(e.currentTarget.value)}
        />
        <NumberInput
          label="Shelf life (days)"
          description="Blank assumes it keeps"
          placeholder="365"
          value={form.shelf_life_days}
          onChange={set('shelf_life_days')}
          min={1}
        />
      </Group>
      <Textarea
        label="Where to buy it"
        placeholder="HelloFresh box, Asian supermarket, ..."
        value={form.source_note}
        onChange={(e) => set('source_note')(e.currentTarget.value)}
        autosize
        minRows={1}
      />
      {children}
      <Group justify="flex-end">
        <Button onClick={submit} loading={pending} disabled={!valid}>
          {submitLabel}
        </Button>
      </Group>
      {error ? (
        <Group c="red" fz="sm">
          {error}
        </Group>
      ) : null}
    </Stack>
  )
}
