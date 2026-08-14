import { useEffect, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchAuditJob,
  fetchCookMap,
  fetchFacets,
  fetchPlannerBasket,
  fetchPlannerSuggestions,
  fetchProteinPreview,
  fetchRecipe,
  fetchRecipes,
  flagRecipe,
  hideRecipe,
  refreshPlannerStock,
  revertRecipeEdits,
  retryCookMap,
  setPackPreference,
  setPersonalRecipeRating,
  setRecipeWishlist,
  startCookMap,
} from '../api/client.js'

const PAGE_SIZE = 24

// Paginated recipe list as an infinite query keyed on the active filters.
export function useRecipes(
  filters,
  { enabled = true, pageSize = PAGE_SIZE, firstPageSize = pageSize, excludeIds = [] } = {},
) {
  return useInfiniteQuery({
    queryKey: ['recipes', filters, pageSize, firstPageSize, excludeIds],
    queryFn: ({ pageParam = { offset: 0, page: 1, pageSize: firstPageSize } }) =>
      fetchRecipes(filters, pageParam.page, pageParam.pageSize, {
        offset: pageParam.offset,
        excludeIds,
      }),
    initialPageParam: { offset: 0, page: 1, pageSize: firstPageSize },
    getNextPageParam: (lastPage) =>
      lastPage.has_more
        ? { offset: lastPage.next_offset, page: lastPage.page + 1, pageSize }
        : undefined,
    enabled,
  })
}

export function useRecipeSuggestions(
  filters,
  selections,
  { candidatePortions = 4, enabled = true, pageSize = PAGE_SIZE, firstPageSize = pageSize } = {},
) {
  return useInfiniteQuery({
    queryKey: [
      'planner-suggestions',
      filters,
      selections,
      candidatePortions,
      pageSize,
      firstPageSize,
    ],
    queryFn: ({ pageParam = { offset: 0, page: 1, pageSize: firstPageSize } }) =>
      fetchPlannerSuggestions({
        selections,
        filters,
        candidatePortions,
        page: pageParam.page,
        pageSize: pageParam.pageSize,
        offset: pageParam.offset,
      }),
    initialPageParam: { offset: 0, page: 1, pageSize: firstPageSize },
    getNextPageParam: (lastPage) =>
      lastPage.has_more
        ? { offset: lastPage.next_offset, page: lastPage.page + 1, pageSize }
        : undefined,
    enabled,
  })
}

export function usePlannerBasket(selections, packOverrides = {}, snapOverrides = {}) {
  return useQuery({
    queryKey: ['planner-basket', selections, packOverrides, snapOverrides],
    queryFn: () => fetchPlannerBasket(selections, packOverrides, snapOverrides),
    // A week's pack choice re-prices the same basket, so keep the previous
    // answer on screen while the new one lands instead of blanking the table.
    placeholderData: (previous) => previous,
  })
}

// Refreshing stock rewrites prices and availability in the catalogue, which
// re-covers every affected ingredient — so the basket has to be re-fetched, not
// just re-rendered.
export function useStockRefresh() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: refreshPlannerStock,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['planner-basket'] }),
  })
}

// Pinning a pack size changes what the whole week costs, since other recipes may
// share the ingredient. Only the basket is invalidated: the ranking barely moves
// for one pack, and re-scoring the library on every click was most of the delay.
export function usePackPreference() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: setPackPreference,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['planner-basket'] }),
  })
}

export function useRecipe(id) {
  return useQuery({
    queryKey: ['recipe', id],
    queryFn: () => fetchRecipe(id),
    enabled: id != null,
  })
}

export function useCookMap(id, modifier) {
  const qc = useQueryClient()
  const start = useQuery({
    queryKey: ['cook-map-start', id],
    queryFn: () => startCookMap(id),
    enabled: id != null,
    staleTime: 0,
    refetchOnMount: 'always',
  })
  const poll = useQuery({
    queryKey: ['cook-map', id, modifier],
    queryFn: () => fetchCookMap(id, modifier),
    enabled: id != null && start.isSuccess,
    refetchInterval: (query) => (
      query.state.data?.status === 'processing' ? 1000 : false
    ),
  })
  const retry = useMutation({
    mutationFn: () => retryCookMap(id),
    onSuccess: (data) => {
      qc.setQueryData(['cook-map-start', id], data)
      qc.setQueryData(['cook-map', id, modifier], data)
      qc.invalidateQueries({ queryKey: ['cook-map', id] })
    },
  })
  const current = (
    poll.isFetching && start.data?.status === 'processing'
      ? start.data
      : poll.data ?? start.data
  )
  return {
    data: current,
    status: current?.status ?? (start.isError ? 'failed' : 'processing'),
    error: retry.error?.message ?? current?.error ?? start.error?.message ?? poll.error?.message,
    retry: () => retry.mutateAsync(),
    retrying: retry.isPending,
  }
}

