import { useEffect, useState } from 'react'
import { Link, NavLink, useNavigate, useSearchParams } from 'react-router-dom'
import { Box, Container, Group, TextInput, Title } from '@mantine/core'
import { IconSearch } from '@tabler/icons-react'

import classes from './Header.module.css'

// Debounced search box that writes ?q= and lands on the browse page.
function SearchBox() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [value, setValue] = useState(searchParams.get('q') ?? '')

  // Keep the box in sync when the URL changes elsewhere (e.g. Clear all).
  useEffect(() => {
    setValue(searchParams.get('q') ?? '')
  }, [searchParams])

  useEffect(() => {
    const handle = setTimeout(() => {
      const current = searchParams.get('q') ?? ''
      if (value === current) return
      const next = new URLSearchParams(searchParams)
      if (value) next.set('q', value)
      else next.delete('q')
      next.delete('page')
      navigate({ pathname: '/', search: next.toString() })
    }, 300)
    return () => clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

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
              <NavLink to="/cuisines" className={navClass}>
                Cuisines
              </NavLink>
              <NavLink to="/mapping" className={navClass}>
                Mapping
              </NavLink>
            </Group>
          </Group>
          <SearchBox />
        </Group>
      </Container>
    </Box>
  )
}

function navClass({ isActive }) {
  return isActive ? `${classes.navLink} ${classes.navLinkActive}` : classes.navLink
}
