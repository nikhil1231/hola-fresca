import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Autocomplete,
  Badge,
  Box,
  Button,
  Group,
  Loader,
  NumberInput,
  Stack,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import {
  IconAlertCircle,
  IconCheck,
  IconPlus,
  IconSoup,
  IconTrash,
  IconX,
} from '@tabler/icons-react'

import { formatWeekStart } from '../hooks/useSchedule.js'
import { useDebouncedSearch } from '../hooks/useDebouncedSearch.js'
import {
  formatHeld,
  formatUseBy,
  heldValue,
  provenance,
  todayIso,
  unitLabel,
  useByValue,
  usePantry,
  usePantryIngredients,
  useRemovePantryItem,
  useSetPantryItem,
} from '../hooks/usePantry.js'
import PageHeader from '../components/PageHeader.jsx'
import classes from './PantryPage.module.css'

// Sensible starting amounts. Two of them because grams and units are not
// interchangeable numbers, and 500 of a thing you count is absurd.
const DEFAULT_GRAMS = 500
const DEFAULT_UNITS = 1

function ProvenanceText({ item }) {
  const { kind, week } = provenance(item)
  const useBy = formatUseBy(item.use_by)
  // A date supersedes the ageing story rather than adding to it: the lot no
  // longer shrinks each shop, it simply stops counting on the day.
  const suffix = useBy ? ` · use by ${useBy}` : ''
  if (kind === 'stated') {
    return (
      <Text size="xs" c="dimmed">
        You said so, week of {formatWeekStart(week)}
        {suffix}
      </Text>
    )
  }
  return (
    <Text size="xs" c="dimmed">
      Left over from the shop of {formatWeekStart(week)}
      {item.cycles_held > 0 &&
        ` · ${item.cycles_held} ${item.cycles_held === 1 ? 'shop' : 'shops'} ago`}
      {suffix}
    </Text>
  )
}

function PantryRow({ item, onSave, onRemove, busy }) {
  const [editing, setEditing] = useState(false)
  const [value, setValue] = useState(() => heldValue(item))
  const [useBy, setUseBy] = useState(() => useByValue(item))
  const unit = unitLabel(item.unit_kind, value)

  const start = () => {
    setValue(heldValue(item))
    setUseBy(useByValue(item))
    setEditing(true)
  }
  const commit = () => {
    setEditing(false)
    const next = Number(value)
    if (!Number.isFinite(next) || next < 0) return
    if (next === heldValue(item) && useBy === useByValue(item)) return
    onSave(item, next, useBy || null)
  }

  return (
    <Group className={classes.row} gap="sm" wrap="nowrap">
      <div className={classes.rowMain}>
        <Text size="sm" fw={600}>
          {item.name}
        </Text>
        <ProvenanceText item={item} />
      </div>

      {editing ? (
        <Group gap={4} wrap="nowrap">
          <NumberInput
            value={value}
            onChange={setValue}
            min={0}
            step={item.unit_kind === 'count' ? 1 : 50}
            suffix={` ${unit}`}
            size="xs"
            w={120}
            autoFocus
            onKeyDown={(event) => {
              if (event.key === 'Enter') commit()
              if (event.key === 'Escape') setEditing(false)
            }}
          />
          <TextInput
            type="date"
            value={useBy}
            min={todayIso()}
            onChange={(event) => setUseBy(event.currentTarget.value)}
            size="xs"
            w={140}
            aria-label={`Use ${item.name} by`}
            onKeyDown={(event) => {
              if (event.key === 'Enter') commit()
              if (event.key === 'Escape') setEditing(false)
            }}
          />
          <ActionIcon
            variant="subtle"
            color="fresh"
            size="sm"
            aria-label={`Save ${item.name}`}
            onClick={commit}
          >
            <IconCheck size={16} />
          </ActionIcon>
          <ActionIcon
            variant="subtle"
            color="gray"
            size="sm"
            aria-label="Cancel"
            onClick={() => setEditing(false)}
          >
            <IconX size={16} />
          </ActionIcon>
        </Group>
      ) : (
        <Group gap={4} wrap="nowrap">
          <Tooltip label="Change the amount" withArrow>
            <Button
              size="compact-sm"
              variant="subtle"
              color="gray"
              disabled={busy}
              onClick={start}
              aria-label={`${item.name}: ${formatHeld(item)}, change the amount`}
            >
              {formatHeld(item)}
            </Button>
          </Tooltip>
          <Tooltip label="Take it out of the pantry" withArrow>
            <ActionIcon
              variant="subtle"
              color="red"
              size="sm"
              disabled={busy}
              aria-label={`Remove ${item.name}`}
              onClick={() => onRemove(item)}
            >
              <IconTrash size={16} />
            </ActionIcon>
          </Tooltip>
        </Group>
      )}
    </Group>
  )
}

