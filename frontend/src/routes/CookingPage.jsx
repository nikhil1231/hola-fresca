import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Image,
  Loader,
  Popover,
  Skeleton,
  Text,
  Title,
} from '@mantine/core'
import {
  IconAlertTriangle,
  IconArrowLeft,
  IconArrowRight,
  IconBinaryTree,
  IconCheck,
  IconListNumbers,
  IconX,
} from '@tabler/icons-react'

import { useCookMap, useProteinPreview, useRecipe } from '../hooks/useRecipeQueries.js'
import { usePreloadStepImages } from '../hooks/usePreloadStepImages.js'
import { DEFAULT_PORTIONS, formatProteinModifier, useWeeklyPlan } from '../hooks/useWeeklyPlan.js'
import { buildIngredientMatcher, splitStepText } from '../utils/ingredientMentions.js'
import { hasDisplayQuantity, scaledQuantity } from '../utils/ingredientQuantity.js'
import { RECIPE_PLACEHOLDER_IMAGE } from '../constants/images.js'
import CookMapView from './CookMapView.jsx'
import classes from './CookingPage.module.css'

// Between the word and the tooltip, and between the tooltip and the edges of
// the step it sits in.
const TOOLTIP_GAP = 8
const TOOLTIP_EDGE = 10
// Room the tooltip needs above the word; under this it goes below it instead.
const TOOLTIP_HEADROOM = 52
// Long enough to read as a fade, short enough never to delay the next tap.
const TOOLTIP_EXIT_MS = 110

// The amount, floating over the word that was tapped. Its own element rather
// than a tooltip library's, because of where it has to point: a mention split
// over a line break has a bounding box spanning both lines, and anything
// centred on that hangs in the margin beside the text.
function MentionTooltip({ anchor, closing }) {
  const ref = useRef(null)
  const [placement, setPlacement] = useState({ x: anchor.x, arrow: 0 })

  useLayoutEffect(() => {
    const width = ref.current?.offsetWidth ?? 0
    const rightmost = Math.max(TOOLTIP_EDGE + width / 2, anchor.width - TOOLTIP_EDGE - width / 2)
    // Nudged back inside the step when the word sits near an edge, with the
    // arrow left behind on the word so it still says which one it is about.
    const x = Math.min(Math.max(anchor.x, TOOLTIP_EDGE + width / 2), rightmost)
    const arrow = Math.min(Math.max(anchor.x - x, 12 - width / 2), width / 2 - 12)
    setPlacement({ x, arrow })
  }, [anchor])

  return (
    <span
      ref={ref}
      role="tooltip"
      className={classes.tooltip}
      data-below={anchor.below || undefined}
      data-closing={closing || undefined}
      style={{
        left: `${placement.x}px`,
        top: `${anchor.below ? anchor.bottom + TOOLTIP_GAP : anchor.top - TOOLTIP_GAP}px`,
      }}
    >
      {anchor.label}
      <span className={classes.tooltipArrow} style={{ marginLeft: `${placement.arrow}px` }} />
    </span>
  )
}

// One step: its photo and its method, scrolling together. The ingredients the
// step names are picked out, and tapping one shows what the box holds - which
// is the whole point: reading "add the rice" and then hunting for how much rice
// is how you end up scrolling with wet hands.
function StepPane({ index, step, recipeName, matcher, current, mention, closing, onToggle }) {
  const paneRef = useRef(null)
  const instructionsRef = useRef(null)
  const text = step.text || 'No instructions provided.'
  const segments = useMemo(() => splitStepText(text, matcher), [matcher, text])

  const toggle = (event, segment) => {
    // The first line box, not the union of them, so a name broken across two
    // lines is labelled on the half it starts on.
    const rect = event.currentTarget.getClientRects()[0] ?? event.currentTarget.getBoundingClientRect()
    const box = instructionsRef.current.getBoundingClientRect()
    const pane = paneRef.current.getBoundingClientRect()
    onToggle(index, segment.key, {
      x: rect.left - box.left + rect.width / 2,
      top: rect.top - box.top,
      bottom: rect.bottom - box.top,
      width: box.width,
      below: rect.top - pane.top < TOOLTIP_HEADROOM,
      label: segment.ingredient.label,
    })
  }

  return (
    <section
      ref={paneRef}
      className={classes.pane}
      aria-hidden={current ? undefined : true}
      inert={current ? undefined : true}
    >
      <Image
        src={step.image_url || RECIPE_PLACEHOLDER_IMAGE}
        fallbackSrc={RECIPE_PLACEHOLDER_IMAGE}
        alt={`Step ${step.index}: ${recipeName}`}
        className={classes.stepImage}
      />
      <article className={classes.instructions} ref={instructionsRef}>
        <Text size="sm" c="fresh.7" fw={700} tt="uppercase" className={classes.stepLabel}>
          Step {step.index}
        </Text>
        <Text className={classes.stepText}>
          {segments.map((segment) =>
            segment.ingredient ? (
              <span
                key={segment.key}
                className={classes.ingredientMention}
                data-ingredient-mention="true"
                role="button"
                tabIndex={0}
                aria-label={`${segment.text}: ${segment.ingredient.label}`}
                onClick={(event) => toggle(event, segment)}
                onKeyDown={(event) => {
                  if (event.key !== 'Enter' && event.key !== ' ') return
                  event.preventDefault()
                  toggle(event, segment)
                }}
              >
                {segment.text}
              </span>
            ) : (
              <span key={segment.key}>{segment.text}</span>
            ),
          )}
        </Text>
        {mention && <MentionTooltip key={mention.key} anchor={mention.anchor} closing={closing} />}
      </article>
    </section>
  )
}

