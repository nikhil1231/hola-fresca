import { Link } from 'react-router-dom'
import { Paper, SimpleGrid, Skeleton, Stack, Text, Title } from '@mantine/core'

import { useFacets } from '../hooks/useRecipeQueries.js'
import classes from './CuisinesPage.module.css'

export default function CuisinesPage() {
  const { data: facets, isLoading } = useFacets()

  return (
    <Stack gap="lg">
      <div>
        <Title order={2}>Browse by cuisine</Title>
        <Text c="dimmed">Pick a cuisine to explore its recipes.</Text>
      </div>

      {isLoading ? (
        <SimpleGrid cols={{ base: 2, sm: 3, md: 4 }} spacing="md">
          {Array.from({ length: 12 }).map((_, i) => (
            <Skeleton key={i} height={110} radius="md" />
          ))}
        </SimpleGrid>
      ) : (
        <SimpleGrid cols={{ base: 2, sm: 3, md: 4 }} spacing="md">
          {facets.cuisines.map((cuisine) => (
            <Paper
              key={cuisine.value}
              component={Link}
              to={`/?cuisine=${encodeURIComponent(cuisine.value)}`}
              radius="md"
              className={classes.tile}
              p="lg"
            >
              <Text fw={700} fz="lg" className={classes.name}>
                {cuisine.label}
              </Text>
              <Text size="sm" className={classes.count}>
                {cuisine.count} recipes
              </Text>
            </Paper>
          ))}
        </SimpleGrid>
      )}
    </Stack>
  )
}
