import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Divider,
  Grid,
  Group,
  Image,
  Paper,
  SegmentedControl,
  SimpleGrid,
  Skeleton,
  Stack,
  Text,
  ThemeIcon,
  Title,
} from '@mantine/core'
import {
  IconArrowLeft,
  IconChefHat,
  IconClock,
  IconExternalLink,
  IconStarFilled,
  IconUsers,
} from '@tabler/icons-react'

import { useRecipe } from '../hooks/useRecipeQueries.js'
import classes from './RecipeDetailPage.module.css'

const DIFFICULTY = { 1: 'Easy', 2: 'Medium', 3: 'Hard' }
const SERVINGS = [0.5, 1, 2, 3, 4, 6, 8]
const SERVINGS_LABELS = { 0.5: '½', 1: '1', 2: '2', 3: '3', 4: '4', 6: '6', 8: '8' }
const METRIC_UNITS = ['grams', 'milliliter(s)', 'tbsp', 'tsp', 'pinch']

// Round a scaled quantity to a sensible precision for display.
function roundNice(v) {
  if (v >= 20) return Math.round(v / 5) * 5
  if (v >= 1) return Math.round(v)
  return Math.round(v * 4) / 4
}

function roundCount(v) {
  return Math.round(v * 4) / 4
}

// Format one ingredient at the chosen scale: grams primary, native count in
// parentheses when the source unit is a count/container (e.g. "375g (1.5 carton)").
function scaledQuantity(ing, factor) {
  const parts = []
  if (ing.amount_g != null) {
    parts.push(`${roundNice(ing.amount_g * factor)}${ing.canonical_unit || 'g'}`)
  }
  const nativeIsCount = ing.unit && !METRIC_UNITS.includes(ing.unit)
  if (ing.amount != null && nativeIsCount) {
    const n = roundCount(ing.amount * factor)
    const unit = ing.unit.replace(/\(s\)$/, n === 1 ? '' : 's')
    parts.push(parts.length ? `(${n} ${unit})` : `${n} ${unit}`)
  } else if (ing.amount_g == null && ing.amount != null) {
    parts.push(String(Math.round(ing.amount * factor * 100) / 100))
  }
  return parts.join(' ')
}

function MacroStat({ label, value, unit }) {
  return (
    <Paper withBorder radius="md" p="sm" className={classes.macro}>
      <Text fz="xl" fw={700}>
        {value == null ? '—' : `${Math.round(value)}`}
        <Text span fz="sm" c="dimmed" fw={500}>
          {unit}
        </Text>
      </Text>
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
    </Paper>
  )
}

