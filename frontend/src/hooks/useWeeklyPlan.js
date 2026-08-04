import { useCallback, useMemo } from 'react'
import { useMutation } from '@tanstack/react-query'

import {
  addPlanRecipe,
  removePlanRecipe,
  updatePlanRecipe,
} from '../api/planClient.js'
import { usePlan, useOptimisticWeek, usePlantWeek } from './usePlanQuery.js'

// The week's recipes. This was localStorage — one plan per browser — and is now
// one plan per account, so it follows you between devices. The hook's shape is
// unchanged on purpose: the six pages that use it did not need to learn that
// their plan had moved.
//
// Two things the move changed, both visible here. Recipes come back as cards the
// server rendered, rather than as a snapshot cached beside each entry, so a
// renamed or re-priced dish is never stale. And every edit is one request for
// one entry, so two devices editing the same week do not overwrite each other.

export const DEFAULT_PORTIONS = 4
export const MIN_PORTIONS = 1
export const MAX_PORTIONS = 8
// How many recipes a week holds is a setting (schedule settings,
// `recipes_per_week`), so the cap a page enforces is passed in. This is only the
// ceiling stored state is trimmed to, well above any sane setting: lowering the
// setting must not silently delete recipes already chosen for a week. The server
// enforces the same number.
export const PLAN_WEEK_STORAGE_LIMIT = 14
export const DEFAULT_RECIPES_PER_WEEK = 5

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

export const PROTEIN_SWAP_LABELS = {
  chicken_breast: 'Chicken breast',
  chicken_thigh: 'Chicken thigh',
  beef: 'Beef mince',
  pork: 'Pork mince',
  lamb: 'Lamb mince',
  salmon: 'Salmon',
  basa: 'Basa',
  tofu: 'Tofu',
  halloumi: 'Halloumi',
}

/** How a week's protein modifier reads in one line: the swap if there is one,
 *  then what the scaling is doing.
 *
 *  Lives beside the modifier itself rather than in the card that first showed
 *  it, because every page that renders a planned recipe owes you this: the dish
 *  in the week is not the dish the library publishes, and the basket was priced
 *  for the modified one. */
export function formatProteinModifier(protein) {
  if (!protein) return null
  const parts = []
  if (protein.swap_to) parts.push(PROTEIN_SWAP_LABELS[protein.swap_to] ?? protein.swap_to)
  if (protein.scale) parts.push(`${protein.scale}x protein`)
  else if (protein.target_mode === 'protein_g') parts.push(`${protein.target_value}g protein pp`)
  else if (protein.target_mode === 'energy_kcal') parts.push(`${protein.target_value} kcal pp`)
  return parts.join(' · ') || null
}

function clampPortions(portions) {
  const value = Number(portions)
  if (!Number.isFinite(value)) return DEFAULT_PORTIONS
  return Math.min(MAX_PORTIONS, Math.max(MIN_PORTIONS, Math.round(value)))
}

const EMPTY_RECIPES = []

