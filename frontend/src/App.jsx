import { Outlet, useLocation } from 'react-router-dom'
import { AppShell, Container, Text } from '@mantine/core'
import { useMediaQuery } from '@mantine/hooks'

import Header from './components/Header.jsx'
import AppContainer from './components/AppContainer.jsx'
import { useLocalPlanImport } from './hooks/useLocalPlanImport.js'

const PAGE_LAST_UPDATED =
  typeof __PAGE_LAST_UPDATED__ === 'undefined' ? {} : __PAGE_LAST_UPDATED__

function pageMetaForPath(pathname) {
  if (pathname === '/') return PAGE_LAST_UPDATED.home
  if (pathname === '/settings') return PAGE_LAST_UPDATED.settings
  if (pathname === '/basket') return PAGE_LAST_UPDATED.basket
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
  useLocalPlanImport()
  const { pathname } = useLocation()
  const compactRecipeLayout = useMediaQuery('(max-width: 59.99em)')
  const mobileBasketLayout = useMediaQuery('(max-width: 48em)')
  const recipeSegments = pathname.split('/').filter(Boolean)
  const recipeDetail = recipeSegments[0] === 'recipes' && recipeSegments.length === 2
  const cooking = recipeSegments[0] === 'recipes' && recipeSegments[2] === 'cook'
  const immersive =
    cooking ||
    (pathname === '/basket' && mobileBasketLayout) ||
    (recipeDetail && compactRecipeLayout)
  return (
    <AppShell header={immersive ? undefined : { height: 64 }} padding={0}>
      {!immersive && (
        <AppShell.Header>
          <Header />
        </AppShell.Header>
      )}
      <AppShell.Main>
        {immersive ? (
          <Container fluid px={0} py={0}>
            <Outlet />
          </Container>
        ) : (
          <AppContainer py={{ base: 'lg', sm: 'xl' }}>
            <Outlet />
            <LastUpdatedFooter />
          </AppContainer>
        )}
      </AppShell.Main>
    </AppShell>
  )
}

export default App
