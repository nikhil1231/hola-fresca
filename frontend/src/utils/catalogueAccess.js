/** Whether to offer the catalogue tools, from the account query's three states.
 *
 * A plain function rather than part of the hook so it can be exercised without
 * a React renderer — the branch that matters most is the one that only happens
 * when the backend is down, which is exactly the state that is awkward to stage
 * in a browser.
 *
 * The distinction it exists for: "not an admin" and "could not ask" are not the
 * same answer. Treating the second as the first makes the Mapping tab quietly
 * disappear whenever the API is unreachable, which reads as having lost access
 * rather than as a backend that is not running. That is the common case in local
 * development, and it is what sent somebody looking for a bug in the wrong place.
 *
 * So `allowed` is only false when the server actually said so. The server is
 * what refuses the writes (`require_admin` on every mapping endpoint), so
 * guessing generously here costs at worst a tab whose page then shows its own
 * errors, while guessing meanly costs the owner their tools with no explanation.
 *
 * `known` is kept separate so a caller can tell "hold on, we are still asking"
 * from a settled answer, and show a spinner rather than flashing a page at
 * somebody who may not be allowed it.
 */
export function catalogueAccess({ isPending, isError, isAdmin }) {
  if (isPending) return { allowed: false, known: false }
  if (isError) return { allowed: true, known: false }
  return { allowed: Boolean(isAdmin), known: true }
}
