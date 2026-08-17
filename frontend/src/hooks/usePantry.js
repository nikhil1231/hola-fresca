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

export function unitLabel(unitKind) {
  return unitKind === 'count' ? 'units' : 'g'
}

/** Where a figure came from — a shop's leftovers, or something you said.
 *  A confirmation outranks the shop it started as: what matters for trust is
 *  when a person last vouched for the row, not where the row began. */
export function provenance(item) {
  if (item.confirmed_week_start) return { kind: 'stated', week: item.confirmed_week_start }
  return { kind: 'shop', week: item.week_start }
}
