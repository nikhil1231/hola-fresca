import { useEffect, useMemo } from 'react'
import {
  Alert,
  Box,
  Button,
  Center,
  Drawer,
  Group,
  Loader,
  Select,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  Title,
} from '@mantine/core'
import { useDisclosure, useIntersection, useMediaQuery } from '@mantine/hooks'
import { IconAdjustmentsHorizontal, IconBasket, IconMoodEmpty, IconSparkles } from '@tabler/icons-react'

import FilterPanel from '../components/FilterPanel.jsx'
import { DEFAULT_FACETS } from '../data/defaultFacets.js'
import RecipeCard from '../components/RecipeCard.jsx'
import { useFilters, countActiveFilters } from '../hooks/useFilters.js'
import {
  useFacets,
  usePlannerBasket,
  useRecipes,
  useRecipeSuggestions,
} from '../hooks/useRecipeQueries.js'
import {
  DEFAULT_PORTIONS,
  MAX_RECIPES_PER_WEEK,
  toPlannerSelections,
  useWeeklyPlan,
} from '../hooks/useWeeklyPlan.js'

const GRID_COLS = { base: 1, xs: 2, sm: 2, md: 3, lg: 4 }
const PAGE_ROWS = 6

const money = new Intl.NumberFormat('en-GB', {
  style: 'currency',
  currency: 'GBP',
})

function formatMoney(value) {
  return money.format(value ?? 0)
}

function useBrowseRowSize() {
  const isLg = useMediaQuery('(min-width: 75em)')
  const isMd = useMediaQuery('(min-width: 62em)')
  const isXs = useMediaQuery('(min-width: 36em)')

  if (isLg) return GRID_COLS.lg
  if (isMd) return GRID_COLS.md
  if (isXs) return GRID_COLS.xs
  return GRID_COLS.base
}

function WeekRail({ entries, selections, onRemove, onPortionsChange }) {
  const { data, isLoading } = usePlannerBasket(selections)

  return (
    <Box
      visibleFrom="lg"
      w={260}
      p="md"
      style={{
        flexShrink: 0,
        position: 'sticky',
        top: 88,
        border: '1px solid var(--mantine-color-default-border)',
        borderRadius: 'var(--mantine-radius-md)',
        background: 'var(--mantine-color-body)',
      }}
    >
      <Group gap="xs" mb="sm">
        <IconBasket size={18} />
        <Title order={3} style={{ fontSize: '1rem', letterSpacing: 0 }}>
          Week
        </Title>
      </Group>
      <Text fw={800} size="xl">
        {isLoading ? '...' : formatMoney(data?.cost)}
      </Text>
      <Text size="xs" c="dimmed" mb="md">
        {entries.length} selected
        {data?.unmapped?.length || data?.unpriceable?.length || data?.untracked_lines
          ? `, ${(
              (data?.unmapped?.length ?? 0)
              + (data?.unpriceable?.length ?? 0)
              + (data?.untracked_lines ?? 0)
            ).toLocaleString()} unpriced`
          : ''}
      </Text>
      <Stack gap="xs">
        {entries.map((entry) => (
          <Box
            key={entry.recipe.id}
            p="xs"
            style={{
              border: '1px solid var(--mantine-color-default-border)',
              borderRadius: 'var(--mantine-radius-sm)',
            }}
          >
            <Text fw={700} size="sm" lineClamp={2}>
              {entry.recipe.name}
            </Text>
            <Group justify="space-between" mt={4}>
              <Text size="xs" c="dimmed">
                {entry.portions} portions
              </Text>
              <Group gap={4}>
                <Button
                  size="compact-xs"
                  variant="subtle"
                  color="gray"
                  disabled={entry.portions <= 1}
                  onClick={() => onPortionsChange(entry.recipe.id, entry.portions - 1)}
                >
                  -
                </Button>
                <Button
                  size="compact-xs"
                  variant="subtle"
                  color="fresh"
                  disabled={entry.portions >= 8}
                  onClick={() => onPortionsChange(entry.recipe.id, entry.portions + 1)}
                >
                  +
                </Button>
                <Button
                  size="compact-xs"
                  variant="subtle"
                  color="red"
                  onClick={() => onRemove(entry.recipe.id)}
                >
                  Remove
                </Button>
              </Group>
            </Group>
          </Box>
        ))}
        {entries.length === 0 && (
          <Text size="sm" c="dimmed">
            Add recipes to see a running basket.
          </Text>
        )}
      </Stack>
    </Box>
  )
}