export function useWeeklyPlan() {
  const { data } = usePlan()
  const plantWeek = usePlantWeek()
  const optimistic = useOptimisticWeek()
  const upcomingWeekStart = useMemo(() => getUpcomingWeekStart(), [])

  const weeks = data?.weeks
  // One mutation per kind of edit rather than per call site, so an in-flight add
  // and an in-flight portions change do not queue behind each other.
  const add = useMutation({ mutationFn: addPlanRecipe, onSuccess: plantWeek })
  const remove = useMutation({ mutationFn: removePlanRecipe, onSuccess: plantWeek })
  const update = useMutation({ mutationFn: updatePlanRecipe, onSuccess: plantWeek })

  const getWeekRecipes = useCallback(
    (weekStart = upcomingWeekStart) => weeks?.[weekStart]?.recipes ?? EMPTY_RECIPES,
    [weeks, upcomingWeekStart],
  )

  const getRecipeEntry = useCallback(
    (recipeId, weekStart = upcomingWeekStart) =>
      getWeekRecipes(weekStart).find((entry) => entry.recipe.id === recipeId) ?? null,
    [getWeekRecipes, upcomingWeekStart],
  )

  // How a recipe is being cooked, when the week is a guess rather than a given.
  // The week asked for wins; failing that, the nearest week that holds the
  // recipe, preferring one that has already started — you are standing in the
  // kitchen, so the shop that bought the ingredients is behind you far more
  // often than ahead. A link with no week at all is an old bookmark, and the
  // last time you planned the dish says much more about how you are cooking it
  // than an empty week does.
  const getCookingEntry = useCallback(
    (recipeId, weekStart) => {
      const asked = weekStart ? getRecipeEntry(recipeId, weekStart) : null
      if (asked) return asked
      const holding = Object.entries(weeks ?? {})
        .filter(([, week]) => week.recipes.some((entry) => entry.recipe.id === recipeId))
        .map(([start]) => start)
        .sort()
      const started = holding.filter((start) => start <= upcomingWeekStart)
      const chosen = started.length ? started[started.length - 1] : holding[0]
      return chosen ? getRecipeEntry(recipeId, chosen) : null
    },
    [getRecipeEntry, upcomingWeekStart, weeks],
  )

  const addRecipeToWeek = useCallback(
    (recipe, weekStart = upcomingWeekStart, { protein = null, limit = DEFAULT_RECIPES_PER_WEEK } = {}) => {
      const existing = weeks?.[weekStart]?.recipes ?? EMPTY_RECIPES
      if (existing.some((entry) => entry.recipe.id === recipe.id)) return false
      if (existing.length >= Math.min(limit, PLAN_WEEK_STORAGE_LIMIT)) return false

      const cleanProtein = normalizeProtein(protein)
      // The card is already on screen, so the optimistic entry is the real thing
      // rather than a placeholder; the server's copy replaces it on the response.
      optimistic(weekStart, (week) => ({
        ...week,
        recipes: [
          ...week.recipes,
          {
            recipe,
            portions: DEFAULT_PORTIONS,
            protein: cleanProtein,
            added_at: new Date().toISOString(),
          },
        ],
      })).then((rollback) =>
        add.mutateAsync({
          weekStart,
          recipeId: recipe.id,
          portions: DEFAULT_PORTIONS,
          protein: cleanProtein,
        }).catch((error) => {
          rollback()
          throw error
        }),
      )
      return true
    },
    [add, optimistic, upcomingWeekStart, weeks],
  )

  const removeRecipeFromWeek = useCallback(
    (weekStart, recipeId) => {
      optimistic(weekStart, (week) => ({
        ...week,
        recipes: week.recipes.filter((entry) => entry.recipe.id !== recipeId),
      })).then((rollback) =>
        remove.mutateAsync({ weekStart, recipeId }).catch((error) => {
          rollback()
          throw error
        }),
      )
    },
    [optimistic, remove],
  )

  const setRecipePortions = useCallback(
    (weekStart, recipeId, portions) => {
      const clamped = clampPortions(portions)
      optimistic(weekStart, (week) => ({
        ...week,
        recipes: week.recipes.map((entry) =>
          entry.recipe.id === recipeId ? { ...entry, portions: clamped } : entry,
        ),
      })).then((rollback) =>
        update.mutateAsync({ weekStart, recipeId, portions: clamped }).catch((error) => {
          rollback()
          throw error
        }),
      )
    },
    [optimistic, update],
  )

  const setRecipeProtein = useCallback(
    (weekStart, recipeId, protein) => {
      const cleanProtein = normalizeProtein(protein)
      optimistic(weekStart, (week) => ({
        ...week,
        recipes: week.recipes.map((entry) =>
          entry.recipe.id === recipeId ? { ...entry, protein: cleanProtein } : entry,
        ),
      })).then((rollback) =>
        // Explicitly null rather than absent: that is how the modifier is taken
        // off again, and an absent field would mean "leave it alone".
        update.mutateAsync({ weekStart, recipeId, protein: cleanProtein }).catch((error) => {
          rollback()
          throw error
        }),
      )
    },
    [optimistic, update],
  )

  const weekStarts = useMemo(() => {
    const starts = Object.entries(weeks ?? {})
      .filter(([weekStart, week]) => weekStart >= upcomingWeekStart && week.recipes.length > 0)
      .map(([weekStart]) => weekStart)
      .sort()
    return starts.includes(upcomingWeekStart) ? starts : [upcomingWeekStart, ...starts]
  }, [weeks, upcomingWeekStart])

  // Every week that holds recipes, however old. Weeks that have been and gone
  // are not planning targets, but their baskets are still worth opening, so the
  // pages that let you pick a week need them offered rather than filtered out.
  const plannedWeekStarts = useMemo(
    () =>
      Object.entries(weeks ?? {})
        .filter(([, week]) => week.recipes.length > 0)
        .map(([weekStart]) => weekStart)
        .sort(),
    [weeks],
  )

  return {
    plan: data ?? { weeks: {} },
    upcomingWeekStart,
    weekStarts,
    plannedWeekStarts,
    getWeekRecipes,
    getRecipeEntry,
    getCookingEntry,
    addRecipeToWeek,
    removeRecipeFromWeek,
    setRecipePortions,
    setRecipeProtein,
  }
}
