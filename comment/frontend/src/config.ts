declare global {
  interface Window { MirrowCommentHost?: { apiBase: string; onBack?: () => void } }
}
// The receiving App configures its own private backend. No site or identity is preset.
export function getApiBase(): string {
  const value = window.MirrowCommentHost?.apiBase;
  if (!value) throw new Error('请先接入本家管理后端地址；不得填写公网访客墙地址。');
  return value.replace(/\/$/, '');
}
