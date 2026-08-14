import { Badge, Tooltip } from '@mantine/core'

import { useActiveRetailer } from '../hooks/useRetailer.js'

/** Which shop the page you are looking at is about.
 *
 *  Every price, pack size and product name on the basket and mapping pages is
 *  the active retailer's, and nothing else on those pages says so — two
 *  catalogues disagreeing about what a chicken breast costs is exactly the kind
 *  of difference you would otherwise put down to a stale cache. The chip is the
 *  answer to "priced where?".
 *
 *  Renders nothing until the retailer is known, rather than defaulting: naming
 *  the wrong shop, even for a moment, is worse than naming none. */
export default function RetailerChip({ size = 'sm' }) {
  const { label, shoppable } = useActiveRetailer()
  if (!label) return null

  const chip = (
    <Badge size={size} variant="light" color={shoppable ? 'teal' : 'gray'}>
      {label}
    </Badge>
  )

  // A shop with no cart integration can be planned and priced but not pushed to,
  // and that is worth saying where the difference shows up rather than leaving
  // someone to hunt for a missing button.
  return shoppable ? (
    chip
  ) : (
    <Tooltip label="Priced here, but the shop itself is done by hand — no cart to push to" withArrow>
      {chip}
    </Tooltip>
  )
}
