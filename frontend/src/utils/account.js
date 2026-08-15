export function accountInitials(account) {
  const name = account?.name?.trim()
  if (name) {
    const words = name.split(/\s+/).filter(Boolean)
    return `${words[0][0]}${words.length > 1 ? words.at(-1)[0] : ''}`.toUpperCase()
  }

  const emailName = account?.email?.split('@')[0] ?? ''
  const words = emailName.split(/[._-]+/).filter(Boolean)
  if (words.length) {
    return `${words[0][0]}${words.length > 1 ? words.at(-1)[0] : ''}`.toUpperCase()
  }
  return 'HF'
}
