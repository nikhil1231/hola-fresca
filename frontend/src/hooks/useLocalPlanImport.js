import { useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'

import { importPlan } from '../api/planClient.js'
import { PLAN_KEY } from './usePlanQuery.js'

// A one-off: lift whatever plan is sitting in this browser's localStorage up to
// the account, once, then stop. Before accounts the plan only ever existed in
// the browser that made it, so without this the upgrade would silently look like
// having lost next week's shop.
//
// Deliberately quiet and deliberately safe. It runs unattended on first load, so
// it must not be able to destroy a plan (the server ignores weeks that already
// have recipes) and must not be able to leave anyone stuck (a failure is logged
// and the flag is left unset, so it simply tries again next time).

const PLAN_STORAGE_KEY = 'hola-fresca.weekly-plan.v1'
const PACK_STORAGE_KEY = 'hola-fresca.week-pack-choices.v1'
const OWNED_STORAGE_KEY = 'hola-fresca.owned-basket-items.v1'
const DONE_KEY = 'hola-fresca.plan-imported.v1'

function read(key) {
  try {
    return JSON.parse(window.localStorage.getItem(key)) ?? null
  } catch {
    return null
  }
}

/** Everything the three old keys held, as the import endpoint's week list. */
export function collectLocalPlan() {
  const plan = read(PLAN_STORAGE_KEY)
  const packs = read(PACK_STORAGE_KEY)
  const owned = read(OWNED_STORAGE_KEY)

  const weekStarts = new Set([
    ...Object.keys(plan?.weeks ?? {}),
    ...Object.keys(packs?.weeks ?? {}),
    ...Object.keys(packs?.snaps ?? {}),
    ...Object.keys(owned?.weeks ?? {}),
  ])

  const weeks = []
  for (const weekStart of weekStarts) {
    const recipes = (plan?.weeks?.[weekStart]?.recipes ?? [])
      .filter((entry) => entry?.recipe?.id != null)
      .map((entry) => ({
        recipe_id: entry.recipe.id,
        portions: entry.portions ?? null,
        protein: entry.protein ?? null,
      }))
    const packOverrides = packs?.weeks?.[weekStart] ?? {}
    const snapOverrides = packs?.snaps?.[weekStart] ?? {}
    const ownedKeys = owned?.weeks?.[weekStart] ?? []
    if (!recipes.length && !Object.keys(packOverrides).length
        && !Object.keys(snapOverrides).length && !ownedKeys.length) {
      continue
    }
    weeks.push({
      week_start: weekStart,
      recipes,
      pack_overrides: packOverrides,
      snap_overrides: snapOverrides,
      owned_item_keys: ownedKeys,
    })
  }
  return weeks
}

function clearLocalPlan() {
  for (const key of [PLAN_STORAGE_KEY, PACK_STORAGE_KEY, OWNED_STORAGE_KEY]) {
    window.localStorage.removeItem(key)
  }
}

export function useLocalPlanImport() {
  const queryClient = useQueryClient()
  // React 18's StrictMode mounts effects twice in development. The import is
  // additive so a second run is harmless, but the request is pointless.
  const started = useRef(false)

  useEffect(() => {
    if (started.current || typeof window === 'undefined') return
    started.current = true
    if (window.localStorage.getItem(DONE_KEY)) return

    const weeks = collectLocalPlan()
    if (!weeks.length) {
      // Nothing to carry over — a new browser, or a plan already imported from
      // another one. Record it so this never looks at localStorage again.
      window.localStorage.setItem(DONE_KEY, new Date().toISOString())
      return
    }

    importPlan(weeks)
      .then((result) => {
        window.localStorage.setItem(DONE_KEY, new Date().toISOString())
        // Only once the server has it: dropping the local copy on a failed
        // import would be the one way this could actually lose a plan.
        clearLocalPlan()
        queryClient.setQueryData(PLAN_KEY, result.plan)
        if (result.skipped_recipes?.length) {
          console.info(
            'Plan import skipped recipes no longer in the library:',
            result.skipped_recipes,
          )
        }
      })
      .catch((error) => {
        console.warn('Could not import the local plan; will retry next load', error)
      })
  }, [queryClient])
}
