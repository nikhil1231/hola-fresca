import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { useEffect, useState } from 'react'

import {
  attachManualProduct,
  bulkApprove,
  deleteManualProduct,
  fetchAliases,
  fetchAliasOptions,
  fetchJob,
  fetchManualProducts,
  fetchMappingDetail,
  fetchMappingList,
  fetchMappingStats,
  resolveWithManualProduct,
  saveManualProduct,
  saveMappingDecision,
  searchMappingCandidates,
  setMappingAlias,
  startGenerate,
} from '../api/mappingClient.js'

export function useMappingList(status, options = {}) {
  const { page = 1, pageSize = 100, q = '' } = options
  return useQuery({
    queryKey: ['mapping-list', status ?? 'all', page, pageSize, q],
    queryFn: () => fetchMappingList(status, { page, pageSize, q }),
    // List-level counts feed the coverage summary. Keep the last response in
    // place while a new filter/search request is in flight so that summary and
    // the table do not briefly disappear between keystrokes.
    placeholderData: (previous) => previous,
  })
}

export function useMappingDetail(key) {
  return useQuery({
    queryKey: ['mapping-detail', key],
    queryFn: () => fetchMappingDetail(key),
    enabled: key != null,
  })
}

export function useSaveDecision(key) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body) => saveMappingDecision(key, body),
    onSuccess: (data) => {
      qc.setQueryData(['mapping-detail', key], data)
      qc.invalidateQueries({ queryKey: ['mapping-list'] })
      qc.invalidateQueries({ queryKey: ['mapping-stats'] })
      qc.invalidateQueries({ queryKey: ['mapping-alias-options'] })
    },
  })
}

export function useSearchCandidates(key) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (term) => searchMappingCandidates(key, term),
    onSuccess: (data) => {
      qc.setQueryData(['mapping-detail', key], data)
      qc.invalidateQueries({ queryKey: ['mapping-list'] })
      qc.invalidateQueries({ queryKey: ['mapping-stats'] })
    },
  })
}

export function useMappingStats() {
  return useQuery({ queryKey: ['mapping-stats'], queryFn: fetchMappingStats })
}

export function useAliases() {
  return useQuery({ queryKey: ['mapping-aliases'], queryFn: fetchAliases })
}

export function useAliasOptions(exclude) {
  return useQuery({
    queryKey: ['mapping-alias-options', exclude ?? 'none'],
    queryFn: () => fetchAliasOptions({ exclude, limit: 1000 }),
    enabled: exclude != null,
  })
}

export function useSetAlias(key) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (aliasOf) => setMappingAlias(key, aliasOf),
    onSuccess: (data) => {
      qc.setQueryData(['mapping-detail', key], data)
      qc.invalidateQueries({ queryKey: ['mapping-list'] })
      qc.invalidateQueries({ queryKey: ['mapping-aliases'] })
      qc.invalidateQueries({ queryKey: ['mapping-alias-options'] })
      qc.invalidateQueries({ queryKey: ['mapping-stats'] })
    },
  })
}

// Starts a generate job and polls it to completion. Exposes a single `running`
// flag plus the job payload so the button can show live progress.
export function useGenerateMappings() {
  const qc = useQueryClient()
  const [jobId, setJobId] = useState(null)

  const start = useMutation({
    mutationFn: (count) => startGenerate(count),
    onSuccess: (job) => setJobId(job.job_id),
  })

  const { data: job } = useQuery({
    queryKey: ['mapping-job', jobId],
    queryFn: () => fetchJob(jobId),
    enabled: jobId != null,
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 1500 : false),
  })

  const finished = job != null && job.status !== 'running'
  const [lastJob, setLastJob] = useState(null)
  useEffect(() => {
    if (!finished) return
    qc.invalidateQueries({ queryKey: ['mapping-list'] })
    qc.invalidateQueries({ queryKey: ['mapping-stats'] })
    qc.invalidateQueries({ queryKey: ['mapping-alias-options'] })
    // Hold on to the finished job so its outcome (including a failure) stays on
    // screen; clearing jobId only stops the polling.
    setLastJob(job)
    setJobId(null)
  }, [finished, job, qc])

  const current = job ?? lastJob
  return {
    start: (count) => {
      setLastJob(null)
      start.mutate(count)
    },
    job: current,
    // Treat the gap between the POST resolving and the first poll as running too,
    // otherwise the button flickers back to idle mid-job.
    running: start.isPending || (jobId != null && current?.status !== 'failed'),
    error: start.error?.message ?? current?.error ?? null,
  }
}

export function useManualProducts() {
  return useQuery({ queryKey: ['manual-products'], queryFn: fetchManualProducts })
}

export function useSaveManualProduct() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body) => saveManualProduct(body),
    onSuccess: (data) => {
      qc.setQueryData(['manual-products'], data)
      // A price or pack-size change moves every basket that uses it.
      qc.invalidateQueries({ queryKey: ['mapping-detail'] })
    },
  })
}

export function useDeleteManualProduct() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sku) => deleteManualProduct(sku),
    onSuccess: (data) => qc.setQueryData(['manual-products'], data),
  })
}

// "This shop doesn't sell it": records what you buy instead and approves it.
export function useResolveWithManualProduct(key) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body) => resolveWithManualProduct(key, body),
    onSuccess: (data) => {
      qc.setQueryData(['mapping-detail', key], data)
      qc.invalidateQueries({ queryKey: ['manual-products'] })
      qc.invalidateQueries({ queryKey: ['mapping-list'] })
      qc.invalidateQueries({ queryKey: ['mapping-stats'] })
    },
  })
}

export function useAttachManualProduct(key) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (sku) => attachManualProduct(key, sku),
    onSuccess: (data) => {
      qc.setQueryData(['mapping-detail', key], data)
      qc.invalidateQueries({ queryKey: ['manual-products'] })
    },
  })
}

export function useBulkApprove() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (keys) => bulkApprove(keys),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['mapping-list'] })
      qc.invalidateQueries({ queryKey: ['mapping-stats'] })
    },
  })
}
