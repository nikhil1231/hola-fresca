import {
  Alert,
  Badge,
  Box,
  Button,
  Group,
  Loader,
  PasswordInput,
  Stack,
  Text,
  TextInput,
  Title,
} from '@mantine/core'
import { IconAlertCircle, IconLogin } from '@tabler/icons-react'

import classes from './CheckoutPanel.module.css'

const DEFAULT_SHOP = 'the shop'

function statusLabel(status) {
  if (status === 'ready') return 'connected'
  if (status === 'awaiting_otp') return 'awaiting OTP'
  if (status === 'needs_password') return 'sign-in needed'
  return 'logged out'
}

const STAGE_LABELS = {
  checking_session: 'Checking your saved session…',
  signing_in: 'Signing in…',
  waiting_for_code: 'Waiting for the emailed code…',
  entering_code: 'Entering the code…',
}

function stageLabel(stage, fallback) {
  return STAGE_LABELS[stage] ?? fallback
}

// There is no account picker any more: you have one connection per shop, it is
// yours, and the server resolves it. What is left to show is whether it works.
export function RetailerAccountStatus({ connection, shop = DEFAULT_SHOP }) {
  const { connected, reconnecting, status } = connection
  const label = reconnecting ? 'checking' : statusLabel(status.data?.status)
  return (
    <Badge
      color={connected ? 'green' : reconnecting ? 'yellow' : 'gray'}
      variant="light"
      aria-label={`${shop} connection: ${label}`}
    >
      {label}
    </Badge>
  )
}

export default function RetailerLoginPanel({ connection, shop = DEFAULT_SHOP }) {
  const {
    awaitingOtp,
    handlingCode,
    login,
    loginEmail,
    loginPassword,
    otp,
    otpCode,
    reconnecting,
    setLoginEmail,
    setLoginPassword,
    setOtpCode,
    stage,
    status,
    submitLogin,
  } = connection
  const signingIn = login.isPending || reconnecting

  return (
    <Box className={classes.connectionPanel}>
      <Stack gap="sm">
        <Group justify="space-between">
          <Title order={4}>{awaitingOtp ? 'Check your email' : `Connect to ${shop}`}</Title>
          {(status.isLoading || reconnecting || handlingCode) && <Loader size="sm" />}
        </Group>
        {awaitingOtp ? (
          <>
            <Text size="sm" c="dimmed">
              {shop} sent a verification code. Enter it to finish signing in.
            </Text>
            <Group align="flex-end">
              <PasswordInput
                label="Verification code"
                value={otpCode}
                disabled={otp.isPending}
                onChange={(event) => setOtpCode(event.currentTarget.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && otpCode.trim()) otp.mutate(otpCode)
                }}
                flex={1}
              />
              <Button
                disabled={!otpCode.trim() || otp.isPending}
                loading={otp.isPending}
                onClick={() => otp.mutate(otpCode)}
              >
                Submit
              </Button>
            </Group>
          </>
        ) : (
          <>
            <Text size="sm" c="dimmed">
              {reconnecting
                ? 'Reusing your saved session…'
                : signingIn
                  ? `${shop} is checking your login. This can take up to 90 seconds.`
                  : `Sign in to connect your ${shop} trolley. Your password is used for this request and is not stored.`}
            </Text>
            <TextInput
              label="Email"
              autoComplete="username"
              value={loginEmail}
              disabled={login.isPending}
              onChange={(event) => setLoginEmail(event.currentTarget.value)}
            />
            <PasswordInput
              label="Password"
              autoComplete="current-password"
              value={loginPassword}
              disabled={login.isPending}
              onChange={(event) => setLoginPassword(event.currentTarget.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && loginEmail.trim() && loginPassword) submitLogin()
              }}
            />
            <Button
              leftSection={signingIn ? null : <IconLogin size={16} />}
              disabled={!loginEmail.trim() || !loginPassword || signingIn}
              loading={signingIn}
              onClick={submitLogin}
            >
              {signingIn ? stageLabel(stage, 'Signing in…') : `Sign in to ${shop}`}
            </Button>
          </>
        )}
        {(login.error || otp.error || status.error) && (
          <Alert color="red" icon={<IconAlertCircle size={18} />}>
            {login.error?.message ?? otp.error?.message ?? status.error?.message}
          </Alert>
        )}
      </Stack>
    </Box>
  )
}
