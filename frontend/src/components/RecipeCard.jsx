import { Link } from 'react-router-dom'
import { Badge, Card, Group, Image, Text, Tooltip } from '@mantine/core'
import { IconChefHat, IconClock, IconFlame, IconStarFilled } from '@tabler/icons-react'

import classes from './RecipeCard.module.css'

const DIFFICULTY = { 1: 'Easy', 2: 'Medium', 3: 'Hard' }

const PLACEHOLDER =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="100%" height="100%" fill="#e9f9f0"/></svg>',
  )

function round(value) {
  return value == null ? null : Math.round(value)
}

function ProteinValue({ protein, density }) {
  const value = <Text size="xs">{round(protein)}g protein</Text>

  if (density == null) return value

  return (
    <Tooltip label={`${density}g protein / 100 kcal`} withArrow position="top">
      {value}
    </Tooltip>
  )
}

function StatSlot({ children, strong = false }) {
  return (
    <div className={strong ? `${classes.statSlot} ${classes.statSlotStrong}` : classes.statSlot}>
      {children}
    </div>
  )
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

      <div className={classes.body}>
        <div className={classes.copy}>
          {recipe.cuisines?.length > 0 && (
            <Text size="xs" c="fresh.8" fw={600} tt="uppercase" className={classes.cuisine}>
              {recipe.cuisines[0]}
            </Text>
          )}
          <Text fw={600} lineClamp={2} className={classes.title}>
            {recipe.name}
          </Text>
          {recipe.headline && (
            <Text size="xs" c="dimmed" lineClamp={1} className={classes.subtitle}>
              {recipe.headline}
            </Text>
          )}
          <div className={classes.badgeRow}>
            {recipe.tags?.length > 0 &&
              recipe.tags.slice(0, 2).map((tag) => (
                <Badge key={tag} variant="light" color="fresh" size="sm" radius="sm">
                  {tag}
                </Badge>
              ))}
          </div>
        </div>

        <div className={classes.stats}>
          <div className={classes.statRow}>
            <StatSlot>
              {recipe.energy_kcal != null && (
                <>
                  <IconFlame size={14} />
                  <Text size="xs">{round(recipe.energy_kcal)} kcal</Text>
                </>
              )}
            </StatSlot>
            <StatSlot strong>
              {recipe.protein_g != null && (
                <ProteinValue
                  protein={recipe.protein_g}
                  density={recipe.protein_energy_ratio}
                />
              )}
            </StatSlot>
          </div>

          <div className={classes.statRow}>
            <StatSlot>
              {recipe.difficulty != null && (
                <>
                  <IconChefHat size={14} />
                  <Text size="xs">{DIFFICULTY[recipe.difficulty] ?? 'Difficulty'}</Text>
                </>
              )}
            </StatSlot>
            <StatSlot>
              {recipe.total_time_min != null && (
                <>
                  <IconClock size={14} />
                  <Text size="xs">{recipe.total_time_min} min</Text>
                </>
              )}
            </StatSlot>
          </div>
        </div>
      </div>
    </Card>
  )
}
