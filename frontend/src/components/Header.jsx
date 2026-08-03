import { Link, NavLink, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { Box, Container, Group, TextInput, Title } from '@mantine/core'
import { IconSearch } from '@tabler/icons-react'

import { useDebouncedSearch } from '../hooks/useDebouncedSearch.js'
import classes from './Header.module.css'

// Debounced search box that writes ?q= and lands on the browse page.
function SearchBox() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const q = searchParams.get('q') ?? ''
  const [value, setValue] = useDebouncedSearch(q, (nextValue) => {
    const next = new URLSearchParams(searchParams)
    if (nextValue) next.set('q', nextValue)
    else next.delete('q')
    next.delete('page')
    navigate({ pathname: '/', search: next.toString() })
  })

  return (
    <TextInput
      value={value}
      onChange={(e) => setValue(e.currentTarget.value)}
      placeholder="Search recipes"
      leftSection={<IconSearch size={16} />}
      radius="xl"
      w={{ base: 140, sm: 280 }}
      aria-label="Search recipes"
    />
  )
}

export default function Header() {
  const { pathname } = useLocation()

  return (
    <Box className={classes.header}>
      <Container size="xl" h="100%">
        <Group h="100%" justify="space-between" wrap="nowrap">
          <Group gap="xl" wrap="nowrap">
            <Title order={3} component={Link} to="/" className={classes.logo}>
              Hola<span className={classes.logoAccent}>Fresca</span>
            </Title>
            <Group gap="lg" visibleFrom="xs" wrap="nowrap">
              <NavLink to="/" end className={navClass}>
                Browse
              </NavLink>
              <NavLink to="/dashboard" className={navClass}>
                Week
              </NavLink>
              <NavLink to="/cuisines" className={navClass}>
                Cuisines
              </NavLink>
              <NavLink to="/mapping" className={navClass}>
                Mapping
              </NavLink>
              <NavLink to="/ocado" className={navClass}>
                Ocado
              </NavLink>
            </Group>
          </Group>
          {pathname === '/' && <SearchBox />}
        </Group>
      </Container>
    </Box>
  )
}

function navClass({ isActive }) {
  return isActive ? `${classes.navLink} ${classes.navLinkActive}` : classes.navLink
}
