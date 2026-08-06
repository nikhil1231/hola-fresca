import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ActionIcon, Alert, Badge, Button, Group, Image, Loader, Popover, Skeleton, Text, Title } from '@mantine/core'
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
import { formatProteinModifier, useWeeklyPlan } from '../hooks/useWeeklyPlan.js'
import { RECIPE_PLACEHOLDER_IMAGE } from '../constants/images.js'
import CookMapView from './CookMapView.jsx'
import classes from './CookingPage.module.css'

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

  useEffect(() => {
    if (viewMode === 'map' && cookMap.status !== 'ready') setViewMode('steps')
  }, [cookMap.status, viewMode])

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

  const steps = modified?.steps ?? recipe.steps ?? []
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

  // Clamped, because the step list can change underneath you: the modified
  // method arrives a moment after the stored one, and being three steps into a
  // recipe that just got shorter should move you back, not blank the page.
  const position = Math.min(stepPosition, steps.length - 1)
  const currentStep = steps[position]
  const isFirst = position === 0
  const isLast = position === steps.length - 1
  const image = currentStep.image_url || recipe.image_url || RECIPE_PLACEHOLDER_IMAGE
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
            <div className={classes.mapStatus} role="status" aria-label="Preparing cook map" title="Preparing cook map">
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
              onClick={() => setViewMode((current) => current === 'map' ? 'steps' : 'map')}
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
          ingredients={modified?.ingredients ?? recipe.ingredients ?? []}
          servings={entry?.portions ?? recipe.base_yield ?? 2}
          modifierKey={JSON.stringify(entry?.protein ?? {})}
        />
      ) : (
        <>
          <section className={classes.stepArea} aria-live="polite">
            <Image
              src={image}
              fallbackSrc={RECIPE_PLACEHOLDER_IMAGE}
              alt={`Step ${currentStep.index}: ${recipe.name}`}
              className={classes.stepImage}
            />
            <article className={classes.instructions} tabIndex="-1">
              <Text size="sm" c="fresh.7" fw={700} tt="uppercase" className={classes.stepLabel}>
                Step {currentStep.index}
              </Text>
              <Text className={classes.stepText}>{currentStep.text || 'No instructions provided.'}</Text>
            </article>
          </section>

          <nav className={classes.navigation} aria-label="Cooking steps">
            <ActionIcon
              variant="light"
              color="fresh"
              size="xl"
              radius="xl"
              disabled={isFirst}
              aria-label="Previous step"
              onClick={() => setStepPosition(position - 1)}
            >
              <IconArrowLeft size={22} />
            </ActionIcon>
            <Text fw={700} size="sm" ta="center">
              Step {position + 1} of {steps.length}
            </Text>
            <ActionIcon
              variant="filled"
              color="fresh"
              size="xl"
              radius="xl"
              aria-label={isLast ? 'Finish cooking' : 'Next step'}
              onClick={isLast ? finish : () => setStepPosition(position + 1)}
            >
              {isLast ? <IconCheck size={22} /> : <IconArrowRight size={22} />}
            </ActionIcon>
          </nav>
        </>
      )}
    </main>
  )
}
