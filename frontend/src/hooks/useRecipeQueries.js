import { useInfiniteQuery, useQuery } from '@tanstack/react-query'

import { fetchFacets, fetchRecipe, fetchRecipes } from '../api/client.js'

const PAGE_SIZE = 24

// Paginated recipe list as an infinite query keyed on the active filters.
export function useRecipes(filters) {
  return useInfiniteQuery({
    queryKey: ['recipes', filters],
    queryFn: ({ pageParam = 1 }) => fetchRecipes(filters, pageParam, PAGE_SIZE),
    initialPageParam: 1,
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.page + 1 : undefined,
  })
}

export function useRecipe(id) {
  return useQuery({
    queryKey: ['recipe', id],
    queryFn: () => fetchRecipe(id),
    enabled: id != null,
  })
}

export function useFacets() {
  return useQuery({
    queryKey: ['facets'],
    queryFn: fetchFacets,
    staleTime: Infinity,
  })
}
