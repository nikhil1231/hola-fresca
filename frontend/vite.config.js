import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const frontendRoot = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(frontendRoot, '..')

// The backend port is overridable via VITE_API_TARGET so the dev server can
// point at a non-default port when 8000 is taken.
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

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
  browse: [
    'frontend/src/routes/BrowsePage.jsx',
    'frontend/src/components/FilterPanel.jsx',
    'frontend/src/components/RangeFilter.jsx',
    'frontend/src/components/RecipeCard.jsx',
    'frontend/src/components/RecipeCard.module.css',
    'frontend/src/data/defaultFacets.js',
    'frontend/src/hooks/useFilters.js',
    'frontend/src/hooks/useRecipeQueries.js',
    'frontend/src/api/client.js',
  ],
  cuisines: [
    'frontend/src/routes/CuisinesPage.jsx',
    'frontend/src/routes/CuisinesPage.module.css',
    'frontend/src/hooks/useRecipeQueries.js',
    'frontend/src/api/client.js',
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
    'frontend/src/hooks/useMappingQueries.js',
    'frontend/src/api/mappingClient.js',
  ],
  mappingReview: [
    'frontend/src/routes/MappingReviewPage.jsx',
    'frontend/src/routes/MappingReviewPage.module.css',
    'frontend/src/components/ManualProductForm.jsx',
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

function getPageLastUpdated() {
  return Object.fromEntries(
    Object.entries(pageFiles).map(([page, files]) => [
      page,
      gitLastCommit([...sharedPageFiles, ...files]),
    ]),
  )
}

export default defineConfig({
  define: {
    __PAGE_LAST_UPDATED__: JSON.stringify(getPageLastUpdated()),
  },
  plugins: [react()],
  server: {
    proxy: {
      '/api': apiTarget,
    },
  },
})
