import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Accordion,
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  Group,
  Image,
  Loader,
  NumberInput,
  Paper,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  TextInput,
} from '@mantine/core'
import { IconSnowflake } from '@tabler/icons-react'

import classes from './MappingReviewPage.module.css'
import ManualProductForm from '../components/ManualProductForm.jsx'
import RecipeCard from '../components/RecipeCard.jsx'

import {
  useAliasOptions,
  useMappingDetail,
  useMappingList,
  useResolveWithManualProduct,
  useSaveDecision,
  useSearchCandidates,
  useSetAlias,
} from '../hooks/useMappingQueries.js'
import { useActiveRetailer } from '../hooks/useRetailer.js'
import PageHeader from '../components/PageHeader.jsx'
import {
  canonicalUnitPriceBasis,
  comparisonDescription,
  comparisonPalette,
  formatRating,
  ratingDescription,
  ratingQualityScore,
  relativeQualityScores,
  sortUnitPrice,
} from './mappingComparison.js'

//: The candidate tabs split "the shop sells this" from "sourced by hand". The
//: first used to be spelled 'ocado'; it is the active shop now, whichever that
//: is, so the tab value is a fixed token and only its label moves.
const RETAILER_TAB = 'retailer'

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

function MetricPill({ children, description, metric, score, unavailable = false }) {
  const comparison = description ?? comparisonDescription(score)
  const valueDescription = unavailable ? `${metric} unavailable` : `${metric} ${children}`
  return (
    <span
      className={classes.metricPill}
      style={comparisonPalette(score)}
      aria-label={`${valueDescription}; ${comparison}`}
      data-darkreader-ignore
    >
      {children}
    </span>
  )
}

