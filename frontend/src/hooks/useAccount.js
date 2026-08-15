import { useQuery } from '@tanstack/react-query'

import { fetchAccount } from '../api/accountClient.js'

export const ACCOUNT_KEY = ['account']

export function useAccount() {
  return useQuery({
    queryKey: ACCOUNT_KEY,
    queryFn: fetchAccount,
    staleTime: Infinity,
  })
}