export default function BrowsePage() {
  const { filters, setScalar, setArray, toggleArrayValue, clearAll } = useFilters()
  const rowSize = useBrowseRowSize()
  const {
    upcomingWeekStart,
    getWeekRecipes,
    getRecipeEntry,
    addRecipeToWeek,
    removeRecipeFromWeek,
    setRecipePortions,
  } = useWeeklyPlan()
  const { data: facets } = useFacets()
  const upcomingRecipes = getWeekRecipes(upcomingWeekStart)
  const upcomingWeekFull = upcomingRecipes.length >= MAX_RECIPES_PER_WEEK
  const selectedRecipeIds = useMemo(
    () => upcomingRecipes.map((entry) => entry.recipe.id),
    [upcomingRecipes],
  )
  const plannerSelections = useMemo(() => toPlannerSelections(upcomingRecipes), [upcomingRecipes])
  const basePageSize = rowSize * PAGE_ROWS
  const pinnedRemainder = upcomingRecipes.length % rowSize
  const firstPageSize = pinnedRemainder === 0 ? basePageSize : basePageSize - pinnedRemainder
  const pageSize = basePageSize
  const requestedSort = filters.sort ?? (upcomingRecipes.length > 0 ? 'best_fit' : 'popular')
  const bestFitRequested = requestedSort === 'best_fit'
  const bestFitActive = bestFitRequested && upcomingRecipes.length > 0
  const suggestionFilters = useMemo(() => {
    const { sort: _sort, ...rest } = filters
    return rest
  }, [filters])
  const recipesQuery = useRecipes(bestFitActive ? suggestionFilters : filters, {
    enabled: !bestFitActive,
    pageSize,
    firstPageSize,
    excludeIds: selectedRecipeIds,
  })
  const suggestionsQuery = useRecipeSuggestions(suggestionFilters, plannerSelections, {
    candidatePortions: DEFAULT_PORTIONS,
    enabled: bestFitActive,
    pageSize,
    firstPageSize,
  })
  const activeQuery = bestFitActive ? suggestionsQuery : recipesQuery
  const {
    data,
    isLoading,
    isError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = activeQuery
  const [drawerOpen, drawer] = useDisclosure(false)

  const { ref: sentinelRef, entry } = useIntersection({ threshold: 0, rootMargin: '400px' })
  useEffect(() => {
    if (entry?.isIntersecting && hasNextPage && !isFetchingNextPage) fetchNextPage()
  }, [entry?.isIntersecting, hasNextPage, isFetchingNextPage, fetchNextPage])

  const recipes = useMemo(() => data?.pages.flatMap((p) => p.items) ?? [], [data])
  const selectedIds = useMemo(() => new Set(selectedRecipeIds), [selectedRecipeIds])
  const displayRecipes = useMemo(
    () => [
      ...upcomingRecipes.map((entry) => entry.recipe),
      ...recipes.filter((recipe) => !selectedIds.has(recipe.id)),
    ],
    [recipes, selectedIds, upcomingRecipes],
  )
  const total = data?.pages[0]?.total ?? 0
  const activeCount = countActiveFilters(filters)
  const filterFacets = facets ?? DEFAULT_FACETS
  const sortOptions = filterFacets.sorts.map((s) => ({ value: s.value, label: s.label }))
  const sortValue = bestFitRequested ? 'popular' : requestedSort
  const loadingTiles = Array.from({ length: firstPageSize })

  function renderRecipeCard(recipe) {
    const plannerEntry = getRecipeEntry(recipe.id, upcomingWeekStart)
    return (
      <RecipeCard
        key={recipe.id}
        recipe={recipe}
        marginalScore={bestFitActive && !plannerEntry ? recipe.marginal_score : null}
        unpricedGapCount={bestFitActive && !plannerEntry ? recipe.unpriced_gap_count : 0}
        basketAvailable={bestFitActive && !plannerEntry ? recipe.basket_available : true}
        plannerEntry={plannerEntry}
        plannerDisabled={!plannerEntry && upcomingWeekFull}
        onAddToPlan={() => addRecipeToWeek(recipe, upcomingWeekStart)}
        onRemoveFromPlan={() => removeRecipeFromWeek(upcomingWeekStart, recipe.id)}
        onPortionsChange={(portions) =>
          setRecipePortions(upcomingWeekStart, recipe.id, portions)
        }
      />
    )
  }

  const panel = (
    <FilterPanel
      facets={filterFacets}
      filters={filters}
      setScalar={setScalar}
      setArray={setArray}
      toggleArrayValue={toggleArrayValue}
      clearAll={clearAll}
    />
  )

  return (
    <Group align="flex-start" gap="xl" wrap="nowrap">
      <Box visibleFrom="md" w={260} style={{ flexShrink: 0, position: 'sticky', top: 88 }}>
        {panel}
      </Box>

      <Stack gap="md" style={{ flex: 1, minWidth: 0 }}>
        <Group justify="space-between" wrap="nowrap">
          <Group gap="sm">
            <Button
              hiddenFrom="md"
              variant="default"
              size="sm"
              leftSection={<IconAdjustmentsHorizontal size={16} />}
              onClick={drawer.open}
            >
              Filters{activeCount > 0 ? ` (${activeCount})` : ''}
            </Button>
            <Text c="dimmed" size="sm">
              {isLoading ? 'Loading…' : `${total.toLocaleString()} recipes`}
            </Text>
          </Group>
          <Group gap="xs" wrap="nowrap">
            <Button
              variant={bestFitActive ? 'filled' : 'light'}
              color="fresh"
              size="sm"
              leftSection={<IconSparkles size={16} />}
              disabled={upcomingRecipes.length === 0}
              onClick={() => setScalar('sort', bestFitActive ? 'popular' : 'best_fit')}
            >
              Best fit
            </Button>
            <Select
              value={sortValue}
              onChange={(v) => setScalar('sort', v)}
              data={sortOptions}
              allowDeselect={false}
              radius="md"
              size="sm"
              w={{ base: 180, sm: 220 }}
              aria-label="Sort recipes"
            />
          </Group>
        </Group>

        {isError ? (
          <Alert color="red" title="Couldn't load recipes">
            Please check the backend is running and try again.
          </Alert>
        ) : isLoading ? (
          <SimpleGrid cols={GRID_COLS} spacing="lg">
            {upcomingRecipes.map((entry) => renderRecipeCard(entry.recipe))}
            {loadingTiles.map((_, i) => (
              <Skeleton key={i} height={280} radius="md" />
            ))}
          </SimpleGrid>
        ) : displayRecipes.length === 0 ? (
          <Center mih={280}>
            <Stack align="center" gap="xs">
              <IconMoodEmpty size={40} stroke={1.5} />
              <Text fw={600}>No recipes match these filters</Text>
              <Button variant="light" color="fresh" onClick={clearAll}>
                Clear filters
              </Button>
            </Stack>
          </Center>
        ) : (
          <>
            <SimpleGrid cols={GRID_COLS} spacing="lg">
              {displayRecipes.map((recipe) => renderRecipeCard(recipe))}
            </SimpleGrid>
            <Box ref={sentinelRef} h={1} />
            {isFetchingNextPage && (
              <Center py="md">
                <Loader color="fresh" />
              </Center>
            )}
          </>
        )}
      </Stack>

      <WeekRail
        entries={upcomingRecipes}
        selections={plannerSelections}
        onRemove={(recipeId) => removeRecipeFromWeek(upcomingWeekStart, recipeId)}
        onPortionsChange={(recipeId, portions) =>
          setRecipePortions(upcomingWeekStart, recipeId, portions)
        }
      />

      <Drawer
        opened={drawerOpen}
        onClose={drawer.close}
        title="Filters"
        size="85%"
        padding="md"
      >
        {panel}
      </Drawer>
    </Group>
  )
}
