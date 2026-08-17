import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const frontendRoot = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(frontendRoot, '..')

// Keep the development proxy aligned with run.py's default backend port.
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8101'

const sharedPageFiles = [
  'frontend/src/App.jsx',
  'frontend/src/components/Header.jsx',
  'frontend/src/components/Header.module.css',
  'frontend/src/index.css',
  'frontend/src/main.jsx',
  'frontend/src/theme.js',
  'frontend/package.json',
  'frontend/vite.config.js',
]

const pageFiles = {
  home: [
    'frontend/src/routes/HomePage.jsx',
    'frontend/src/routes/HomePage.module.css',
    'frontend/src/hooks/useSchedule.js',
    'frontend/src/hooks/useWeeklyPlan.js',
    // The cooked ticks live on the week rows here, so what decides whether a
    // recipe reads as cooked belongs to this page's history too.
    'frontend/src/hooks/usePastRecipes.js',
    'frontend/src/api/planClient.js',
    'frontend/src/api/scheduleClient.js',
    'app/api/plan.py',
    'app/api/schedule.py',
    'app/pantry/cooks.py',
    'app/schedule.py',
  ],
  settings: [
    'frontend/src/routes/SettingsPage.jsx',
    'frontend/src/hooks/useSchedule.js',
    'frontend/src/api/scheduleClient.js',
    'app/api/schedule.py',
    'app/schedule.py',
  ],
  browse: [
    'frontend/src/routes/BrowsePage.jsx',
    'frontend/src/hooks/useSchedule.js',
    'frontend/src/components/FilterPanel.jsx',
    'frontend/src/components/RangeFilter.jsx',
    'frontend/src/components/RecipeCard.jsx',
    'frontend/src/components/RecipeCard.module.css',
    'frontend/src/data/defaultFacets.js',
    'frontend/src/hooks/useFilters.js',
    'frontend/src/hooks/useRecipeQueries.js',
    'frontend/src/api/client.js',
  ],
  basket: [
    'frontend/src/routes/BasketPage.jsx',
    'frontend/src/routes/BasketPage.module.css',
    'frontend/src/components/CheckoutPanel.jsx',
    'frontend/src/hooks/useCartQueries.js',
    'frontend/src/hooks/useOwnedBasketItems.js',
    'frontend/src/hooks/useRecipeQueries.js',
    'frontend/src/hooks/useWeekPackChoices.js',
    'frontend/src/api/cartClient.js',
    'frontend/src/api/client.js',
    'app/api/cart.py',
    'app/api/planner.py',
    'app/planner/basket.py',
  ],
  pantry: [
    'frontend/src/routes/PantryPage.jsx',
    'frontend/src/routes/PantryPage.module.css',
    'frontend/src/hooks/usePantry.js',
    'frontend/src/hooks/useDebouncedSearch.js',
    'frontend/src/api/pantryClient.js',
    'app/api/pantry.py',
    'app/pantry/model.py',
    'app/pantry/store.py',
    'app/pantry/harvest.py',
  ],
  recipeDetail: [
    'frontend/src/routes/RecipeDetailPage.jsx',
    'frontend/src/routes/RecipeDetailPage.module.css',
    'frontend/src/hooks/useRecipeQueries.js',
    'frontend/src/api/client.js',
    'app/audit.py',
  ],
  mapping: [
    'frontend/src/routes/MappingPage.jsx',
    'frontend/src/hooks/useMappingQueries.js',
    'frontend/src/api/mappingClient.js',
  ],
  mappingAliases: [
    'frontend/src/routes/MappingAliasesPage.jsx',
    'frontend/src/hooks/useMappingQueries.js',
    'frontend/src/api/mappingClient.js',
  ],
  mappingManual: [
    'frontend/src/routes/MappingManualPage.jsx',
    'frontend/src/components/ManualProductForm.jsx',
    'frontend/src/components/RecipeCard.jsx',
    'frontend/src/components/RecipeCard.module.css',
    'frontend/src/hooks/useMappingQueries.js',
    'frontend/src/api/mappingClient.js',
  ],
  mappingReview: [
    'frontend/src/routes/MappingReviewPage.jsx',
    'frontend/src/routes/MappingReviewPage.module.css',
    'frontend/src/routes/mappingComparison.js',
    'frontend/src/components/ManualProductForm.jsx',
    'frontend/src/components/RecipeCard.jsx',
    'frontend/src/components/RecipeCard.module.css',
    'frontend/src/hooks/useMappingQueries.js',
    'frontend/src/api/mappingClient.js',
  ],
}

function gitLastCommit(paths) {
  try {
    const output = execFileSync(
      'git',
      ['log', '-1', '--format=%cI%x09%h', '--', ...paths],
      { cwd: repoRoot, encoding: 'utf8' },
    ).trim()

    const [committedAt, shortSha] = output.split('\t')
    return committedAt ? { committedAt, shortSha } : null
  } catch {
    return null
  }
}

function latestFileMtime(paths) {
  const times = paths
    .map((file) => {
      try {
        return fs.statSync(path.resolve(repoRoot, file)).mtimeMs
      } catch {
        return 0
      }
    })
    .filter(Boolean)

  return times.length ? Math.max(...times) : 0
}

function pageLastUpdated(files) {
  const paths = [...sharedPageFiles, ...files]
  const gitMeta = gitLastCommit(paths)
  const committedMs = gitMeta?.committedAt ? new Date(gitMeta.committedAt).getTime() : 0
  const mtimeMs = latestFileMtime(paths)
  const updatedMs = Math.max(committedMs, mtimeMs)

  return updatedMs ? { committedAt: new Date(updatedMs).toISOString(), shortSha: gitMeta?.shortSha } : null
}

function getPageLastUpdated() {
  return Object.fromEntries(
    Object.entries(pageFiles).map(([page, files]) => [page, pageLastUpdated(files)]),
  )
}

export default defineConfig({
  define: {
    __PAGE_LAST_UPDATED__: JSON.stringify(getPageLastUpdated()),
  },
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    proxy: {
      '/api': apiTarget,
    },
  },
})
