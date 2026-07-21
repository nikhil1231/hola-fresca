import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MantineProvider } from '@mantine/core'

import '@mantine/core/styles.css'
import './index.css'

import { theme } from './theme.js'
import App from './App.jsx'
import BrowsePage from './routes/BrowsePage.jsx'
import CuisinesPage from './routes/CuisinesPage.jsx'
import RecipeDetailPage from './routes/RecipeDetailPage.jsx'
import MappingPage from './routes/MappingPage.jsx'
import MappingReviewPage from './routes/MappingReviewPage.jsx'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 60_000, refetchOnWindowFocus: false, retry: 1 },
  },
})

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="light">
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <Routes>
            <Route element={<App />}>
              <Route index element={<BrowsePage />} />
              <Route path="cuisines" element={<CuisinesPage />} />
              <Route path="recipes/:id" element={<RecipeDetailPage />} />
              <Route path="mapping" element={<MappingPage />} />
              <Route path="mapping/:key" element={<MappingReviewPage />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
)
