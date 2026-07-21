import { Link } from 'react-router-dom'
import { Badge, Card, Group, Image, Stack, Text, Tooltip } from '@mantine/core'
import { IconClock, IconFlame, IconGauge, IconStarFilled } from '@tabler/icons-react'

import classes from './RecipeCard.module.css'

const PROTEIN_DENSITY_BREAKPOINTS = [4, 6, 8]
const PROTEIN_DENSITY_SEGMENTS = PROTEIN_DENSITY_BREAKPOINTS.length + 1

const PLACEHOLDER =
  'data:image/svg+xml;utf8,' +
  encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="100%" height="100%" fill="#e9f9f0"/></svg>',
  )

function round(value) {
  return value == null ? null : Math.round(value)
}

function proteinDensityLevel(value) {
  if (value == null) return 0

  return PROTEIN_DENSITY_BREAKPOINTS.filter((breakpoint) => value >= breakpoint).length + 1
}

function ProteinDensityMeter({ value }) {
  const activeSegments = proteinDensityLevel(value)
  const label = `${value}g/100kcal protein density`

  return (
    <Tooltip label={label} withArrow position="top">
      <span className={classes.densityMetric} aria-label={label}>
        <IconGauge size={14} className={classes.densityIcon} />
        <span className={classes.densityMeter} aria-hidden="true">
          {Array.from({ length: PROTEIN_DENSITY_SEGMENTS }).map((_, index) => (
            <span
              key={index}
              className={[
                classes.densitySegment,
                index < activeSegments ? classes.densitySegmentActive : '',
              ]
                .filter(Boolean)
                .join(' ')}
            />
          ))}
        </span>
      </span>
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

      <Stack gap={6} p="sm" className={classes.body}>
        <div className={classes.content}>
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
          {recipe.tags?.length > 0 && (
            <Group gap={6} mt={4}>
              {recipe.tags.slice(0, 2).map((tag) => (
                <Badge key={tag} variant="light" color="fresh" size="sm" radius="sm">
                  {tag}
                </Badge>
              ))}
            </Group>
          )}
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
                <Text size="xs">{round(recipe.protein_g)}g protein</Text>
              )}
            </StatSlot>
          </div>

          <div className={classes.statRow}>
            <StatSlot>
              {recipe.protein_energy_ratio != null && (
                <ProteinDensityMeter value={recipe.protein_energy_ratio} />
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
      </Stack>
    </Card>
  )
}
