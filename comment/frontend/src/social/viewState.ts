// Volatile UI state only. Never persist credentials, feed rows, cursors or grants.
const state = new Map<string, unknown>();
export const socialViewKey = (base: string, ...parts: string[]) => JSON.stringify([base, ...parts]);
export function readView<T>(key: string, fallback: T | (() => T)): T {
  return state.has(key) ? state.get(key) as T : typeof fallback === 'function' ? (fallback as () => T)() : fallback;
}
export function writeView<T>(key: string, value: T) {
  state.delete(key); state.set(key, value);
  // Bounded across cards/homes during a long desktop session; reload clears all.
  while (state.size > 512) state.delete(state.keys().next().value!);
}
export function clearSocialViews() { state.clear(); }
