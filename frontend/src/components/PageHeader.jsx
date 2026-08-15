import { Anchor, Box, Group, Title } from '@mantine/core'
import { IconArrowLeft } from '@tabler/icons-react'
import { Link } from 'react-router-dom'

import classes from './PageHeader.module.css'

export default function PageHeader({
  title,
  description,
  icon,
  badge,
  actions,
  backLink,
  className,
}) {
  return (
    <header className={[classes.root, className].filter(Boolean).join(' ')}>
      {backLink && (
        <Anchor component={Link} to={backLink.to} className={classes.backLink}>
          <IconArrowLeft size={16} aria-hidden="true" />
          {backLink.label}
        </Anchor>
      )}

      <Group justify="space-between" align="flex-start" wrap="wrap" gap="md">
        <Group gap="sm" align="center" wrap="nowrap" className={classes.identity}>
          {icon && <Box className={classes.icon}>{icon}</Box>}
          <Group gap="xs" align="center" wrap="wrap" className={classes.titleRow}>
            <Title order={1} className={classes.title}>
              {title}
            </Title>
            {badge}
          </Group>
        </Group>
        {actions && <Box className={classes.actions}>{actions}</Box>}
      </Group>

      {description && <div className={classes.description}>{description}</div>}
    </header>
  )
}
