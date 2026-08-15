import { Container } from '@mantine/core'

const APP_CONTAINER_SIZE = 'var(--hf-content-max)'
const APP_CONTAINER_PADDING = { base: 'md', sm: 'xl' }

export default function AppContainer({ children, ...props }) {
  return (
    <Container size={APP_CONTAINER_SIZE} px={APP_CONTAINER_PADDING} {...props}>
      {children}
    </Container>
  )
}
