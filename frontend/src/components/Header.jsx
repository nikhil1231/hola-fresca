import { useEffect, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  ActionIcon,
  Box,
  Container,
  Drawer,
  Group,
  Stack,
  TextInput,
  Title,
  Tooltip,
} from '@mantine/core'
import { IconMenu2, IconSearch, IconSettings, IconX } from '@tabler/icons-react'

import { useDebouncedSearch } from '../hooks/useDebouncedSearch.js'
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
  const [menuOpen, setMenuOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)

  useEffect(() => {
    setMenuOpen(false)
    setSearchOpen(false)
  }, [pathname])

  return (
    <Box className={classes.header}>
      <Container size="xl" h="100%">
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
              <NavLink to="/cuisines" className={navClass}>
                Cuisines
              </NavLink>
              <NavLink to="/mapping" className={navClass}>
                Mapping
              </NavLink>
            </Group>
          </Group>
          <Group gap="sm" wrap="nowrap">
            {pathname === '/browse' && <SearchBox />}
            <Tooltip label="Settings" withArrow>
              <ActionIcon
                component={NavLink}
                to="/settings"
                variant="subtle"
                color="gray"
                size="lg"
                aria-label="Settings"
              >
                <IconSettings size={20} />
              </ActionIcon>
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
      </Container>

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
          <NavLink to="/cuisines" className={mobileNavClass} onClick={() => setMenuOpen(false)}>
            Cuisines
          </NavLink>
          <NavLink to="/settings" className={mobileNavClass} onClick={() => setMenuOpen(false)}>
            Settings
          </NavLink>
        </Stack>
      </Drawer>
    </Box>
  )
}

function navClass({ isActive }) {
  return isActive ? `${classes.navLink} ${classes.navLinkActive}` : classes.navLink
}

function mobileNavClass({ isActive }) {
  return isActive
    ? `${classes.mobileNavLink} ${classes.mobileNavLinkActive}`
    : classes.mobileNavLink
}
