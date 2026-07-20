import { Link } from 'react-router-dom'
import { Badge, Card, Group, Image, Stack, Text } from '@mantine/core'
import { IconClock, IconFlame, IconStarFilled } from '@tabler/icons-react'

import classes from './RecipeCard.module.css'

const PLACEHOLDER =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="100%" height="100%" fill="#e9f9f0"/></svg>',
  )

function round(value) {
  return value == null ? null : Math.round(value)
}

export default function RecipeCard({ recipe }) {
  return (
    <Card
      component={Link}
      to={`/recipes/${recipe.id}`}
      padding="0"
      radius="md"
      withBorder
      className={classes.card}
    >
      <Card.Section className={classes.imageWrap}>
        <Image
          src={recipe.image_url || PLACEHOLDER}
          fallbackSrc={PLACEHOLDER}
          alt={recipe.name}
          className={classes.image}
          loading="lazy"
        />
        {recipe.ratings_count != null && (
          <Badge className={classes.ratingBadge} variant="filled" color="dark" radius="sm">
            <Group gap={3} wrap="nowrap">
              <IconStarFilled size={11} />
              {recipe.avg_rating != null ? recipe.avg_rating.toFixed(1) : '—'}
            </Group>
          </Badge>
        )}
      </Card.Section>

      <Stack gap={6} p="sm" className={classes.body}>
        {recipe.cuisines?.length > 0 && (
          <Text size="xs" c="fresh.8" fw={600} tt="uppercase" className={classes.cuisine}>
            {recipe.cuisines[0]}
          </Text>
        )}
        <Text fw={600} lineClamp={2} className={classes.title}>
          {recipe.name}
        </Text>
        {recipe.headline && (
          <Text size="xs" c="dimmed" lineClamp={1}>
            {recipe.headline}
          </Text>
        )}

        <Group gap="md" mt={4} className={classes.meta}>
          {recipe.energy_kcal != null && (
            <Group gap={4} wrap="nowrap">
              <IconFlame size={14} />
              <Text size="xs">{round(recipe.energy_kcal)} kcal</Text>
            </Group>
          )}
          {recipe.protein_g != null && (
            <Text size="xs" fw={600}>
              {round(recipe.protein_g)}g protein
            </Text>
          )}
          {recipe.protein_energy_ratio != null && (
            <Text size="xs" c="dimmed" title="protein density">
              {recipe.protein_energy_ratio}g/100kcal
            </Text>
          )}
          {recipe.total_time_min != null && (
            <Group gap={4} wrap="nowrap">
              <IconClock size={14} />
              <Text size="xs">{recipe.total_time_min} min</Text>
            </Group>
          )}
        </Group>

        {recipe.tags?.length > 0 && (
          <Group gap={6} mt={4}>
            {recipe.tags.slice(0, 2).map((tag) => (
              <Badge key={tag} variant="light" color="fresh" size="sm" radius="sm">
                {tag}
              </Badge>
            ))}
          </Group>
        )}
      </Stack>
    </Card>
  )
}