function AddItem({ onAdd, busy }) {
  // The query lags the keystrokes: `text` is what is typed, `search` is what
  // has settled, and only the latter reaches the server.
  const [search, setSearch] = useState('')
  const [text, setText] = useDebouncedSearch(search, setSearch)
  const [picked, setPicked] = useState(null)
  const [amount, setAmount] = useState(DEFAULT_GRAMS)
  const [useBy, setUseBy] = useState('')
  const { data } = usePantryIngredients(search)

  const options = useMemo(() => data?.items ?? [], [data])
  const byName = useMemo(() => {
    const map = new Map()
    for (const option of options) map.set(option.name, option)
    return map
  }, [options])

  // Grams and units are not interchangeable numbers: carrying 500 across from a
  // bag of rice to a pack of sausages offers to put 500 sausages in the
  // cupboard, and would have submitted it.
  const pick = (option) => {
    setPicked(option)
    if (option && option.unit_kind !== picked?.unit_kind) {
      setAmount(option.unit_kind === 'count' ? DEFAULT_UNITS : DEFAULT_GRAMS)
    }
    if (!option) setUseBy('')
  }

  const choose = (name) => {
    setText(name)
    pick(byName.get(name) ?? null)
  }

  const submit = () => {
    if (!picked) return
    const next = Number(amount)
    if (!Number.isFinite(next) || next <= 0) return
    onAdd(picked, next, useBy || null)
    setText('')
    setPicked(null)
    setAmount(DEFAULT_GRAMS)
    setUseBy('')
  }

  const unit = picked ? unitLabel(picked.unit_kind, amount) : 'g'

  return (
    <Box className={classes.addBox}>
      <Group gap="sm" align="flex-end" wrap="wrap">
        <Autocomplete
          label="Add an ingredient"
          placeholder="Start typing a name"
          value={text}
          onChange={(next) => {
            setText(next)
            pick(byName.get(next) ?? null)
          }}
          onOptionSubmit={choose}
          data={options.map((option) => option.name)}
          className={classes.addSearch}
          size="sm"
        />
        <NumberInput
          label="How much"
          value={amount}
          onChange={setAmount}
          min={0}
          step={picked?.unit_kind === 'count' ? 1 : 50}
          suffix={` ${unit}`}
          size="sm"
          w={150}
          onKeyDown={(event) => event.key === 'Enter' && submit()}
        />
        <TextInput
          type="date"
          label="Use by"
          placeholder="optional"
          value={useBy}
          min={todayIso()}
          onChange={(event) => setUseBy(event.currentTarget.value)}
          size="sm"
          w={165}
          disabled={!picked}
          onKeyDown={(event) => event.key === 'Enter' && submit()}
        />
        <Button
          size="sm"
          leftSection={<IconPlus size={15} />}
          disabled={!picked || busy}
          onClick={submit}
        >
          {picked?.held ? 'Update' : 'Add'}
        </Button>
      </Group>

      {picked?.perishable && (
        <Text size="xs" c="dimmed" mt="xs">
          {picked.name} does not keep. Give it a date and it counts in full
          until then, and not at all after. Without one it will have aged away
          to almost nothing by the next shop.
        </Text>
      )}
      {picked?.held && (
        <Text size="xs" c="dimmed" mt="xs">
          Already in the pantry. Adding replaces the amount rather than adding
          to it.
        </Text>
      )}
    </Box>
  )
}

export default function PantryPage() {
  const pantry = usePantry()
  const setItem = useSetPantryItem()
  const removeItem = useRemovePantryItem()
  const busy = setItem.isPending || removeItem.isPending

  const state = (ingredient, value, useBy) =>
    setItem.mutate({
      ingredientKey: ingredient.ingredient_key,
      useBy,
      ...(ingredient.unit_kind === 'count' ? { qty: value } : { grams: value }),
    })

  const items = pantry.data?.items ?? []

  return (
    <Stack gap={{ base: 'lg', sm: 'xl' }}>
      <PageHeader
        title="Pantry"
        description="What the planner thinks you already have, and takes off the next shop. Leftovers land here when a basket is pushed; correct anything it has wrong."
        icon={<IconSoup size={22} />}
        badge={
          items.length > 0 ? (
            <Badge variant="light" color="fresh" radius="sm">
              {items.length} {items.length === 1 ? 'item' : 'items'}
            </Badge>
          ) : null
        }
      />

      {(setItem.error || removeItem.error) && (
        <Alert color="red" icon={<IconAlertCircle size={18} />}>
          {(setItem.error ?? removeItem.error).message}
        </Alert>
      )}

      <AddItem onAdd={state} busy={busy} />

      {pantry.isError ? (
        <Alert color="red" title="Couldn't load the pantry" icon={<IconAlertCircle size={18} />}>
          {pantry.error?.message ?? 'Please check the backend is running and try again.'}
        </Alert>
      ) : pantry.isPaused ? (
        <Alert color="orange" title="Can't reach the backend" icon={<IconAlertCircle size={18} />}>
          The pantry will load by itself once the connection is back.
        </Alert>
      ) : !pantry.data ? (
        <Group justify="center" py="xl">
          <Loader color="fresh" />
        </Group>
      ) : items.length === 0 ? (
        <Box className={classes.emptyState}>
          <Text fw={800}>Nothing in the pantry</Text>
          <Text size="sm" c="dimmed">
            Leftovers that keep — rice, tins, spices, frozen — land here after a
            basket is pushed. You can add anything by hand above.
          </Text>
        </Box>
      ) : (
        <Stack gap={4}>
          {items.map((item) => (
            <PantryRow
              key={item.ingredient_key}
              item={item}
              onSave={state}
              onRemove={(target) => removeItem.mutate(target.ingredient_key)}
              busy={busy}
            />
          ))}
        </Stack>
      )}

      <Text size="xs" c="dimmed">
        Amounts shrink each shop by how well the food keeps, and are dropped
        once they are too old to trust. Say what got cooked on the{' '}
        <Link to="/">shops</Link> page.
      </Text>
    </Stack>
  )
}