export default function RecipeDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { data: recipe, isLoading, isError } = useRecipe(id)
  const [servingsOverride, setServingsOverride] = useState(null)

  if (isLoading) {
    return (
      <Stack gap="lg">
        <Skeleton height={36} width={120} />
        <Skeleton height={360} radius="md" />
        <Skeleton height={28} width="60%" />
        <Skeleton height={80} />
      </Stack>
    )
  }

  if (isError || !recipe) {
    return (
      <Stack gap="md">
        <Alert color="red" title="Recipe not found">
          We couldn't find that recipe.
        </Alert>
        <Button component={Link} to="/" variant="light" color="fresh" w="fit-content">
          Back to recipes
        </Button>
      </Stack>
    )
  }

  const baseYield = recipe.base_yield || 2
  const servings = servingsOverride ?? baseYield
  const factor = servings / baseYield

  return (
    <Stack gap="xl">
      <Button
        variant="subtle"
        color="gray"
        size="sm"
        w="fit-content"
        leftSection={<IconArrowLeft size={16} />}
        onClick={() => navigate(-1)}
      >
        Back
      </Button>

      <Grid gutter="xl">
        <Grid.Col span={{ base: 12, sm: 6 }}>
          <Image
            src={recipe.image_url}
            alt={recipe.name}
            radius="md"
            className={classes.hero}
          />
        </Grid.Col>
        <Grid.Col span={{ base: 12, sm: 6 }}>
          <Stack gap="sm" h="100%" justify="center">
            <Group gap="xs">
              {recipe.cuisines.map((c) => (
                <Badge key={c} color="fresh" variant="light" radius="sm">
                  {c}
                </Badge>
              ))}
              {recipe.tags.map((t) => (
                <Badge key={t} color="gray" variant="light" radius="sm">
                  {t}
                </Badge>
              ))}
            </Group>
            <Title order={1} className={classes.title}>
              {recipe.name}
            </Title>
            {recipe.headline && (
              <Text c="dimmed" fz="lg">
                {recipe.headline}
              </Text>
            )}

            <Group gap="lg" mt="xs">
              {recipe.avg_rating != null && (
                <Group gap={6} wrap="nowrap">
                  <IconStarFilled size={18} className={classes.star} />
                  <Text fw={600}>{recipe.avg_rating.toFixed(1)}</Text>
                  {recipe.ratings_count != null && (
                    <Text c="dimmed" size="sm">
                      ({recipe.ratings_count.toLocaleString()})
                    </Text>
                  )}
                </Group>
              )}
              {recipe.total_time_min != null && (
                <Group gap={6} wrap="nowrap">
                  <IconClock size={18} />
                  <Text>{recipe.total_time_min} min</Text>
                </Group>
              )}
              {recipe.difficulty != null && (
                <Group gap={6} wrap="nowrap">
                  <IconChefHat size={18} />
                  <Text>{DIFFICULTY[recipe.difficulty] ?? '—'}</Text>
                </Group>
              )}
              {recipe.base_yield != null && (
                <Group gap={6} wrap="nowrap">
                  <IconUsers size={18} />
                  <Text>Serves {recipe.base_yield}</Text>
                </Group>
              )}
            </Group>
          </Stack>
        </Grid.Col>
      </Grid>

      <SimpleGrid cols={{ base: 2, sm: 4 }} spacing="md">
        <MacroStat label="Energy" value={recipe.energy_kcal} unit=" kcal" />
        <MacroStat label="Protein" value={recipe.protein_g} unit="g" />
        <MacroStat label="Carbs" value={recipe.carbs_g} unit="g" />
        <MacroStat label="Fat" value={recipe.fat_g} unit="g" />
      </SimpleGrid>
      <Text size="xs" c="dimmed" mt={-12}>
        Per serving{recipe.serving_size_g ? ` · ~${Math.round(recipe.serving_size_g)}g` : ''}
        {recipe.protein_energy_ratio != null
          ? ` · ${recipe.protein_energy_ratio}g protein / 100 kcal`
          : ''}
      </Text>

      <Divider />

      <Grid gutter="xl">
        <Grid.Col span={{ base: 12, md: 4 }}>
          <Stack gap="md">
            <Title order={3}>Ingredients</Title>
            <div>
              <Text size="sm" fw={600} mb={4}>
                Servings
              </Text>
              <SegmentedControl
                size="xs"
                color="fresh"
                fullWidth
                value={String(servings)}
                onChange={(v) => setServingsOverride(Number(v))}
                data={SERVINGS.map((n) => ({ label: SERVINGS_LABELS[n], value: String(n) }))}
              />
            </div>
            <Stack gap="xs">
              {recipe.ingredients.map((ing, i) => (
                <Group key={i} gap="sm" wrap="nowrap" align="flex-start">
                  {ing.image_url && (
                    <Image src={ing.image_url} w={36} h={36} radius="sm" className={classes.ingImg} />
                  )}
                  <Text size="sm">
                    <Text span fw={600}>
                      {scaledQuantity(ing, factor)}{' '}
                    </Text>
                    {ing.name}
                  </Text>
                </Group>
              ))}
            </Stack>

            {recipe.allergens.length > 0 && (
              <>
                <Divider mt="sm" />
                <div>
                  <Text size="sm" fw={700} mb={6}>
                    Allergens
                  </Text>
                  <Group gap={6}>
                    {recipe.allergens.map((a) => (
                      <Badge key={a} variant="outline" color="gray" radius="sm" size="sm">
                        {a}
                      </Badge>
                    ))}
                  </Group>
                </div>
              </>
            )}
          </Stack>
        </Grid.Col>

        <Grid.Col span={{ base: 12, md: 8 }}>
          <Stack gap="md">
            <Title order={3}>Method</Title>
            <Stack gap="lg">
              {recipe.steps.map((step) => (
                <Group key={step.index} gap="md" align="flex-start" wrap="nowrap">
                  <ThemeIcon color="fresh" radius="xl" size={30} variant="filled">
                    {step.index}
                  </ThemeIcon>
                  <Text className={classes.step}>{step.text}</Text>
                </Group>
              ))}
            </Stack>
          </Stack>
        </Grid.Col>
      </Grid>

      {recipe.source_url && (
        <>
          <Divider />
          <Anchor href={recipe.source_url} target="_blank" c="dimmed" size="sm">
            <Group gap={6} wrap="nowrap">
              <IconExternalLink size={14} />
              View original on HelloFresh
            </Group>
          </Anchor>
        </>
      )}
    </Stack>
  )
}
