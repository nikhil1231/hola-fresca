import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { useEffect, useState } from 'react'

import {
  bulkApprove,
  fetchAliases,
  fetchJob,
  fetchMappingDetail,
  fetchMappingList,
  saveMappingDecision,
  searchMappingCandidates,
  setMappingAlias,
  startGenerate,
} from '../api/mappingClient.js'

export function useMappingList(status) {
  return useQuery({
    queryKey: ['mapping-list', status ?? 'all'],
    queryFn: () => fetchMappingList(status),
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
    },
  })
}

export function useSearchCandidates(key) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (term) => searchMappingCandidates(key, term),
    onSuccess: (data) => {
      qc.setQueryData(['mapping-detail', key], data)
    },
  })
}

export function useAliases() {
  return useQuery({ queryKey: ['mapping-aliases'], queryFn: fetchAliases })
}

export function useSetAlias(key) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (aliasOf) => setMappingAlias(key, aliasOf),
    onSuccess: (data) => {
      qc.setQueryData(['mapping-detail', key], data)
      qc.invalidateQueries({ queryKey: ['mapping-list'] })
      qc.invalidateQueries({ queryKey: ['mapping-aliases'] })
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
  useEffect(() => {
    if (!finished) return
    qc.invalidateQueries({ queryKey: ['mapping-list'] })
    setJobId(null)
  }, [finished, qc])

  return {
    start: (count) => start.mutate(count),
    job,
    running: start.isPending || (jobId != null && job?.status === 'running'),
    error: start.error,
  }
}

export function useBulkApprove() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (keys) => bulkApprove(keys),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['mapping-list'] }),
  })
}