export default function CookingPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { data: recipe, isLoading, isError } = useRecipe(id)
  // Cook the dish you planned, not the one the library publishes. A week's
  // protein swap rewrites the weights in the steps as well as the ingredient
  // list, so following the stored method with a bag of tofu in front of you is
  // how you end up cooking to the chicken's timings and quantities.
  const { getCookingEntry } = useWeeklyPlan()
  const entry = getCookingEntry(Number(id), searchParams.get('week'))
  const { data: proteinPreview } = useProteinPreview(id, entry?.protein, {
    enabled: Boolean(entry?.protein),
  })
  const cookMap = useCookMap(id, entry?.protein)
  const modified = proteinPreview?.changed ? proteinPreview : null
  const modifierLabel = formatProteinModifier(entry?.protein)
  const [stepPosition, setStepPosition] = useState(0)
  const [viewMode, setViewMode] = useState('steps')
  const [mapFailureOpen, setMapFailureOpen] = useState(false)
  usePreloadStepImages(modified?.steps ?? recipe?.steps)
  // At most one amount is on screen at a time, so the open one lives here
  // rather than inside the step it belongs to.
  const [mention, setMention] = useState(null)
  const [closing, setClosing] = useState(false)
  const closeTimer = useRef(null)
  const trackRef = useRef(null)

  // The amounts are the ones you shopped for: the planned portions against the
  // recipe's own yield, matching what the recipe page's ingredient table shows.
  const ingredients = modified?.ingredients ?? recipe?.ingredients
  const factor = (entry?.portions ?? DEFAULT_PORTIONS) / (recipe?.base_yield || 2)
  const matcher = useMemo(() => {
    if (!ingredients?.length) return null
    // Only what there is a number to show for - a green word that opens an
    // empty tooltip is worse than no green word.
    const withAmounts = []
    for (const ing of ingredients) {
      if (!hasDisplayQuantity(ing)) continue
      const quantity = scaledQuantity(ing, factor)
      if (!quantity) continue
      // A bare count is just a loose number with the name sitting in front of
      // it. The ingredient table has a Qty column to say what it is; here the
      // sign has to.
      const amount = /^[\d¼½¾.]+$/.test(quantity) ? `×${quantity}` : quantity
      withAmounts.push({ ...ing, label: `${ing.name} · ${amount}` })
    }
    return buildIngredientMatcher(withAmounts)
  }, [factor, ingredients])

  const closeMention = useCallback(() => {
    clearTimeout(closeTimer.current)
    setClosing(true)
    closeTimer.current = setTimeout(() => {
      setMention(null)
      setClosing(false)
    }, TOOLTIP_EXIT_MS)
  }, [])

  const toggleMention = useCallback(
    (pane, key, anchor) => {
      if (mention && !closing && mention.pane === pane && mention.key === key) {
        closeMention()
        return
      }
      clearTimeout(closeTimer.current)
      setClosing(false)
      setMention({ pane, key, anchor })
    },
    [closing, closeMention, mention],
  )

  useEffect(() => () => clearTimeout(closeTimer.current), [])

  useEffect(() => {
    if (viewMode === 'map' && cookMap.status !== 'ready') setViewMode('steps')
  }, [cookMap.status, viewMode])

  // Tapping anywhere else puts the amount away. On pointerdown, so it goes on
  // the same touch that starts a swipe or reaches for the next-step button, and
  // ignoring the mentions themselves so tapping one straight after another
  // swaps the tooltip over instead of closing and reopening.
  useEffect(() => {
    if (!mention || closing) return undefined
    const close = (event) => {
      if (event.target instanceof Element && event.target.closest('[data-ingredient-mention]')) return
      closeMention()
    }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [closeMention, closing, mention])

  const steps = modified?.steps ?? recipe?.steps ?? []
  // Clamped, because the step list can change underneath you: the modified
  // method arrives a moment after the stored one, and being three steps into a
  // recipe that just got shorter should move you back, not blank the page.
  const position = steps.length ? Math.min(stepPosition, steps.length - 1) : 0

  // The steps sit side by side in one horizontal scroller, so a swipe drags the
  // next one along under your finger and settles wherever you let it go. Which
  // step you are on is then a fact about the scroll offset rather than
  // something this component has to decide from a gesture.
  const onScroll = (event) => {
    const track = event.currentTarget
    if (!track.clientWidth) return
    const next = Math.round(track.scrollLeft / track.clientWidth)
    if (next === position) return
    setStepPosition(next)
    if (mention) closeMention()
  }

  const goToStep = (next) => {
    const track = trackRef.current
    track?.scrollTo({ left: next * track.clientWidth, behavior: 'smooth' })
  }

  // Keep the offset honest when the width changes underneath it - a rotation,
  // or the step list changing length while you are part-way along it. Deliberately
  // not run on every step change: the browser is already snapping, and putting the
  // scroller back where we think it belongs mid-swipe drags it out from under the
  // finger that is moving it.
  const positionRef = useRef(position)
  positionRef.current = position
  useLayoutEffect(() => {
    const track = trackRef.current
    if (!track) return undefined
    const align = () => track.scrollTo({ left: positionRef.current * track.clientWidth })
    const observer = new ResizeObserver(align)
    observer.observe(track)
    return () => observer.disconnect()
  }, [steps.length])

  if (isLoading) {
    return (
      <main className={classes.page} aria-busy="true">
        <Skeleton height={56} />
        <Skeleton className={classes.loadingImage} />
        <Skeleton height={180} />
      </main>
    )
  }

  if (isError || !recipe) {
    return (
      <main className={classes.errorPage}>
        <Alert color="red" title="Recipe not found">
          We couldn't find that recipe.
        </Alert>
        <Button component={Link} to="/" color="fresh" w="fit-content">
          Back to recipes
        </Button>
      </main>
    )
  }

  if (steps.length === 0) {
    return (
      <main className={classes.errorPage}>
        <Alert color="yellow" title="No cooking steps yet">
          This recipe doesn't have any instructions to follow.
        </Alert>
        <Button component={Link} to={`/recipes/${recipe.id}`} color="fresh" w="fit-content">
          Back to recipe
        </Button>
      </main>
    )
  }

  const isFirst = position === 0
  const isLast = position === steps.length - 1
  // Back where you came from, week and all — the detail page reads the week to
  // decide which plan entry it is describing.
  const weekParam = searchParams.get('week')
  const detailHref = `/recipes/${recipe.id}${weekParam ? `?week=${weekParam}` : ''}`

  const finish = () => navigate(detailHref, { replace: true })

  return (
    <main className={classes.page}>
      <header className={classes.header}>
        <div className={classes.recipeName}>
          <Text size="xs" c="dimmed" fw={700} tt="uppercase">
            Cooking now
          </Text>
          <Title order={1} className={classes.title} lineClamp={1}>
            {recipe.name}
          </Title>
          {/* What you planned, said before the first step rather than left for
              you to notice halfway through. The steps below are already written
              for it; this is why they do not match the recipe you remember. */}
          {(modifierLabel || entry) && (
            <Group gap={6} mt={4} wrap="nowrap">
              {modifierLabel && (
                <Badge color="grape" variant="light" size="sm" radius="sm">
                  {modifierLabel}
                </Badge>
              )}
              {entry && (
                <Badge color="gray" variant="light" size="sm" radius="sm">
                  {entry.portions} portions
                </Badge>
              )}
            </Group>
          )}
        </div>
        <Group gap={4} wrap="nowrap">
          {(cookMap.status === 'processing' || cookMap.retrying) && (
            <div
              className={classes.mapStatus}
              role="status"
              aria-label="Preparing cook map"
              title="Preparing cook map"
            >
              <Loader size={20} color="fresh" />
            </div>
          )}
          {cookMap.status === 'ready' && cookMap.data?.graph && (
            <ActionIcon
              variant={viewMode === 'map' ? 'filled' : 'subtle'}
              color="fresh"
              size="lg"
              radius="xl"
              aria-label={viewMode === 'map' ? 'Show recipe steps' : 'Show cook map'}
              title={viewMode === 'map' ? 'Show recipe steps' : 'Show cook map'}
              onClick={() => setViewMode((current) => (current === 'map' ? 'steps' : 'map'))}
            >
              {viewMode === 'map' ? <IconListNumbers size={21} /> : <IconBinaryTree size={21} />}
            </ActionIcon>
          )}
          {cookMap.status === 'failed' && !cookMap.retrying && (
            <Popover
              opened={mapFailureOpen}
              onChange={setMapFailureOpen}
              position="bottom-end"
              width={300}
              shadow="md"
              withinPortal
            >
              <Popover.Target>
                <ActionIcon
                  variant="light"
                  color="orange"
                  size="lg"
                  radius="xl"
                  aria-label="Cook map unavailable. Show details and retry"
                  aria-expanded={mapFailureOpen}
                  title="Cook map unavailable · show details"
                  onClick={() => setMapFailureOpen((open) => !open)}
                >
                  <IconAlertTriangle size={20} />
                </ActionIcon>
              </Popover.Target>
              <Popover.Dropdown role="alert" className={classes.mapFailure}>
                <Text fw={700} size="sm">Cook map unavailable</Text>
                <Text size="sm" c="dimmed">
                  {cookMap.error?.includes('OPENAI_API_KEY')
                    ? 'Map generation is not configured. Add OPENAI_API_KEY to the repo-root .env, then restart the backend.'
                    : cookMap.error || 'The map could not be generated. Linear cooking is still available.'}
                </Text>
                <Button
                  color="orange"
                  variant="light"
                  size="xs"
                  loading={cookMap.retrying}
                  onClick={() => cookMap.retry()}
                >
                  Retry map generation
                </Button>
              </Popover.Dropdown>
            </Popover>
          )}
          <ActionIcon
            component={Link}
            to={detailHref}
            replace
            variant="subtle"
            color="gray"
            size="lg"
            radius="xl"
            aria-label="Exit cooking mode"
          >
            <IconX size={21} />
          </ActionIcon>
        </Group>
      </header>

      {viewMode === 'map' && cookMap.data?.graph ? (
        <CookMapView
          graph={{ ...cookMap.data.graph, source_fingerprint: cookMap.data.source_fingerprint }}
          recipe={recipe}
          ingredients={ingredients ?? []}
          servings={entry?.portions ?? recipe.base_yield ?? 2}
          modifierKey={JSON.stringify(entry?.protein ?? {})}
        />
      ) : (
        <>
          {/* The photo of a step is part of the step, so it scrolls with the method
              rather than holding on to height the instructions need. Only the title
              bar and the step controls stay put. */}
          <div className={classes.track} ref={trackRef} onScroll={onScroll}>
            {steps.map((step, index) => (
              <StepPane
                key={step.index ?? index}
                index={index}
                step={step}
                recipeName={recipe.name}
                matcher={matcher}
                current={index === position}
                mention={mention?.pane === index ? mention : null}
                closing={closing}
                onToggle={toggleMention}
              />
            ))}
          </div>

          <nav className={classes.navigation} aria-label="Cooking steps">
            <ActionIcon
              variant="light"
              color="fresh"
              size="xl"
              radius="xl"
              disabled={isFirst}
              aria-label="Previous step"
              onClick={() => goToStep(position - 1)}
            >
              <IconArrowLeft size={22} />
            </ActionIcon>
            <Text fw={700} size="sm" ta="center" aria-live="polite">
              Step {position + 1} of {steps.length}
            </Text>
            <ActionIcon
              variant="filled"
              color="fresh"
              size="xl"
              radius="xl"
              aria-label={isLast ? 'Finish cooking' : 'Next step'}
              onClick={isLast ? finish : () => goToStep(position + 1)}
            >
              {isLast ? <IconCheck size={22} /> : <IconArrowRight size={22} />}
            </ActionIcon>
          </nav>
        </>
      )}
    </main>
  )
}
