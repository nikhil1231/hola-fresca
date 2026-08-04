import { Outlet, useLocation } from 'react-router-dom'
import { AppShell, Container, Text } from '@mantine/core'

import Header from './components/Header.jsx'

const PAGE_LAST_UPDATED =
  typeof __PAGE_LAST_UPDATED__ === 'undefined' ? {} : __PAGE_LAST_UPDATED__

function pageMetaForPath(pathname) {
  if (pathname === '/') return PAGE_LAST_UPDATED.home
  if (pathname === '/settings') return PAGE_LAST_UPDATED.settings
  if (pathname === '/basket') return PAGE_LAST_UPDATED.basket
  if (pathname === '/cuisines') return PAGE_LAST_UPDATED.cuisines
  if (pathname === '/mapping/aliases') return PAGE_LAST_UPDATED.mappingAliases
  if (pathname === '/mapping/manual') return PAGE_LAST_UPDATED.mappingManual
  // Must stay below the exact sub-page paths above, which it would swallow.
  if (pathname.startsWith('/mapping/')) return PAGE_LAST_UPDATED.mappingReview
  if (pathname === '/mapping') return PAGE_LAST_UPDATED.mapping
  if (pathname.startsWith('/recipes/')) return PAGE_LAST_UPDATED.recipeDetail
  return PAGE_LAST_UPDATED.browse
}

function formatUpdatedAt(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null

  return new Intl.DateTimeFormat('en-GB', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

function LastUpdatedFooter() {
  const { pathname } = useLocation()
  const meta = pageMetaForPath(pathname)
  const updatedAt = formatUpdatedAt(meta?.committedAt)

  if (!updatedAt) return null

  return (
    <Text
      component="footer"
      c="dimmed"
      size="xs"
      ta="center"
      mt="xl"
      pt="md"
      visibleFrom="sm"
    >
      last updated at {updatedAt}
    </Text>
  )
}

function App() {
  return (
    <AppShell header={{ height: 64 }} padding={0}>
      <AppShell.Header>
        <Header />
      </AppShell.Header>
      <AppShell.Main>
        <Container size="xl" px={{ base: 24, sm: 'xl' }} py={{ base: 'lg', sm: 'xl' }}>
          <Outlet />
          <LastUpdatedFooter />
        </Container>
      </AppShell.Main>
    </AppShell>
  )
}

export default App
