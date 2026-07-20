import {
  Button,
  Chip,
  Divider,
  Group,
  MultiSelect,
  Pill,
  SegmentedControl,
  Stack,
  Text,
} from '@mantine/core'

import RangeFilter from './RangeFilter.jsx'
import { countActiveFilters } from '../hooks/useFilters.js'

function Section({ label, children }) {
  return (
    <Stack gap="xs">
      <Text size="sm" fw={700}>
        {label}
      </Text>
      {children}
    </Stack>
  )
}

function ChipFilter({ options, selected, onToggle }) {
  return (
    <Chip.Group multiple value={selected}>
      <Group gap={6}>
        {options.map((opt) => (
          <Chip
            key={opt.value}
            value={opt.value}
            size="sm"
            radius="sm"
            color="fresh"
            variant="outline"
            onClick={() => onToggle(opt.value)}
          >
            {opt.label}
          </Chip>
        ))}
      </Group>
    </Chip.Group>
  )
}

export default function FilterPanel({ facets, filters, setScalar, setArray, toggleArrayValue, clearAll }) {
  if (!facets) return null
  const ranges = facets.ranges
  const activeCount = countActiveFilters(filters)

  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <Text fw={700} size="lg">
          Filters
        </Text>
        {activeCount > 0 && (
          <Button variant="subtle" color="gray" size="xs" onClick={clearAll}>
            Clear all ({activeCount})
          </Button>
        )}
      </Group>

      {filters.q && (
        <div>
          <Text size="xs" c="dimmed" mb={4}>
            Search
          </Text>
          <Pill
            size="md"
            withRemoveButton
            onRemove={() => setScalar('q', null)}
            styles={{
              root: {
                backgroundColor: 'var(--mantine-color-fresh-1)',
                color: 'var(--mantine-color-fresh-9)',
              },
            }}
          >
            {filters.q}
          </Pill>
        </div>
      )}

      {facets.diets.length > 0 && (
        <Section label="Diet">
          <ChipFilter
            options={facets.diets}
            selected={filters.diet ?? []}
            onToggle={(v) => toggleArrayValue('diet', v)}
          />
        </Section>
      )}

      {facets.proteins.length > 0 && (
        <Section label="Protein">
          <ChipFilter
            options={facets.proteins}
            selected={filters.protein ?? []}
            onToggle={(v) => toggleArrayValue('protein', v)}
          />
        </Section>
      )}

      {facets.attributes.length > 0 && (
        <Section label="Attributes">
          <ChipFilter
            options={facets.attributes}
            selected={filters.tag ?? []}
            onToggle={(v) => toggleArrayValue('tag', v)}
          />
        </Section>
      )}

      <Divider />

      <Section label="Difficulty">
        <SegmentedControl
          fullWidth
          size="xs"
          color="fresh"
          value={String(filters.difficulty ?? '')}
          onChange={(v) => setScalar('difficulty', v || null)}
          data={[
            { label: 'Any', value: '' },
            { label: 'Easy', value: '1' },
            { label: 'Medium', value: '2' },
          ]}
        />
      </Section>

      <RangeFilter
        label="Max cook time"
        min={10}
        max={ranges.time.max}
        step={5}
        offValue={ranges.time.max}
        value={filters.max_time}
        onCommit={(v) => setScalar('max_time', v)}
        formatValue={(v) => `${v} min`}
        marks={[
          { value: 15 },
          { value: 30 },
          { value: 45 },
          { value: 60 },
        ]}
      />

      <RangeFilter
        label="Min protein"
        min={ranges.protein.min}
        max={ranges.protein.max}
        step={5}
        offValue={ranges.protein.min}
        value={filters.min_protein}
        onCommit={(v) => setScalar('min_protein', v)}
        formatValue={(v) => `${v}g`}
        marks={[
          { value: 20 },
          { value: 40 },
          { value: 60 },
        ]}
      />

      <RangeFilter
        label="Min protein density"
        min={ranges.protein_ratio.min}
        max={ranges.protein_ratio.max}
        step={0.5}
        offValue={ranges.protein_ratio.min}
        value={filters.min_protein_ratio}
        onCommit={(v) => setScalar('min_protein_ratio', v)}
        formatValue={(v) => `${v} g/100kcal`}
        marks={[
          { value: 4 },
          { value: 6 },
          { value: 8 },
        ]}
      />

      <RangeFilter
        label="Max calories"
        min={300}
        max={ranges.kcal.max}
        step={50}
        offValue={ranges.kcal.max}
        value={filters.max_kcal}
        onCommit={(v) => setScalar('max_kcal', v)}
        formatValue={(v) => `${v} kcal`}
        marks={[
          { value: 500 },
          { value: 800 },
          { value: 1100 },
        ]}
      />

      <Divider />

      <Section label="Cuisine">
        <MultiSelect
          data={facets.cuisines.map((c) => ({ value: c.value, label: c.label }))}
          value={filters.cuisine ?? []}
          onChange={(v) => setArray('cuisine', v)}
          placeholder="Any cuisine"
          searchable
          clearable
          radius="md"
        />
      </Section>

      <Section label="Exclude">
        <MultiSelect
          data={facets.excludes.map((a) => ({ value: a.value, label: a.label }))}
          value={filters.exclude ?? []}
          onChange={(v) => setArray('exclude', v)}
          placeholder="Nothing excluded"
          searchable
          clearable
          radius="md"
        />
      </Section>
    </Stack>
  )
}
