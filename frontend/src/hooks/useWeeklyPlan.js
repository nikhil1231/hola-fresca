import { useCallback, useEffect, useMemo, useState } from 'react'

export const DEFAULT_PORTIONS = 4
export const MIN_PORTIONS = 1
export const MAX_PORTIONS = 8
// How many recipes a week holds is a setting now (schedule settings,
// `recipes_per_week`), so the cap a page enforces is passed in. This is only the
// ceiling stored state is trimmed to, well above any sane setting: lowering the
// setting must not silently delete recipes already chosen for a week.
export const PLAN_WEEK_STORAGE_LIMIT = 14
export const DEFAULT_RECIPES_PER_WEEK = 5

const STORAGE_KEY = 'hola-fresca.weekly-plan.v1'

function pad(n) {
  return String(n).padStart(2, '0')
}

export function formatWeekStart(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

export function getUpcomingWeekStart(input = new Date()) {
  const date = new Date(input)
  date.setHours(0, 0, 0, 0)
  const daysUntilMonday = (8 - date.getDay()) % 7
  date.setDate(date.getDate() + daysUntilMonday)
  return formatWeekStart(date)
}

export function formatWeekLabel(weekStart) {
  const date = new Date(`${weekStart}T00:00:00`)
  return new Intl.DateTimeFormat(undefined, {
    weekday: 'long',
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  }).format(date)
}

export function toPlannerSelections(entries) {
  return entries.map((entry) => ({
    recipe_id: entry.recipe.id,
    portions: entry.portions,
    ...(entry.protein ? { protein: entry.protein } : {}),
  }))
}

// A protein swap/scale belongs to the week, not the recipe: the library keeps
// publishing the dish as written, and this rides along with the plan entry the
// same way portions do, so it expires when the week does.
const PROTEIN_MODES = ['protein_g', 'energy_kcal']

export function normalizeProtein(value) {
  if (!value || typeof value !== 'object') return null
  const protein = {}
  if (typeof value.swap_to === 'string' && value.swap_to) protein.swap_to = value.swap_to
  const scale = Number(value.scale)
  if (Number.isFinite(scale) && scale > 0) protein.scale = Math.min(4, Math.max(0.25, scale))
  const targetValue = Number(value.target_value)
  if (PROTEIN_MODES.includes(value.target_mode) && Number.isFinite(targetValue) && targetValue > 0) {
    protein.target_mode = value.target_mode
    protein.target_value = targetValue
  }
  return Object.keys(protein).length > 0 ? protein : null
}

function recipeSnapshot(recipe) {
  return {
    id: recipe.id,
    name: recipe.name,
    headline: recipe.headline,
    image_url: recipe.image_url,
    energy_kcal: recipe.energy_kcal,
    protein_g: recipe.protein_g,
    protein_energy_ratio: recipe.protein_energy_ratio,
    total_time_min: recipe.total_time_min,
    difficulty: recipe.difficulty,
    avg_rating: recipe.avg_rating,
    ratings_count: recipe.ratings_count,
    cuisines: recipe.cuisines ?? [],
    tags: recipe.tags ?? [],
  }
}

function clampPortions(portions) {
  const value = Number(portions)
  if (!Number.isFinite(value)) return DEFAULT_PORTIONS
  return Math.min(MAX_PORTIONS, Math.max(MIN_PORTIONS, Math.round(value)))
}

function normalizePlan(value) {
  if (!value || typeof value !== 'object') return { weeks: {} }
  const weeks = {}
  for (const [weekStart, week] of Object.entries(value.weeks ?? {})) {
    const recipes = Array.isArray(week?.recipes) ? week.recipes : []
    weeks[weekStart] = {
      recipes: recipes
        .filter((entry) => entry?.recipe?.id != null)
        .slice(0, PLAN_WEEK_STORAGE_LIMIT)
        .map((entry) => ({
          recipe: recipeSnapshot(entry.recipe),
          portions: clampPortions(entry.portions),
          protein: normalizeProtein(entry.protein),
          addedAt: entry.addedAt ?? new Date().toISOString(),
        })),
    }
  }
  return { weeks }
}

function readStoredPlan() {
  if (typeof window === 'undefined') return { weeks: {} }
  try {
    return normalizePlan(JSON.parse(window.localStorage.getItem(STORAGE_KEY)))
  } catch {
    return { weeks: {} }
  }
}

function writeStoredPlan(plan) {
  if (typeof window === 'undefined') return
  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(plan))
}

