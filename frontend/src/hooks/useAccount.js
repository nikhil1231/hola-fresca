import { useQuery } from '@tanstack/react-query'

import { fetchAccount } from '../api/accountClient.js'
import { catalogueAccess } from '../utils/catalogueAccess.js'

export const ACCOUNT_KEY = ['account']

export function useAccount() {
  return useQuery({
    queryKey: ACCOUNT_KEY,
    queryFn: fetchAccount,
    staleTime: Infinity,
  })
}

/** Whether to offer the catalogue tools: true, false, or *not yet known*.
 *
 * The rule and the reasoning for it live in `catalogueAccess`, which is a plain
 * function so that the branch that only happens when the backend is down can be
 * exercised without staging a broken backend in a browser.
 */
export function useCanEditCatalogue() {
  const { data, isPending, isError } = useAccount()
  return catalogueAccess({ isPending, isError, isAdmin: data?.is_admin })
}
