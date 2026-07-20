import { useEffect, useState } from 'react'
import { Group, Slider, Text } from '@mantine/core'

// A slider whose "off" extreme means no constraint. Keeps a local value so the
// thumb drags smoothly, committing to the URL only on release.
export default function RangeFilter({
  label,
  min,
  max,
  step,
  offValue,
  value,
  onCommit,
  formatValue,
  marks,
}) {
  const [local, setLocal] = useState(value ?? offValue)

  useEffect(() => {
    setLocal(value ?? offValue)
  }, [value, offValue])

  const isOff = local === offValue
  return (
    <div>
      <Group justify="space-between" mb={4}>
        <Text size="sm" fw={600}>
          {label}
        </Text>
        <Text size="sm" c={isOff ? 'dimmed' : 'fresh.8'} fw={isOff ? 400 : 600}>
          {isOff ? 'Any' : formatValue(local)}
        </Text>
      </Group>
      <Slider
        value={local}
        min={min}
        max={max}
        step={step}
        marks={marks}
        color="fresh"
        label={null}
        onChange={setLocal}
        onChangeEnd={(v) => onCommit(v === offValue ? null : v)}
      />
    </div>
  )
}
