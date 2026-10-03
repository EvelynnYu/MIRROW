import { useEffect, useState } from 'react';
import type { Song } from './types';
import MusicComposer from './MusicComposer';
import MusicHub from './MusicHub';
import { setMusicDraft } from './draft';
export default function MusicHost({ onAttach }: { onAttach: () => void }) {
  const [section, setSection] = useState<string | null>(null);
  const [song, setSong] = useState<Song | undefined>();
  const [composeSong, setComposeSong] = useState<Song | undefined>();
  const [composing, setComposing] = useState(false);
  useEffect(() => {
    const open = (event: Event) => { setComposing(false); const detail = (event as CustomEvent).detail; setSection(detail?.section || 'home'); setSong(detail?.song); };
    const compose = (event: Event) => { setSection(null); setComposeSong((event as CustomEvent).detail?.song); setComposing(true); };
    window.addEventListener('mirrow:music-open', open); window.addEventListener('mirrow:music-compose', compose);
    return () => { window.removeEventListener('mirrow:music-open', open); window.removeEventListener('mirrow:music-compose', compose); };
  }, []);
  return <>{section && <MusicHub section={section} songToAdd={song} onClose={() => setSection(null)} />}{composing && <MusicComposer initialSong={composeSong} onClose={() => { setComposing(false); setComposeSong(undefined); }} onSave={card => { setMusicDraft(card); setComposing(false); setComposeSong(undefined); onAttach(); }} />}</>;
}
