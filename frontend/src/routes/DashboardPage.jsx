import { Link } from 'react-router-dom'
import { Box, Button, Group, Progress, SimpleGrid, Stack, Text, Title } from '@mantine/core'
import { IconArrowRight, IconCalendarWeek } from '@tabler/icons-react'

import RecipeCard from '../components/RecipeCard.jsx'
import {
  formatWeekLabel,
  MAX_RECIPES_PER_WEEK,
  useWeeklyPlan,
} from '../hooks/useWeeklyPlan.js'
import classes from './DashboardPage.module.css'

const GRID_COLS = { base: 1, xs: 2, sm: 2, md: 3, lg: 4 }

export default function DashboardPage() {
  const {
    upcomingWeekStart,
    weekStarts,
    getWeekRecipes,
    removeRecipeFromWeek,
    setRecipePortions,
  } = useWeeklyPlan()

  return (
    <Stack gap="xl">
      <Group justify="space-between" align="flex-end">
        <div>
          <Title order={2}>Dashboard</Title>
          <Text c="dimmed">Plan recipes for each Monday-start week.</Text>
        </div>
        <Button component={Link} to="/" color="fresh" rightSection={<IconArrowRight size={16} />}>
          Browse recipes
        </Button>
      </Group>

      <Stack component="ul" gap="xl" className={classes.weekList}>
        {weekStarts.map((weekStart) => {
          const entries = getWeekRecipes(weekStart)
          const isUpcoming = weekStart === upcomingWeekStart
          const count = entries.length

          return (
            <Box component="li" key={weekStart} className={classes.weekItem}>
              <Group justify="space-between" align="flex-start" gap="md">
                <Group gap="sm" wrap="nowrap">
                  <IconCalendarWeek size={26} className={classes.weekIcon} />
                  <div>
                    <Group gap="xs">
                      <Title order={3} className={classes.weekTitle}>
                        {formatWeekLabel(weekStart)}
                      </Title>
                      {isUpcoming && (
                        <Text size="xs" fw={700} c="fresh.8" tt="uppercase">
                          Upcoming
                        </Text>
                      )}
                    </Group>
                    <Text c="dimmed" size="sm">
                      {count}/{MAX_RECIPES_PER_WEEK} recipes selected
                    </Text>
                  </div>
                </Group>
                <Progress
                  value={(count / MAX_RECIPES_PER_WEEK) * 100}
                  color="fresh"
                  radius="xl"
                  size="sm"
                  w={{ base: 96, xs: 140 }}
                  className={classes.progress}
                  aria-label={`${count} of ${MAX_RECIPES_PER_WEEK} recipes selected`}
                />
              </Group>

              {count === 0 ? (
                <Box className={classes.emptyState}>
                  <Text fw={600}>No recipes selected yet</Text>
                  <Text size="sm" c="dimmed">
                    Add recipes from Browse to build this week.
                  </Text>
                </Box>
              ) : (
                <SimpleGrid cols={GRID_COLS} spacing="lg">
                  {entries.map((entry) => (
                    <RecipeCard
                      key={entry.recipe.id}
                      recipe={entry.recipe}
                      plannerEntry={entry}
                      plannerControlsVisible
                      onRemoveFromPlan={() => removeRecipeFromWeek(weekStart, entry.recipe.id)}
                      onPortionsChange={(portions) =>
                        setRecipePortions(weekStart, entry.recipe.id, portions)
                      }
                    />
                  ))}
                </SimpleGrid>
              )}
            </Box>
          )
        })}
      </Stack>
    </Stack>
  )
}
