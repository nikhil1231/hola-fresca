import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Outlet, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MantineProvider } from '@mantine/core'

import '@mantine/core/styles.css'
import './index.css'

import { theme } from './theme.js'
import { installAccessSessionHandling } from './api/session.js'
import AdminOnly from './components/AdminOnly.jsx'
import App from './App.jsx'
import BasketPage from './routes/BasketPage.jsx'
import BrowsePage from './routes/BrowsePage.jsx'
import CookingPage from './routes/CookingPage.jsx'
import HomePage from './routes/HomePage.jsx'
import RecipeDetailPage from './routes/RecipeDetailPage.jsx'
import SettingsPage from './routes/SettingsPage.jsx'
import MappingAliasesPage from './routes/MappingAliasesPage.jsx'
import MappingManualPage from './routes/MappingManualPage.jsx'
import MappingPage from './routes/MappingPage.jsx'
import MappingReviewPage from './routes/MappingReviewPage.jsx'
import PastRecipesPage from './routes/PastRecipesPage.jsx'

// Before anything can issue a request, so no API call escapes unwrapped.
installAccessSessionHandling()

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 60_000, refetchOnWindowFocus: false, retry: 1 },
  },
})

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="dark">
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Routes>
            <Route path="recipes/:id/cook" element={<CookingPage />} />
            <Route element={<App />}>
              <Route index element={<HomePage />} />
              <Route path="browse" element={<BrowsePage />} />
              <Route path="basket" element={<BasketPage />} />
              <Route path="past" element={<PastRecipesPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="recipes/:id" element={<RecipeDetailPage />} />
              {/* The catalogue is shared, so editing it is not a personal act:
                  one person rejecting a mapping changes what every other user's
                  basket buys. The endpoints behind these are admin-gated. */}
              <Route element={<AdminOnly><Outlet /></AdminOnly>}>
                <Route path="mapping" element={<MappingPage />} />
                <Route path="mapping/aliases" element={<MappingAliasesPage />} />
                <Route path="mapping/manual" element={<MappingManualPage />} />
                <Route path="mapping/:key" element={<MappingReviewPage />} />
              </Route>
            </Route>
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
)
