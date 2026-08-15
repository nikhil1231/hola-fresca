import { Badge, Tooltip } from '@mantine/core'
import { Link } from 'react-router-dom'

import { useActiveRetailer } from '../hooks/useRetailer.js'

const RETAILER_COLORS = {
  ocado: 'grape',
  sainsburys: 'orange',
}

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
export default function RetailerChip({ size = 'md' }) {
  const { id, label, shoppable } = useActiveRetailer()
  if (!label) return null

  const chip = (
    <Badge
      component={Link}
      to="/settings"
      size={size}
      variant="light"
      color={RETAILER_COLORS[id] ?? 'gray'}
      aria-label={`${label} selected; change retailer in Settings`}
      style={{ cursor: 'pointer', textDecoration: 'none' }}
    >
      {label}
    </Badge>
  )

  // A shop with no cart integration can be planned and priced but not pushed to,
  // and that is worth saying where the difference shows up rather than leaving
  // someone to hunt for a missing button.
  return (
    <Tooltip
      label={shoppable
        ? 'Change retailer in Settings'
        : 'Priced here, but the shop itself is done by hand — no cart to push to'}
      withArrow
    >
      {chip}
    </Tooltip>
  )
}
