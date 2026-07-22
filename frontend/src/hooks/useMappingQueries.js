import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  bulkApprove,
  fetchMappingDetail,
  fetchMappingList,
  saveMappingDecision,
  searchMappingCandidates,
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

export function useBulkApprove() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (keys) => bulkApprove(keys),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['mapping-list'] }),
  })
}
