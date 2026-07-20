import { useEffect } from 'react'
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
} from '@mantine/core'
import { useDisclosure, useIntersection } from '@mantine/hooks'
import { IconAdjustmentsHorizontal, IconMoodEmpty } from '@tabler/icons-react'

import FilterPanel from '../components/FilterPanel.jsx'
import RecipeCard from '../components/RecipeCard.jsx'
import { useFilters, countActiveFilters } from '../hooks/useFilters.js'
import { useFacets, useRecipes } from '../hooks/useRecipeQueries.js'

const GRID_COLS = { base: 1, xs: 2, sm: 2, md: 3, lg: 4 }

export default function BrowsePage() {
  const { filters, setScalar, setArray, toggleArrayValue, clearAll } = useFilters()
  const { data: facets } = useFacets()
  const {
    data,
    isLoading,
    isError,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useRecipes(filters)
  const [drawerOpen, drawer] = useDisclosure(false)

  const { ref: sentinelRef, entry } = useIntersection({ threshold: 0, rootMargin: '400px' })
  useEffect(() => {
    if (entry?.isIntersecting && hasNextPage && !isFetchingNextPage) fetchNextPage()
  }, [entry?.isIntersecting, hasNextPage, isFetchingNextPage, fetchNextPage])

  const recipes = data?.pages.flatMap((p) => p.items) ?? []
  const total = data?.pages[0]?.total ?? 0
  const activeCount = countActiveFilters(filters)

  const panel = (
    <FilterPanel
      facets={facets}
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
          <Select
            value={filters.sort ?? 'popular'}
            onChange={(v) => setScalar('sort', v)}
            data={(facets?.sorts ?? []).map((s) => ({ value: s.value, label: s.label }))}
            allowDeselect={false}
            radius="md"
            size="sm"
            w={180}
            aria-label="Sort recipes"
          />
        </Group>

        {isError ? (
          <Alert color="red" title="Couldn't load recipes">
            Please check the backend is running and try again.
          </Alert>
        ) : isLoading ? (
          <SimpleGrid cols={GRID_COLS} spacing="lg">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} height={280} radius="md" />
            ))}
          </SimpleGrid>
        ) : recipes.length === 0 ? (
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
              {recipes.map((recipe) => (
                <RecipeCard key={recipe.id} recipe={recipe} />
              ))}
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
