export function getApiBase(): string {
  return localStorage.getItem('music_api') || 'http://127.0.0.1:8005';
}
export function getPlatform(): string {
  return /Android|iPhone|iPad|Mobile/i.test(navigator.userAgent) ? 'mobile' : 'pc';
}
export function getAccessHeaders(): Record<string,string> {
  const token = localStorage.getItem('music_token');
  return token ? {Authorization:'Bearer '+token} : {};
}
