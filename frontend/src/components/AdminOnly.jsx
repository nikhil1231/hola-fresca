import { Navigate } from 'react-router-dom'
import { Center, Loader } from '@mantine/core'

import { useAccount } from '../hooks/useAccount.js'

/** Gate for the pages that edit the shared catalogue.
 *
 * Presentation only, and worth being explicit that it is: the catalogue
 * endpoints are gated server-side by `require_admin`, and nothing here is what
 * stops a second user changing what everybody's basket buys. What this stops is
 * the app *offering* them a page whose every action answers 403.
 *
 * While the account is loading it renders a spinner rather than the page or the
 * redirect. Guessing either way is worse: showing the page flashes it at
 * somebody who may not be allowed it, and redirecting bounces the owner off
 * their own Mapping tab on every cold load.
 */
export default function AdminOnly({ children }) {
  const { data: account, isPending } = useAccount()

  if (isPending) {
    return (
      <Center py="xl">
        <Loader color="fresh" />
      </Center>
    )
  }
  if (!account?.is_admin) return <Navigate to="/" replace />
  return children
}
