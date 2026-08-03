import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ActionIcon, Alert, Button, Image, Skeleton, Text, Title } from '@mantine/core'
import { IconArrowLeft, IconArrowRight, IconCheck, IconX } from '@tabler/icons-react'

import { useRecipe } from '../hooks/useRecipeQueries.js'
import { usePreloadStepImages } from '../hooks/usePreloadStepImages.js'
import { RECIPE_PLACEHOLDER_IMAGE } from '../constants/images.js'
import classes from './CookingPage.module.css'

export default function CookingPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { data: recipe, isLoading, isError } = useRecipe(id)
  const [stepPosition, setStepPosition] = useState(0)
  usePreloadStepImages(recipe?.steps)

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

  const steps = recipe.steps ?? []
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

  const currentStep = steps[stepPosition]
  const isFirst = stepPosition === 0
  const isLast = stepPosition === steps.length - 1
  const image = currentStep.image_url || recipe.image_url || RECIPE_PLACEHOLDER_IMAGE

  const finish = () => navigate(`/recipes/${recipe.id}`, { replace: true })

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
        </div>
        <ActionIcon
          component={Link}
          to={`/recipes/${recipe.id}`}
          replace
          variant="subtle"
          color="gray"
          size="lg"
          radius="xl"
          aria-label="Exit cooking mode"
        >
          <IconX size={21} />
        </ActionIcon>
      </header>

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
          onClick={() => setStepPosition((position) => position - 1)}
        >
          <IconArrowLeft size={22} />
        </ActionIcon>
        <Text fw={700} size="sm" ta="center">
          Step {stepPosition + 1} of {steps.length}
        </Text>
        <ActionIcon
          variant="filled"
          color="fresh"
          size="xl"
          radius="xl"
          aria-label={isLast ? 'Finish cooking' : 'Next step'}
          onClick={isLast ? finish : () => setStepPosition((position) => position + 1)}
        >
          {isLast ? <IconCheck size={22} /> : <IconArrowRight size={22} />}
        </ActionIcon>
      </nav>
    </main>
  )
}
