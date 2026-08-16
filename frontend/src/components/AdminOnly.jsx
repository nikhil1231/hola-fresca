import { Navigate } from 'react-router-dom'
import { Center, Loader } from '@mantine/core'

import { useCanEditCatalogue } from '../hooks/useAccount.js'

/** Gate for the pages that edit the shared catalogue.
 *
 * Presentation only, and worth being explicit that it is: the catalogue
 * endpoints are gated server-side by `require_admin`, and nothing here is what
 * stops a second user changing what everybody's basket buys. What this stops is
 * the app *offering* them a page whose every action answers 403.
 *
 * Three states, not two — see `useCanEditCatalogue`. While the account is
 * loading this renders a spinner rather than the page or the redirect, because
 * guessing either way is worse: showing the page flashes it at somebody who may
 * not be allowed it, and redirecting bounces the owner off their own Mapping tab
 * on every cold load. When the account could not be *fetched* it renders the
 * page: the page's own queries will fail too and say so, which is a far better
 * account of a backend that is down than a silent redirect to the home page.
 */
export default function AdminOnly({ children }) {
  const { allowed, known } = useCanEditCatalogue()

  if (!allowed && !known) {
    return (
      <Center py="xl">
        <Loader color="fresh" />
      </Center>
    )
  }
  if (!allowed) return <Navigate to="/" replace />
  return children
}
