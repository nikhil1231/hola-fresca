import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchPantry,
  fetchPantryIngredients,
  removePantryItem,
  setPantryItem,
} from '../api/pantryClient.js'

// The cupboard. Every write here changes what the next basket buys, so all of
// them invalidate the priced basket alongside planting the cupboard the write
// returned — the server sends the whole thing back, so there is nothing to
// refetch.

export const PANTRY_KEY = ['pantry']

export function usePantry() {
  return useQuery({ queryKey: PANTRY_KEY, queryFn: fetchPantry })
}

export function usePantryIngredients(q) {
  return useQuery({
    queryKey: [...PANTRY_KEY, 'ingredients', q],
    queryFn: () => fetchPantryIngredients(q),
    placeholderData: (previous) => previous,
  })
}

function usePantryWrite(mutationFn) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: (pantry) => {
      queryClient.setQueryData(PANTRY_KEY, pantry)
      // What is held decides what a line still has to buy.
      queryClient.invalidateQueries({ queryKey: ['planner-basket'] })
      // "Already in the cupboard" is part of the add box's answer.
      queryClient.invalidateQueries({ queryKey: [...PANTRY_KEY, 'ingredients'] })
    },
  })
}

export function useSetPantryItem() {
  return usePantryWrite(setPantryItem)
}

export function useRemovePantryItem() {
  return usePantryWrite(removePantryItem)
}

/** How much is held, in whichever space the ingredient is counted in. */
export function formatHeld(item) {
  if (item.unit_kind === 'count' && item.held_qty != null) {
    const rounded = Math.round(item.held_qty * 10) / 10
    return `${rounded} ${rounded === 1 ? 'unit' : 'units'}`
  }
  if (item.held_g >= 1000) return `${(item.held_g / 1000).toFixed(1)} kg`
  return `${Math.round(item.held_g)} g`
}

/** The editable number, which is the held figure in the ingredient's own unit. */
export function heldValue(item) {
  return item.unit_kind === 'count'
    ? Math.round((item.held_qty ?? 0) * 10) / 10
    : Math.round(item.held_g ?? 0)
}

export function unitLabel(unitKind, amount = 0) {
  if (unitKind !== 'count') return 'g'
  return Number(amount) === 1 ? 'unit' : 'units'
}

/** Where a figure came from — a shop's leftovers, or something you said.
 *  A confirmation outranks the shop it started as: what matters for trust is
 *  when a person last vouched for the row, not where the row began. */
export function provenance(item) {
  if (item.confirmed_week_start) return { kind: 'stated', week: item.confirmed_week_start }
  return { kind: 'shop', week: item.week_start }
}

/** A use-by as a date input wants it, and back. The API speaks YYYY-MM-DD,
 *  which is what <input type="date"> uses too, so this is only a null guard. */
export function useByValue(item) {
  return item?.use_by ?? ''
}

/** How a date reads next to a quantity: what it means is when the stock stops
 *  counting, so the wording is about the food rather than about the field. */
export function formatUseBy(useBy) {
  if (!useBy) return null
  const date = new Date(`${useBy}T00:00:00`)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' }).format(date)
}

/** Today as YYYY-MM-DD, for the date input's floor — a use-by in the past would
 *  mean the food is already gone, which is what removing it says. */
export function todayIso() {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}