export default function MappingReviewPage() {
  const { key } = useParams()
  const navigate = useNavigate()
  const location = useLocation()
  // The list's filter travels in the URL, so the arrows walk exactly the list
  // that was being browsed and "back" returns to it.
  const [searchParams] = useSearchParams()
  const browseStatus = searchParams.get('status') ?? undefined
  const browseQ = searchParams.get('q') ?? ''
  const { data, isLoading, isError } = useMappingDetail(key)
  const save = useSaveDecision(key)
  const research = useSearchCandidates(key)
  // The remaining review queue, so a decision can jump straight to the next one.
  const { data: queue } = useMappingList('proposed', { pageSize: 1000 })
  // Lightweight target list for the "same as" dropdown.
  const { data: aliasTargets } = useAliasOptions(key)
  // The filtered list being browsed, for prev/next.
  const { data: siblings } = useMappingList(browseStatus, { pageSize: 1000, q: browseQ })
  const alias = useSetAlias(key)
  const { label: retailerLabel } = useActiveRetailer()

  const [picks, setPicks] = useState({})
  const [eachToGrams, setEachToGrams] = useState('')
  const [needsSub, setNeedsSub] = useState(false)
  const [pantryStaple, setPantryStaple] = useState(false)
  const [notes, setNotes] = useState('')
  const [term, setTerm] = useState('')
  const [candidateOrder, setCandidateOrder] = useState([])
  const [draggedSku, setDraggedSku] = useState(null)
  const [dropTarget, setDropTarget] = useState(null)
  const [retailerTab, setRetailerTab] = useState(RETAILER_TAB)
  const [sourcingManually, setSourcingManually] = useState(false)
  const resolveManual = useResolveWithManualProduct(key)

  // Seed local editing state once the detail loads.
  useEffect(() => {
    if (!data) return
    const initial = {}
    for (const c of data.candidates) {
      initial[c.sku] = {
        accepted: c.accepted,
        match_type: c.match_type ?? (c.is_frozen ? 'form_differs' : 'exact'),
        reason: c.reason ?? '',
      }
    }
    setPicks(initial)
    setEachToGrams(data.each_to_grams ?? '')
    setNeedsSub(data.needs_substitution)
    setPantryStaple(data.pantry_staple)
    setNotes(data.reviewer_notes ?? '')
    setTerm(data.search_term ?? data.name ?? '')
    setCandidateOrder(data.candidates.map((candidate) => candidate.sku))
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
  const liveNav = useMemo(() => {
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

  // Aliasing an ingredient drops it from the (filtered) browsed list, so its
  // position vanishes and both arrows would go dead. Remember the neighbours it
  // had while it was still in the list so review can keep moving through it.
  const lastNav = useRef(liveNav)
  useEffect(() => {
    if (liveNav.idx >= 0) lastNav.current = liveNav
  }, [liveNav])
  const nav = liveNav.idx >= 0 ? liveNav : lastNav.current

  const aliasOptions = useMemo(
    () =>
      (aliasTargets?.items ?? [])
        .map((i) => ({ value: i.ingredient_key, label: i.name }))
        .sort((a, b) => a.label.localeCompare(b.label)),
    [aliasTargets],
  )

  const comparisonScores = useMemo(() => {
    const candidates = data?.candidates ?? []
    const options = {
      keyOf: (candidate) => candidate.sku,
      selectedOf: (candidate) => Boolean(picks[candidate.sku]?.accepted),
    }
    const rating = new Map()
    for (const candidate of candidates) {
      if (!options.selectedOf(candidate)) continue
      const score = ratingQualityScore(candidate.avg_rating, candidate.ratings_count)
      if (score != null) rating.set(candidate.sku, score)
    }
    return {
      unitPrice: relativeQualityScores(candidates, {
        ...options,
        valueOf: (candidate) => sortUnitPrice(candidate),
        groupOf: (candidate) => canonicalUnitPriceBasis(candidate.unit_price_basis),
        higherIsBetter: false,
      }),
      rating,
    }
  }, [data, picks])

  if (isLoading) {
    return (
      <Stack gap="xl">
        <PageHeader
          backLink={{ to: `/mapping${location.search}`, label: 'Back to mappings' }}
          title="Ingredient mapping"
        />
        <Group justify="center" py="xl">
          <Loader />
        </Group>
      </Stack>
    )
  }
  if (isError || !data) {
    return (
      <Stack gap="xl">
        <PageHeader
          backLink={{ to: `/mapping${location.search}`, label: 'Back to mappings' }}
          title="Ingredient mapping"
        />
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
    setPicks((prev) => ({ ...prev, [sku]: { ...prev[sku], accepted: checked } }))
  }

  function update(sku, field, value) {
    setPicks((prev) => ({ ...prev, [sku]: { ...prev[sku], [field]: value } }))
  }

  function submit(status) {
    const orderedSkus = candidateOrder.length
      ? candidateOrder
      : data.candidates.map((candidate) => candidate.sku)
    const accepted = orderedSkus
      .filter((sku) => picks[sku]?.accepted)
      .map((sku, index) => ({
        sku,
        rank: index + 1,
        match_type: picks[sku].match_type,
        reason: picks[sku].reason,
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

  // Candidates split by who sells them. Only the *display* is filtered — picks
  // are keyed by sku and submit() walks the full list, so accepting a manual
  // product and then switching back to the shop never silently drops it.
  const retailerCandidates = data.candidates.filter((c) => c.retailer !== 'manual')
  const manualCandidates = data.candidates.filter((c) => c.retailer === 'manual')
  const candidatesBySku = new Map(data.candidates.map((candidate) => [candidate.sku, candidate]))
  const orderedCandidates = candidateOrder
    .map((sku) => candidatesBySku.get(sku))
    .filter(Boolean)
  const visibleCandidates = orderedCandidates.filter((candidate) =>
    retailerTab === 'manual' ? candidate.retailer === 'manual' : candidate.retailer !== 'manual',
  )

  function reorderCandidate(sourceSku, targetSku, position) {
    if (!sourceSku || sourceSku === targetSku) return
    setCandidateOrder((current) => {
      const next = current.filter((sku) => sku !== sourceSku)
      const targetIndex = next.indexOf(targetSku)
      if (targetIndex === -1) return current
      next.splice(targetIndex + (position === 'after' ? 1 : 0), 0, sourceSku)
      return next
    })
  }

  function beginCandidateDrag(event, sourceSku) {
    if (event.button !== 0) return

    const pointerId = event.pointerId
    const sourceRow = event.currentTarget
    const start = { x: event.clientX, y: event.clientY }
    const previousUserSelect = document.body.style.userSelect
    let active = false
    let target = null

    function clearDrag() {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', handlePointerUp)
      window.removeEventListener('pointercancel', handlePointerCancel)
      if (sourceRow.hasPointerCapture(pointerId)) sourceRow.releasePointerCapture(pointerId)
      document.body.style.userSelect = previousUserSelect
      setDraggedSku(null)
      setDropTarget(null)
    }

    function handlePointerMove(pointerEvent) {
      if (pointerEvent.pointerId !== pointerId) return
      if (!active) {
        const distance = Math.hypot(
          pointerEvent.clientX - start.x,
          pointerEvent.clientY - start.y,
        )
        if (distance < 6) return
        active = true
        sourceRow.setPointerCapture(pointerId)
        document.body.style.userSelect = 'none'
        setDraggedSku(sourceSku)
      }

      pointerEvent.preventDefault()
      const targetRow = document
        .elementFromPoint(pointerEvent.clientX, pointerEvent.clientY)
        ?.closest('[data-candidate-sku]')
      const targetSku = targetRow?.dataset.candidateSku
      if (!targetSku || targetSku === sourceSku) {
        target = null
        setDropTarget(null)
        return
      }

      const bounds = targetRow.getBoundingClientRect()
      target = {
        sku: targetSku,
        position: pointerEvent.clientY >= bounds.top + bounds.height / 2 ? 'after' : 'before',
      }
      setDropTarget(target)
    }

    function handlePointerUp(pointerEvent) {
      if (pointerEvent.pointerId !== pointerId) return
      if (active && target) reorderCandidate(sourceSku, target.sku, target.position)
      if (active) {
        // A drag that starts over a checkbox, link or select must not also
        // activate that control when the pointer is released.
        const suppressClick = (clickEvent) => {
          clickEvent.preventDefault()
          clickEvent.stopPropagation()
        }
        window.addEventListener('click', suppressClick, { capture: true, once: true })
        window.setTimeout(() => window.removeEventListener('click', suppressClick, true), 0)
      }
      clearDrag()
    }

    function handlePointerCancel(pointerEvent) {
      if (pointerEvent.pointerId === pointerId) clearDrag()
    }

    window.addEventListener('pointermove', handlePointerMove, { passive: false })
    window.addEventListener('pointerup', handlePointerUp)
    window.addEventListener('pointercancel', handlePointerCancel)
  }

  // Rendered twice — under the header and at the foot of the candidate table —
  // so a quick approve never needs a scroll past the whole list.
  function actionButtons({ header = false } = {}) {
    return (
      <Group
        className={`${classes.decisionActions} ${header ? classes.headerDecisionActions : ''}`}
        wrap="nowrap"
      >
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
    <Stack gap="xl">
      <Paper withBorder radius="md" p={{ base: 'md', sm: 'lg' }} className={classes.summaryPanel}>
        <PageHeader
          className={classes.summaryHeader}
          backLink={{
            to: `/mapping${location.search}`,
            label: `Back to mappings${browseStatus ? ` (${browseStatus.replace('_', ' ')})` : ''}`,
          }}
          title={data.name}
          icon={data.ingredient_icon_url ? (
            <Image src={data.ingredient_icon_url} alt="" fit="contain" h="72%" w="72%" />
          ) : (
            <Text fw={800} size="lg">{data.name.slice(0, 1).toUpperCase()}</Text>
          )}
          badge={(
            <Group gap="xs" wrap="wrap">
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
            </Group>
          )}
          description={(
            <Stack gap={2}>
              <span>
                Used in {data.line_count.toLocaleString('en-GB')} recipe lines
                {u.median != null && ` - typically ${u.median}${u.metric_unit ?? 'g'}`}
                {u.p25 != null && u.p75 != null && ` (${u.p25}-${u.p75})`}
              </span>
              {u.common_native_amounts && (
                <span>Common amounts: {u.common_native_amounts}</span>
              )}
            </Stack>
          )}
          actions={(
            <Stack gap="sm" align="flex-end">
              {/* Flick through the list being browsed without going back to it. */}
              <Group gap="xs" wrap="nowrap">
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
              {!isAlias && actionButtons({ header: true })}
            </Stack>
          )}
        />

        <div className={classes.reviewGrid}>
          <div className={classes.reviewCell}>
            <Select
              label="Same as another ingredient"
              description="Link near-duplicates so they share one mapping."
              placeholder="Not an alias"
              data={aliasOptions}
              value={data.alias_of}
              onChange={(v) => alias.mutate(v)}
              searchable
              clearable
              disabled={alias.isPending}
            />
            {isAlias && (
              <Button
                variant="default"
                onClick={() => alias.mutate(null)}
                loading={alias.isPending}
                mt="xs"
              >
                Remove alias
              </Button>
            )}
            {alias.isError && (
              <Text size="xs" c="red" mt="xs">
                {alias.error?.message}
              </Text>
            )}
          </div>
          <NumberInput
            label="Grams per unit"
            description="Blank when sold by weight."
            value={eachToGrams}
            onChange={setEachToGrams}
            min={0}
            disabled={isAlias}
          />
          <Checkbox
            className={classes.optionCheckbox}
            label="Needs substitution"
            checked={needsSub}
            disabled={isAlias}
            onChange={(e) => setNeedsSub(e.currentTarget.checked)}
          />
          <Checkbox
            className={classes.optionCheckbox}
            label="Pantry staple"
            description="Left out of the shopping basket."
            checked={pantryStaple}
            disabled={isAlias}
            onChange={(e) => setPantryStaple(e.currentTarget.checked)}
          />
        </div>
      </Paper>

      {save.isError && (
        <Alert color="red" variant="light" title="Couldn't save decision">
          {save.error?.message}
        </Alert>
      )}

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

      {data.example_recipes?.length > 0 && (
        <Accordion variant="contained" radius="md" className={classes.exampleRecipes}>
          <Accordion.Item value="example-recipes">
            <Accordion.Control>
              <Group justify="space-between" align="baseline" wrap="nowrap" pr="xs">
                <Text fw={650}>Example recipes</Text>
                <Text size="sm" c="dimmed">
                  {data.example_recipes.length} examples
                </Text>
              </Group>
            </Accordion.Control>
            <Accordion.Panel>
              <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }} spacing="md">
                {data.example_recipes.map((recipe) => (
                  <RecipeCard key={recipe.id} recipe={recipe} showStats={false} />
                ))}
              </SimpleGrid>
            </Accordion.Panel>
          </Accordion.Item>
        </Accordion>
      )}

      <div className={classes.searchProposalRow}>
        <Paper
          withBorder
          radius="md"
          p={{ base: 'md', sm: 'lg' }}
          className={`${classes.mappingSurface} ${!data.llm_notes ? classes.searchOnly : ''}`}
        >
          <Group align="flex-end" gap="sm" wrap="nowrap" className={classes.searchControls}>
            <TextInput
              label={`${retailerLabel ?? 'Retailer'} search term`}
              description="Reword and search again when the candidates miss."
              value={term}
              disabled={isAlias}
              onChange={(e) => setTerm(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && term.trim()) research.mutate(term.trim())
              }}
              className={classes.searchInput}
            />
            <Button
              onClick={() => research.mutate(term.trim())}
              loading={research.isPending}
              disabled={isAlias || !term.trim()}
            >
              Search {retailerLabel ?? 'the shop'}
            </Button>
          </Group>
          {research.isPending && (
            <Text size="xs" c="dimmed" mt="xs">
              Searching {retailerLabel ?? 'the shop'} - this drives a real browser session, so it
              takes a few seconds.
            </Text>
          )}
          {research.isError && (
            <Text size="xs" c="red" mt="xs">
              Search failed: {research.error?.message}
            </Text>
          )}
        </Paper>
        {data.llm_notes && (
          <Alert
            color="blue"
            variant="light"
            title={`Proposal note${data.model ? ` (${data.model})` : ''}`}
            className={classes.proposalNote}
          >
            {data.llm_notes}
          </Alert>
        )}
      </div>

      <div
        style={
          isAlias
            ? { opacity: 0.45, pointerEvents: 'none', userSelect: 'none' }
            : undefined
        }
        aria-disabled={isAlias}
      >
        <Stack gap="lg">
          {data.candidates.length === 0 && (
        <Alert color="yellow" variant="light" title="No product candidates">
          {retailerLabel ?? 'The shop'} returned nothing for this ingredient's name — common for
          HelloFresh-specific wording
          ("21 Day Aged British Sirloin Steaks"). Reword the search above to find real products, or
          record what you buy instead below.
        </Alert>
      )}

      <Group justify="space-between" align="flex-end">
        <Stack gap={4}>
          <Tabs value={retailerTab} onChange={(v) => setRetailerTab(v ?? RETAILER_TAB)}>
            <Tabs.List>
              <Tabs.Tab value={RETAILER_TAB}>
                {retailerLabel ?? 'Retailer'} ({retailerCandidates.length})
              </Tabs.Tab>
              <Tabs.Tab value="manual">Manual ({manualCandidates.length})</Tabs.Tab>
            </Tabs.List>
          </Tabs>
          <Text size="xs" c="dimmed">
            Accepted products are ranked from top to bottom. Drag any row to change the order.
          </Text>
        </Stack>
        <Button
          variant={sourcingManually ? 'filled' : 'light'}
          size="xs"
          onClick={() => setSourcingManually((v) => !v)}
        >
          {sourcingManually ? 'Cancel' : 'Source this manually'}
        </Button>
      </Group>

      {sourcingManually && (
        <Paper withBorder radius="md" p={{ base: 'md', sm: 'lg' }}>
          <Text fw={600} mb={4}>
            Buy this somewhere else
          </Text>
          <Text size="sm" c="dimmed" mb="sm">
            Records the product, accepts it as this ingredient's first choice, and approves the
            mapping. It stays costed in the basket but is listed apart from the online order.
            Leaving the ingredient unmapped instead would price it at zero and bias the planner
            towards recipes that use it.
          </Text>
          <ManualProductForm
            initial={{ name: data.name }}
            submitLabel="Save and approve"
            pending={resolveManual.isPending}
            error={resolveManual.error?.message}
            onSubmit={(body) =>
              resolveManual.mutate(body, {
                onSuccess: () => {
                  setSourcingManually(false)
                  setRetailerTab('manual')
                },
              })
            }
          />
        </Paper>
      )}

      <Paper withBorder radius="md" className={classes.mappingSurface}>
        <Table.ScrollContainer minWidth={820}>
          <Table verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th w={70}>Accept</Table.Th>
                <Table.Th>Product</Table.Th>
                <Table.Th>Pack</Table.Th>
                <Table.Th className={classes.priceColumn}>Price</Table.Th>
                <Table.Th>Unit price</Table.Th>
                <Table.Th>Rating</Table.Th>
                <Table.Th w={140}>Match</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {visibleCandidates.map((c) => {
                const pick = picks[c.sku] ?? {}
                return (
                  <Table.Tr
                    key={c.sku}
                    data-candidate-sku={c.sku}
                    onPointerDown={(event) => beginCandidateDrag(event, c.sku)}
                    title="Drag to reorder"
                    className={[
                      classes.draggableCandidateRow,
                      pick.accepted ? classes.selectedCandidateRow : '',
                      draggedSku === c.sku ? classes.draggingCandidateRow : '',
                      dropTarget?.sku === c.sku
                        ? dropTarget.position === 'after'
                          ? classes.dropAfterCandidateRow
                          : classes.dropBeforeCandidateRow
                        : '',
                    ]
                      .filter(Boolean)
                      .join(' ')}
                  >
                    <Table.Td>
                      <Checkbox
                        checked={!!pick.accepted}
                        onChange={(e) => toggle(c.sku, e.currentTarget.checked)}
                      />
                    </Table.Td>
                    <Table.Td>
                      <Group gap={6} align="center" wrap="nowrap">
                        <Anchor href={c.url ?? '#'} target="_blank" fw={600} size="sm">
                          {c.name}
                        </Anchor>
                        {c.is_frozen && (
                          <span
                            className={classes.frozenIcon}
                            aria-label="Frozen"
                            title="Frozen"
                          >
                            <IconSnowflake size={16} stroke={2} aria-hidden="true" />
                          </span>
                        )}
                      </Group>
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
                    <Table.Td className={classes.priceColumn}>
                      <Stack gap={4} align="flex-start">
                        <Text size="sm">{money(c.base_price ?? c.price)}</Text>
                        {c.base_price != null && (
                          <Badge
                            className={classes.offerBadge}
                            size="xs"
                            variant="light"
                            color="grape"
                            tt="none"
                          >
                            {money(c.price)}
                          </Badge>
                        )}
                      </Stack>
                    </Table.Td>
                    <Table.Td>
                      <Stack gap={4} align="flex-start">
                        <MetricPill
                          metric="Unit price"
                          score={comparisonScores.unitPrice.get(c.sku)}
                          unavailable={sortUnitPrice(c) == null}
                        >
                          {sortUnitPrice(c) != null
                            ? `£${sortUnitPrice(c)}/${c.unit_price_basis}`
                            : '—'}
                        </MetricPill>
                        {/* Ranked on the shelf price, so a promotion has to be
                            stated rather than silently priced in — otherwise this
                            column looks wrong against the shop's own page. */}
                        {c.base_unit_price != null && (
                          <Badge
                            className={classes.offerBadge}
                            size="xs"
                            variant="light"
                            color="grape"
                            tt="none"
                          >
                            £{c.unit_price}/{c.unit_price_basis}
                          </Badge>
                        )}
                      </Stack>
                    </Table.Td>
                    <Table.Td>
                      <MetricPill
                        metric="Rating"
                        score={comparisonScores.rating.get(c.sku)}
                        description={ratingDescription(comparisonScores.rating.get(c.sku))}
                        unavailable={c.avg_rating == null}
                      >
                        {formatRating(c.avg_rating, c.ratings_count)}
                      </MetricPill>
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
              {visibleCandidates.length === 0 && (
                <Table.Tr>
                  <Table.Td colSpan={7}>
                    <Text size="sm" c="dimmed" ta="center" py="md">
                      {retailerTab === 'manual'
                        ? 'No hand-entered products for this ingredient yet.'
                        : `No ${retailerLabel ?? 'retailer'} candidates for this ingredient.`}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      </Paper>

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
