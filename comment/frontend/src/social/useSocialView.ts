import {useCallback, useEffect, useLayoutEffect, useRef, useState, type SetStateAction} from 'react';
import {readView, writeView} from './viewState';

export function useSocialView<T>(key: string, initial: T | (() => T)) {
  // Pages are keyed by home. Keys must stay fixed for a mounted field.
  const [value, setValue] = useState<T>(() => readView(key, initial));
  const update = useCallback((next: SetStateAction<T>) => setValue(old => {
    const value = typeof next === 'function' ? (next as (v:T)=>T)(old) : next;
    writeView(key, value); return value;
  }), [key]);
  return [value, update] as const;
}

type Place = {id: string; offset: number; top: number};
export function useSocialReadingPlace(key: string, ready: boolean, version: unknown,
  more: boolean, loadEarlier: () => void, active = true) {
  const body = useRef<HTMLElement | null>(null);
  const saved = useRef(readView<Place | null>(key, null));
  const restoring = useRef(Boolean(saved.current));
  const rounds = useRef(0);
  const [notice, setNotice] = useState('');
  const capture = useCallback(() => {
    const node = body.current; if (!node || restoring.current) return;
    const top = node.getBoundingClientRect().top;
    const row = [...node.querySelectorAll<HTMLElement>('[data-social-view-post]')]
      .find(row => row.getBoundingClientRect().bottom > top);
    if(!row)return; // Loading/denied/empty pages must not erase the old anchor.
    const value = {id: row?.dataset.socialViewPost || '', offset: row ? row.getBoundingClientRect().top - top : 0, top: node.scrollTop};
    saved.current = value; writeView(key, value);
  }, [key]);
  useLayoutEffect(() => {
    if (!active) return;
    const node = body.current; if (!node) return;
    saved.current = readView<Place | null>(key, null);
    restoring.current = Boolean(saved.current); rounds.current = 0;
    const cancel = () => { restoring.current = false; };
    node.addEventListener('scroll', capture, {passive:true});
    node.addEventListener('wheel', cancel, {passive:true});
    node.addEventListener('touchstart', cancel, {passive:true});
    node.addEventListener('pointerdown', cancel, {passive:true});
    return () => { capture(); node.removeEventListener('scroll', capture); node.removeEventListener('wheel', cancel);
      node.removeEventListener('touchstart', cancel); node.removeEventListener('pointerdown', cancel); };
  }, [active, key, capture]);
  useEffect(() => {
    if (!active || !ready || !restoring.current || !body.current || !saved.current) return;
    const node = body.current, place = saved.current;
    const row = [...node.querySelectorAll<HTMLElement>('[data-social-view-post]')].find(row => row.dataset.socialViewPost === place.id);
    if (place.id && !row && more && rounds.current < 8) {
      rounds.current++; loadEarlier(); return;
    }
    if (row) node.scrollTop += row.getBoundingClientRect().top - node.getBoundingClientRect().top - place.offset;
    else node.scrollTop = place.top;
    restoring.current = false;
    if (place.id && !row) setNotice('原来的阅读位置暂未找到（内容可能已移除）；已恢复到可读取的位置。');
  }, [active, ready, version, more, loadEarlier]);
  return {body, restoring, notice};
}