// The recipe rewritten for a protein modifier. Only asked for when there is a
// modifier to apply — an unmodified recipe is already on the page, and asking
// for it again would be a round trip to be told nothing changed.
export function useProteinPreview(id, modifier, { enabled = true } = {}) {
  return useQuery({
    queryKey: ['recipe', id, 'protein-preview', modifier],
    queryFn: () => fetchProteinPreview(id, modifier),
    enabled: enabled && id != null && Boolean(modifier),
    // Hold the last preview while the next one loads, so changing the target
    // does not flash the recipe back to how it is written. Dropped the moment
    // the modifier is cleared, or a reset would keep showing the swap.
    placeholderData: (previous) => (modifier ? previous : undefined),
  })
}

// Flags the macros and polls the audit job it starts. The recipe is refetched
// when the job finishes, so any correction shows up without a reload.
export function useAuditRecipe(id) {
  const qc = useQueryClient()
  const [jobId, setJobId] = useState(null)
  const [lastJob, setLastJob] = useState(null)

  const start = useMutation({
    mutationFn: () => flagRecipe(id),
    onSuccess: (job) => setJobId(job.job_id),
  })

  const { data: job } = useQuery({
    queryKey: ['recipe-audit-job', jobId],
    queryFn: () => fetchAuditJob(jobId),
    enabled: jobId != null,
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 1000 : false),
  })

  const finished = job != null && job.status !== 'running'
  useEffect(() => {
    if (!finished) return
    qc.invalidateQueries({ queryKey: ['recipe', id] })
    // Keep the outcome on screen after polling stops.
    setLastJob(job)
    setJobId(null)
  }, [finished, job, id, qc])

  const current = job ?? lastJob
  return {
    run: () => {
      setLastJob(null)
      start.mutate()
    },
    job: current,
    // Cover the gap between the POST resolving and the first poll.
    running: start.isPending || (jobId != null && current?.status !== 'failed'),
    error: start.error?.message ?? current?.error ?? null,
    dismiss: () => setLastJob(null),
  }
}

export function useRevertRecipeEdits(id) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => revertRecipeEdits(id),
    onSuccess: (data) => qc.setQueryData(['recipe', id], data),
  })
}

function useOptimisticRecipeFieldMutation(id, field, mutationFn) {
  const qc = useQueryClient()
  const recipeKey = ['recipe', id]

  return useMutation({
    mutationFn,
    onMutate: async (value) => {
      await qc.cancelQueries({ queryKey: recipeKey })
      const previous = qc.getQueryData(recipeKey)
      qc.setQueryData(recipeKey, (current) => (
        current ? { ...current, [field]: value } : current
      ))
      return { previous }
    },
    onError: (_error, value, context) => {
      qc.setQueryData(recipeKey, (current) => (
        current?.[field] === value ? context?.previous : current
      ))
    },
    onSuccess: (data, value) => {
      qc.setQueryData(recipeKey, (current) => (
        current?.[field] === value ? data : current
      ))
      qc.invalidateQueries({ queryKey: ['recipes'] })
      qc.invalidateQueries({ queryKey: ['planner-suggestions'] })
    },
  })
}

export function usePersonalRecipeRating(id) {
  return useOptimisticRecipeFieldMutation(
    id,
    'personal_rating',
    (rating) => setPersonalRecipeRating(id, rating),
  )
}

export function useRecipeWishlist(id) {
  return useOptimisticRecipeFieldMutation(
    id,
    'wishlisted',
    (wishlisted) => setRecipeWishlist(id, wishlisted),
  )
}

export function useHideRecipe(id) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => hideRecipe(id),
    onSuccess: () => {
      qc.removeQueries({ queryKey: ['recipe', id] })
      qc.invalidateQueries({ queryKey: ['recipes'] })
      qc.invalidateQueries({ queryKey: ['planner-suggestions'] })
      qc.invalidateQueries({ queryKey: ['facets'] })
      qc.invalidateQueries({ queryKey: ['planner-basket'] })
    },
  })
}

export function useFacets() {
  return useQuery({
    queryKey: ['facets'],
    queryFn: fetchFacets,
    staleTime: Infinity,
  })
}
