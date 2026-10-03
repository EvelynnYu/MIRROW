import { useSyncExternalStore } from 'react';
import type { LongTextCard } from '../shared/types';
let draft: LongTextCard | null = null;
const subscribers = new Set<() => void>();
export function setMusicDraft(value: LongTextCard | null) { draft = value; subscribers.forEach(fn => fn()); }
export function useMusicDraft() { return useSyncExternalStore(fn => { subscribers.add(fn); return () => subscribers.delete(fn); }, () => draft); }
