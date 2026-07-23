import { useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Group,
  Loader,
  NumberInput,
  Paper,
  Select,
  Stack,
  Table,
  Text,
  TextInput,
  Textarea,
  Title,
} from '@mantine/core'

import classes from './MappingReviewPage.module.css'

import {
  useMappingDetail,
  useMappingList,
  useSaveDecision,
  useSearchCandidates,
  useSetAlias,
} from '../hooks/useMappingQueries.js'

const MATCH_TYPES = [
  { value: 'exact', label: 'exact' },
  { value: 'substitute', label: 'substitute' },
  { value: 'form_differs', label: 'form differs' },
]

const STATUS_BADGE_CLASSES = {
  proposed: classes.status_proposed,
  needs_review: classes.status_needs_review,
  approved: classes.status_approved,
  rejected: classes.status_rejected,
}

function money(value) {
  return value == null ? '—' : `£${value.toFixed(2)}`
}

export default function MappingReviewPage() {
  const { key } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  // The list's filter travels in the URL, so the arrows walk exactly the list
  // that was being browsed and "back" returns to it.
  const [searchParams] = useSearchParams()
  const browseStatus = searchParams.get('status') ?? undefined
  const { data, isLoading, isError } = useMappingDetail(key)
  const save = useSaveDecision(key)
  const research = useSearchCandidates(key)
  // The remaining review queue, so a decision can jump straight to the next one.
  const { data: queue } = useMappingList('proposed')
  // Every mapping, for the "same as" dropdown.
  const { data: allItems } = useMappingList()
  // The filtered list being browsed, for prev/next.
  const { data: siblings } = useMappingList(browseStatus)
  const alias = useSetAlias(key)

  const [picks, setPicks] = useState({})
  const [eachToGrams, setEachToGrams] = useState('')
  const [needsSub, setNeedsSub] = useState(false)
  const [pantryStaple, setPantryStaple] = useState(false)
  const [notes, setNotes] = useState('')
  const [term, setTerm] = useState('')

  // Seed local editing state once the detail loads.
  useEffect(() => {
    if (!data) return
    const initial = {}
    for (const c of data.candidates) {
      initial[c.sku] = {
        accepted: c.accepted,
        rank: c.rank ?? 0,
        match_type: c.match_type ?? 'exact',
        reason: c.reason ?? '',
      }
    }
    setPicks(initial)
    setEachToGrams(data.each_to_grams ?? '')
    setNeedsSub(data.needs_substitution)
    setPantryStaple(data.pantry_staple)
    setNotes(data.reviewer_notes ?? '')
    setTerm(data.search_term ?? data.name ?? '')
  }, [data])

  // The next ingredient still awaiting review, in the same spend-sorted order as
  // the list page. Falls back to the top of the queue when the current item is
  // not in it (e.g. revisiting something already decided). Must run on every
  // render (before the loading/error early returns) to keep hook order stable.
  const nextKey = useMemo(() => {
    const items = queue?.items ?? []
    const remaining = items.filter((i) => i.ingredient_key !== key)
    if (!remaining.length) return null
    const idx = items.findIndex((i) => i.ingredient_key === key)
    if (idx === -1) return remaining[0].ingredient_key
    return (items[idx + 1] ?? remaining[0]).ingredient_key
  }, [queue, key])

  // Position in the browsed list, for the prev/next arrows.
  const nav = useMemo(() => {
    const items = siblings?.items ?? []
    const idx = items.findIndex((i) => i.ingredient_key === key)
    if (idx === -1) return { idx: -1, total: items.length, prev: null, next: null }
    return {
      idx,
      total: items.length,
      prev: idx > 0 ? items[idx - 1].ingredient_key : null,
      next: idx < items.length - 1 ? items[idx + 1].ingredient_key : null,
    }
  }, [siblings, key])

  const aliasOptions = useMemo(
    () =>
      (allItems?.items ?? [])
        .filter((i) => i.ingredient_key !== key && !i.alias_of)
        .map((i) => ({ value: i.ingredient_key, label: i.name }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [allItems, key],
  )

  if (isLoading) {
    return (
      <Group justify="center" py="xl">
        <Loader />
      </Group>
    )
  }
  if (isError || !data) {
    return (
      <Stack>
        <Anchor component={Link} to={`/mapping${location.search}`}>
          ← Back to mappings
        </Anchor>
        <Alert color="red">This ingredient has no cached product candidates.</Alert>
      </Stack>
    )
  }

  const to = (k) => `/mapping/${encodeURIComponent(k)}${location.search}`
  const acceptedCount = Object.values(picks).filter((p) => p.accepted).length
  // When aliased, this ingredient inherits another's mapping — its own inputs
  // are inert, so everything below is disabled to make that obvious.
  const isAlias = Boolean(data.alias_of)

  function toggle(sku, checked) {
    setPicks((prev) => {
      const nextRank =
        Object.values(prev).reduce((m, p) => (p.accepted ? Math.max(m, p.rank) : m), 0) + 1
      return { ...prev, [sku]: { ...prev[sku], accepted: checked, rank: checked ? nextRank : 0 } }
    })
  }

  function update(sku, field, value) {
    setPicks((prev) => ({ ...prev, [sku]: { ...prev[sku], [field]: value } }))
  }

  function submit(status) {
    const accepted = data.candidates
      .filter((c) => picks[c.sku]?.accepted)
      .map((c) => ({
        sku: c.sku,
        rank: picks[c.sku].rank || 1,
        match_type: picks[c.sku].match_type,
        reason: picks[c.sku].reason,
      }))
    save.mutate(
      {
        status,
        accepted,
        each_to_grams: eachToGrams === '' ? null : Number(eachToGrams),
        needs_substitution: needsSub,
        pantry_staple: pantryStaple,
        reviewer_notes: notes,
      },
      {
        // Advance straight to the next item in the queue so a review pass keeps
        // moving; fall back to the list once nothing is left to review.
        onSuccess: () =>
          navigate(nextKey ? to(nextKey) : `/mapping${location.search}`),
      },
    )
  }

  const u = data.usage ?? {}

  // Rendered twice — under the header and at the foot of the candidate table —
  // so a quick approve never needs a scroll past the whole list.
  function actionButtons() {
    return (
      <Group className={classes.decisionActions}>
        <Button
          className={`${classes.decisionButton} ${classes.rejectButton}`}
          variant="default"
          onClick={() => submit('rejected')}
          loading={save.isPending}
          data-darkreader-ignore
        >
          Reject
        </Button>
        <Button
          className={`${classes.decisionButton} ${classes.needsReviewButton}`}
          variant="light"
          color="yellow"
          onClick={() => submit('needs_review')}
          loading={save.isPending}
          data-darkreader-ignore
        >
          Needs review
        </Button>
        <Button
          className={`${classes.decisionButton} ${classes.approveButton}`}
          color="teal"
          onClick={() => submit('approved')}
          loading={save.isPending}
          data-darkreader-ignore
        >
          Approve
        </Button>
      </Group>
    )
  }

  return (
    <Stack gap="lg">
      <Group justify="space-between">
        <Anchor component={Link} to={`/mapping${location.search}`}>
          ← Back to mappings
          {browseStatus ? ` (${browseStatus.replace('_', ' ')})` : ''}
        </Anchor>
        {/* Flick through the list being browsed without going back to it. */}
        <Group gap="xs">
          <ActionIcon
            variant="default"
            aria-label="Previous ingredient"
            disabled={!nav.prev}
            onClick={() => nav.prev && navigate(to(nav.prev))}
          >
            ←
          </ActionIcon>
          <Text size="sm" c="dimmed" w={90} ta="center">
            {nav.idx >= 0 ? `${nav.idx + 1} of ${nav.total}` : `${nav.total} items`}
          </Text>
          <ActionIcon
            variant="default"
            aria-label="Next ingredient"
            disabled={!nav.next}
            onClick={() => nav.next && navigate(to(nav.next))}
          >
            →
          </ActionIcon>
        </Group>
      </Group>

      <Group justify="space-between" align="flex-start">
        <div>
          <Title order={2}>{data.name}</Title>
          <Text c="dimmed" size="sm">
            Used in {data.line_count.toLocaleString('en-GB')} recipe lines
            {u.median != null && ` · typically ${u.median}${u.metric_unit ?? 'g'}`}
            {u.p25 != null && u.p75 != null && ` (${u.p25}–${u.p75})`}
          </Text>
          {u.common_native_amounts && (
            <Text c="dimmed" size="xs">
              common amounts: {u.common_native_amounts}
            </Text>
          )}
        </div>
        <Group align="center" gap="sm">
          {data.status && (
            <Badge
              className={`${classes.reviewStatus} ${STATUS_BADGE_CLASSES[data.status] ?? ''}`}
              size="lg"
              variant="light"
              data-darkreader-ignore
            >
              {data.status.replace('_', ' ')}
            </Badge>
          )}
          {!isAlias && actionButtons()}
        </Group>
      </Group>

      <Paper withBorder radius="md" p="md">
        <Group align="flex-end" gap="sm">
          <Select
            label="Same as another ingredient"
            description="Link near-duplicates so they share one mapping and their demand is bought together."
            placeholder="Not an alias"
            data={aliasOptions}
            value={data.alias_of}
            onChange={(v) => alias.mutate(v)}
            searchable
            clearable
            disabled={alias.isPending}
            style={{ flex: 1 }}
          />
          {isAlias && (
            <Button
              variant="default"
              onClick={() => alias.mutate(null)}
              loading={alias.isPending}
            >
              Remove alias
            </Button>
          )}
        </Group>
        {alias.isError && (
          <Text size="xs" c="red" mt="xs">
            {alias.error?.message}
          </Text>
        )}
      </Paper>

      {isAlias && (
        <Alert color="blue" variant="light" title="This ingredient is an alias">
          It inherits the mapping for{' '}
          <Anchor component={Link} to={`/mapping/${encodeURIComponent(data.alias_of)}`}>
            {data.alias_of_name ?? data.alias_of}
          </Anchor>
          , and recipe demand for both is summed onto that product. Remove the alias to map it
          separately.
        </Alert>
      )}

      {data.llm_notes && (
        <Alert color="blue" variant="light" title={`Proposal note${data.model ? ` (${data.model})` : ''}`}>
          {data.llm_notes}
        </Alert>
      )}

      <div
        style={
          isAlias
            ? { opacity: 0.45, pointerEvents: 'none', userSelect: 'none' }
            : undefined
        }
        aria-disabled={isAlias}
      >
       <Stack gap="lg">
      <Paper withBorder radius="md" p="md">
        <Group align="flex-end" gap="sm">
          <TextInput
            label="Ocado search term"
            description="Not finding the right product? Reword and search again — results are added to the list below."
            value={term}
            onChange={(e) => setTerm(e.currentTarget.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && term.trim()) research.mutate(term.trim())
            }}
            style={{ flex: 1 }}
          />
          <Button
            onClick={() => research.mutate(term.trim())}
            loading={research.isPending}
            disabled={!term.trim()}
          >
            Search Ocado
          </Button>
        </Group>
        {research.isPending && (
          <Text size="xs" c="dimmed" mt="xs">
            Searching Ocado — this drives a real browser session, so it takes a few seconds.
          </Text>
        )}
        {research.isError && (
          <Text size="xs" c="red" mt="xs">
            Search failed: {research.error?.message}
          </Text>
        )}
      </Paper>

      <Group>
        <NumberInput
          label="Grams per unit (for count-sold items)"
          description="e.g. 1 lime ≈ 67g. Leave blank if sold by weight."
          value={eachToGrams}
          onChange={setEachToGrams}
          min={0}
          w={280}
        />
        <Checkbox
          label="Needs substitution / no direct match"
          checked={needsSub}
          onChange={(e) => setNeedsSub(e.currentTarget.checked)}
          mt="xl"
        />
        <Checkbox
          label="Pantry staple (assume already owned)"
          description="Kept mapped, but left out of the shopping basket."
          checked={pantryStaple}
          onChange={(e) => setPantryStaple(e.currentTarget.checked)}
          mt="xl"
        />
      </Group>

      {data.candidates.length === 0 && (
        <Alert color="yellow" variant="light" title="No product candidates">
          Ocado returned nothing for this ingredient's name — common for HelloFresh-specific wording
          ("21 Day Aged British Sirloin Steaks"). Reword the search above to find real products.
        </Alert>
      )}

      <Paper withBorder radius="md">
        <Table.ScrollContainer minWidth={820}>
          <Table verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={70}>Accept</Table.Th>
                <Table.Th>Product</Table.Th>
                <Table.Th>Pack</Table.Th>
                <Table.Th>Price</Table.Th>
                <Table.Th>Unit price</Table.Th>
                <Table.Th>Rating</Table.Th>
                <Table.Th w={90}>Rank</Table.Th>
                <Table.Th w={140}>Match</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {data.candidates.map((c) => {
                const pick = picks[c.sku] ?? {}
                return (
                  <Table.Tr key={c.sku} bg={pick.accepted ? 'teal.0' : undefined}>
                    <Table.Td>
                      <Checkbox
                        checked={!!pick.accepted}
                        onChange={(e) => toggle(c.sku, e.currentTarget.checked)}
                      />
                    </Table.Td>
                    <Table.Td>
                      <Anchor href={c.url ?? '#'} target="_blank" fw={600} size="sm">
                        {c.name}
                      </Anchor>
                      {c.brand && (
                        <Text size="xs" c="dimmed">
                          {c.brand}
                        </Text>
                      )}
                      {c.reason && pick.accepted && (
                        <Text size="xs" c="teal.7">
                          {c.reason}
                        </Text>
                      )}
                      {c.search_term && c.search_term !== data.name && (
                        <Badge size="xs" variant="light" color="blue" mt={2}>
                          via "{c.search_term}"
                        </Badge>
                      )}
                    </Table.Td>
                    <Table.Td>{c.pack_size_raw ?? '—'}</Table.Td>
                    <Table.Td>{money(c.price)}</Table.Td>
                    <Table.Td>
                      {c.unit_price != null ? `£${c.unit_price}/${c.unit_price_basis}` : '—'}
                    </Table.Td>
                    <Table.Td>
                      {c.avg_rating != null ? `${c.avg_rating}★ (${c.ratings_count})` : '—'}
                    </Table.Td>
                    <Table.Td>
                      {pick.accepted && (
                        <NumberInput
                          value={pick.rank || 1}
                          onChange={(v) => update(c.sku, 'rank', Number(v) || 1)}
                          min={1}
                          size="xs"
                          w={70}
                        />
                      )}
                    </Table.Td>
                    <Table.Td>
                      {pick.accepted && (
                        <Select
                          value={pick.match_type}
                          onChange={(v) => update(c.sku, 'match_type', v)}
                          data={MATCH_TYPES}
                          size="xs"
                          w={130}
                          allowDeselect={false}
                        />
                      )}
                    </Table.Td>
                  </Table.Tr>
                )
              })}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Paper>

      <Textarea
        label="Reviewer notes"
        value={notes}
        onChange={(e) => setNotes(e.currentTarget.value)}
        autosize
        minRows={2}
      />

      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          {acceptedCount} product{acceptedCount === 1 ? '' : 's'} accepted
        </Text>
        {actionButtons()}
      </Group>
       </Stack>
      </div>
    </Stack>
  )
}
