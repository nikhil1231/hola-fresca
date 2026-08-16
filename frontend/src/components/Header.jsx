import { useEffect, useState } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import {
  ActionIcon,
  Avatar,
  Box,
  Drawer,
  Group,
  Stack,
  Title,
  Tooltip,
} from '@mantine/core'
import { IconMenu2 } from '@tabler/icons-react'

import { useAccount } from '../hooks/useAccount.js'
import { accountInitials } from '../utils/account.js'
import AppContainer from './AppContainer.jsx'
import RetailerChip from './RetailerChip.jsx'
import classes from './Header.module.css'

export default function Header() {
  const { pathname } = useLocation()
  const { data: account } = useAccount()
  const [menuOpen, setMenuOpen] = useState(false)

  useEffect(() => {
    setMenuOpen(false)
  }, [pathname])

  return (
    <Box className={classes.header}>
      <AppContainer h="100%">
        <Group h="100%" justify="space-between" wrap="nowrap" visibleFrom="md">
          <Group gap="xl" wrap="nowrap">
            <Title order={3} component={Link} to="/" className={classes.logo}>
              Hola<span className={classes.logoAccent}>Fresca</span>
            </Title>
            <Group gap="lg" wrap="nowrap">
              <NavLink to="/" end className={navClass}>
                Home
              </NavLink>
              <NavLink to="/browse" className={navClass}>
                Browse
              </NavLink>
              <NavLink to="/basket" className={navClass}>
                Basket
              </NavLink>
              {/* Catalogue editing is admin-only, so a second user is not
                  offered a tab whose every action answers 403. */}
              {account?.is_admin && (
                <NavLink to="/mapping" className={navClass}>
                  Mapping
                </NavLink>
              )}
            </Group>
          </Group>
          <Group gap="sm" wrap="nowrap">
            <RetailerChip />
            <Tooltip label={accountLabel(account)} withArrow>
              <Avatar
                component={Link}
                to="/settings"
                color="fresh"
                variant={pathname === '/settings' ? 'filled' : 'light'}
                size={38}
                className={classes.accountAvatar}
                aria-label={accountLabel(account)}
              >
                {accountInitials(account)}
              </Avatar>
            </Tooltip>
          </Group>
        </Group>

        <Group h="100%" justify="space-between" wrap="nowrap" hiddenFrom="md">
          <Title order={3} component={Link} to="/" className={classes.logo}>
            Hola<span className={classes.logoAccent}>Fresca</span>
          </Title>
          <Group gap={4} wrap="nowrap">
            <RetailerChip size="sm" />
            <Avatar
              component={Link}
              to="/settings"
              color="fresh"
              variant={pathname === '/settings' ? 'filled' : 'light'}
              size={36}
              className={classes.accountAvatar}
              aria-label={accountLabel(account)}
            >
              {accountInitials(account)}
            </Avatar>
            <ActionIcon
              variant="subtle"
              color="gray"
              size={44}
              aria-label="Open navigation menu"
              aria-expanded={menuOpen}
              onClick={() => setMenuOpen(true)}
            >
              <IconMenu2 size={24} />
            </ActionIcon>
          </Group>
        </Group>
      </AppContainer>

      <Drawer
        opened={menuOpen}
        onClose={() => setMenuOpen(false)}
        position="right"
        size="min(85vw, 320px)"
        title="Navigate"
        padding="lg"
      >
        <Stack component="nav" gap="xs" aria-label="Mobile navigation">
          <NavLink to="/" end className={mobileNavClass} onClick={() => setMenuOpen(false)}>
            Home
          </NavLink>
          <NavLink to="/browse" className={mobileNavClass} onClick={() => setMenuOpen(false)}>
            Browse
          </NavLink>
          <NavLink to="/basket" className={mobileNavClass} onClick={() => setMenuOpen(false)}>
            Basket
          </NavLink>
          {account?.is_admin && (
            <NavLink to="/mapping" className={mobileNavClass} onClick={() => setMenuOpen(false)}>
              Mapping
            </NavLink>
          )}
          <NavLink to="/settings" className={mobileNavClass} onClick={() => setMenuOpen(false)}>
            Settings
          </NavLink>
        </Stack>
      </Drawer>
    </Box>
  )
}

function accountLabel(account) {
  if (account?.name) return `Account settings for ${account.name}`
  if (account?.email) return `Account settings for ${account.email}`
  return 'Account settings'
}

function navClass({ isActive }) {
  return isActive ? `${classes.navLink} ${classes.navLinkActive}` : classes.navLink
}

function mobileNavClass({ isActive }) {
  return isActive
    ? `${classes.mobileNavLink} ${classes.mobileNavLinkActive}`
    : classes.mobileNavLink
}
