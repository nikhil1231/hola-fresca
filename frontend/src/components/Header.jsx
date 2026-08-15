import { useEffect, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Avatar,
  Box,
  Drawer,
  Group,
  Stack,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core'
import { IconMenu2, IconSearch, IconX } from '@tabler/icons-react'

import { useDebouncedSearch } from '../hooks/useDebouncedSearch.js'
import { useAccount } from '../hooks/useAccount.js'
import { accountInitials } from '../utils/account.js'
import AppContainer from './AppContainer.jsx'
import classes from './Header.module.css'

// Debounced search box that writes ?q= and lands on the browse page. It carries
// the current query string along, so searching from a week you are editing keeps
// editing that week.
function SearchBox({ autoFocus = false, fluid = false, onEscape }) {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const q = searchParams.get('q') ?? ''
  const [value, setValue] = useDebouncedSearch(q, (nextValue) => {
    const next = new URLSearchParams(searchParams)
    if (nextValue) next.set('q', nextValue)
    else next.delete('q')
    next.delete('page')
    navigate({ pathname: '/browse', search: next.toString() })
  })

  return (
    <TextInput
      value={value}
      onChange={(e) => setValue(e.currentTarget.value)}
      placeholder="Search recipes"
      leftSection={<IconSearch size={16} />}
      radius="xl"
      w={fluid ? '100%' : 280}
      autoFocus={autoFocus}
      aria-label="Search recipes"
      onKeyDown={(event) => {
        if (event.key === 'Escape') onEscape?.()
      }}
    />
  )
}

export default function Header() {
  const { pathname } = useLocation()
  const { data: account } = useAccount()
  const [menuOpen, setMenuOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)

  useEffect(() => {
    setMenuOpen(false)
    setSearchOpen(false)
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
              <NavLink to="/mapping" className={navClass}>
                Mapping
              </NavLink>
            </Group>
          </Group>
          <Group gap="sm" wrap="nowrap">
            {pathname === '/browse' && <SearchBox />}
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
          {searchOpen && pathname === '/browse' ? (
            <Group gap="xs" wrap="nowrap" w="100%">
              <Box style={{ flex: 1, minWidth: 0 }}>
                <SearchBox autoFocus fluid onEscape={() => setSearchOpen(false)} />
              </Box>
              <ActionIcon
                variant="subtle"
                color="gray"
                size={44}
                aria-label="Close recipe search"
                onClick={() => setSearchOpen(false)}
              >
                <IconX size={22} />
              </ActionIcon>
            </Group>
          ) : (
            <>
              <Title order={3} component={Link} to="/" className={classes.logo}>
                Hola<span className={classes.logoAccent}>Fresca</span>
              </Title>
              <Group gap={4} wrap="nowrap">
                {pathname === '/browse' && (
                  <ActionIcon
                    variant="subtle"
                    color="gray"
                    size={44}
                    aria-label="Search recipes"
                    onClick={() => setSearchOpen(true)}
                  >
                    <IconSearch size={22} />
                  </ActionIcon>
                )}
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
            </>
          )}
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
          <NavLink to="/mapping" className={mobileNavClass} onClick={() => setMenuOpen(false)}>
            Mapping
          </NavLink>
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
