import {readView, writeView} from './viewState';

type Attempt = {signature: string; requestId: string};
// Keep an ambiguous manual retry attached to the same operation across view
// switches. Memory only: a full app reload discards pending attempts and drafts.
export function beginSocialWrite(scope: string, payload: object): string {
  const key = `write:${scope}`, signature = JSON.stringify(payload);
  const previous = readView<Attempt | null>(key, null);
  if (previous?.signature === signature) return previous.requestId;
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const requestId = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  writeView(key, {signature, requestId});
  return requestId;
}

export function completeSocialWrite(scope: string, requestId: string) {
  const key = `write:${scope}`;
  if (readView<Attempt | null>(key, null)?.requestId === requestId) writeView(key, null);
}