export function useWeeklyPlan() {
  const [plan, setPlan] = useState(readStoredPlan)
  const upcomingWeekStart = useMemo(() => getUpcomingWeekStart(), [])

  useEffect(() => {
    writeStoredPlan(plan)
  }, [plan])

  useEffect(() => {
    function handleStorage(event) {
      if (event.key === STORAGE_KEY) setPlan(readStoredPlan())
    }
    window.addEventListener('storage', handleStorage)
    return () => window.removeEventListener('storage', handleStorage)
  }, [])

  const updatePlan = useCallback((updater) => {
    setPlan((current) => normalizePlan(updater(current)))
  }, [])

  const getWeekRecipes = useCallback(
    (weekStart = upcomingWeekStart) => plan.weeks[weekStart]?.recipes ?? [],
    [plan.weeks, upcomingWeekStart],
  )

  const getRecipeEntry = useCallback(
    (recipeId, weekStart = upcomingWeekStart) =>
      getWeekRecipes(weekStart).find((entry) => entry.recipe.id === recipeId) ?? null,
    [getWeekRecipes, upcomingWeekStart],
  )

  const addRecipeToWeek = useCallback(
    (recipe, weekStart = upcomingWeekStart, { protein = null, limit = DEFAULT_RECIPES_PER_WEEK } = {}) => {
      let added = false
      updatePlan((current) => {
        const week = current.weeks[weekStart] ?? { recipes: [] }
        const existing = week.recipes.find((entry) => entry.recipe.id === recipe.id)
        if (existing || week.recipes.length >= Math.min(limit, PLAN_WEEK_STORAGE_LIMIT)) {
          return current
        }
        added = true
        return {
          ...current,
          weeks: {
            ...current.weeks,
            [weekStart]: {
              recipes: [
                ...week.recipes,
                {
                  recipe: recipeSnapshot(recipe),
                  portions: DEFAULT_PORTIONS,
                  protein: normalizeProtein(protein),
                  addedAt: new Date().toISOString(),
                },
              ],
            },
          },
        }
      })
      return added
    },
    [upcomingWeekStart, updatePlan],
  )

  const removeRecipeFromWeek = useCallback(
    (weekStart, recipeId) => {
      updatePlan((current) => {
        const week = current.weeks[weekStart]
        if (!week) return current
        const recipes = week.recipes.filter((entry) => entry.recipe.id !== recipeId)
        return {
          ...current,
          weeks: {
            ...current.weeks,
            [weekStart]: { recipes },
          },
        }
      })
    },
    [updatePlan],
  )

  const setRecipePortions = useCallback(
    (weekStart, recipeId, portions) => {
      updatePlan((current) => {
        const week = current.weeks[weekStart]
        if (!week) return current
        return {
          ...current,
          weeks: {
            ...current.weeks,
            [weekStart]: {
              recipes: week.recipes.map((entry) =>
                entry.recipe.id === recipeId
                  ? { ...entry, portions: clampPortions(portions) }
                  : entry,
              ),
            },
          },
        }
      })
    },
    [updatePlan],
  )

  const setRecipeProtein = useCallback(
    (weekStart, recipeId, protein) => {
      updatePlan((current) => {
        const week = current.weeks[weekStart]
        if (!week) return current
        return {
          ...current,
          weeks: {
            ...current.weeks,
            [weekStart]: {
              recipes: week.recipes.map((entry) =>
                entry.recipe.id === recipeId
                  ? { ...entry, protein: normalizeProtein(protein) }
                  : entry,
              ),
            },
          },
        }
      })
    },
    [updatePlan],
  )

  const weekStarts = useMemo(() => {
    const starts = Object.entries(plan.weeks)
      .filter(([weekStart, week]) => weekStart >= upcomingWeekStart && week.recipes.length > 0)
      .map(([weekStart]) => weekStart)
      .sort()
    return starts.includes(upcomingWeekStart) ? starts : [upcomingWeekStart, ...starts]
  }, [plan.weeks, upcomingWeekStart])

  return {
    plan,
    upcomingWeekStart,
    weekStarts,
    getWeekRecipes,
    getRecipeEntry,
    addRecipeToWeek,
    removeRecipeFromWeek,
    setRecipePortions,
    setRecipeProtein,
  }
}
