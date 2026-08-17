import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { fetchCooked, setCookedMark } from '../api/planClient.js'
import { fetchPantry, setPantryItem } from '../api/pantryClient.js'

// Cooked history and the cupboard, together because they move together: unmark
// a recipe and its share of every lot goes back on the shelf, which changes
// what the next basket buys. Every write here therefore invalidates the
// pantry and the priced basket alongside its own query.

export const COOKED_KEY = ['cooked']
export const PANTRY_KEY = ['pantry']

export function useCooked(weekStarts) {
  return useQuery({
    queryKey: [...COOKED_KEY, weekStarts],
    queryFn: () => fetchCooked(weekStarts),
    enabled: weekStarts.length > 0,
    placeholderData: (previous) => previous,
  })
}

export function useSetCooked() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: setCookedMark,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: COOKED_KEY })
      queryClient.invalidateQueries({ queryKey: PANTRY_KEY })
      queryClient.invalidateQueries({ queryKey: ['planner-basket'] })
    },
  })
}

export function usePantry() {
  return useQuery({ queryKey: PANTRY_KEY, queryFn: fetchPantry })
}

export function useSetPantryItem() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: setPantryItem,
    // The write returns the whole cupboard as it now stands, so plant it
    // rather than refetch; the basket still has to be re-priced.
    onSuccess: (pantry) => {
      queryClient.setQueryData(PANTRY_KEY, pantry)
      queryClient.invalidateQueries({ queryKey: ['planner-basket'] })
    },
  })
}

export function formatHeld(item) {
  if (item.unit_kind === 'count' && item.held_qty != null) {
    return `×${Math.round(item.held_qty * 10) / 10}`
  }
  if (item.held_g >= 1000) return `${(item.held_g / 1000).toFixed(1)} kg`
  return `${Math.round(item.held_g)} g`
}
