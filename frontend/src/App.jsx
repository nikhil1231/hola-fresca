import { Outlet } from 'react-router-dom'
import { AppShell, Container } from '@mantine/core'

import Header from './components/Header.jsx'

function App() {
  return (
    <AppShell header={{ height: 64 }} padding={0}>
      <AppShell.Header>
        <Header />
      </AppShell.Header>
      <AppShell.Main>
        <Container size="xl" py="xl">
          <Outlet />
        </Container>
      </AppShell.Main>
    </AppShell>
  )
}

export default App
