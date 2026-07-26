import { useEffect, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  fetchAuditJob,
  fetchFacets,
  fetchPlannerBasket,
  fetchPlannerSuggestions,
  fetchRecipe,
  fetchRecipes,
  flagRecipe,
  revertRecipeEdits,
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

export function usePlannerBasket(selections) {
  return useQuery({
    queryKey: ['planner-basket', selections],
    queryFn: () => fetchPlannerBasket(selections),
  })
}

export function useRecipe(id) {
  return useQuery({
    queryKey: ['recipe', id],
    queryFn: () => fetchRecipe(id),
    enabled: id != null,
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

export function useFacets() {
  return useQuery({
    queryKey: ['facets'],
    queryFn: fetchFacets,
    staleTime: Infinity,
  })
}
