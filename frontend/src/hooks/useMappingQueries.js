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
  const [lastJob, setLastJob] = useState(null)
  useEffect(() => {
    if (!finished) return
    qc.invalidateQueries({ queryKey: ['mapping-list'] })
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

export function useBulkApprove() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (keys) => bulkApprove(keys),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['mapping-list'] }),
  })
}
