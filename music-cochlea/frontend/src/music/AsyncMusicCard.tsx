import { useEffect, useState } from 'react';
import { musicApi } from './api';
import MusicCard from './MusicCard';
import type { Song } from './types';

type Attachment = { generation_id?: string; attachment_status?: string; attachment_error?: string; music_card?: Song };
export default function AsyncMusicCard({ attachment }: { attachment: Attachment }) {
  const [data, setData] = useState(attachment);
  const [error, setError] = useState('');
  useEffect(() => { setData(attachment); }, [attachment]);
  useEffect(() => {
    if (!data.generation_id || data.attachment_status !== 'pending') return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const result = await musicApi<Attachment>('/cards/' + data.generation_id);
        if (cancelled) return;
        setData(result); setError('');
        if (result.attachment_status !== 'pending') return;
      } catch (e) { if (!cancelled) setError((e as Error).message); }
      if (!cancelled) timer = setTimeout(poll, 2000);
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [data.generation_id, data.attachment_status]);
  if (data.attachment_status === 'ready' && data.music_card) return <MusicCard song={data.music_card} />;
  return <article className="music-card" role="status"><div className="music-card-main"><div className="music-cover">♪</div><div className="music-card-copy"><small>一首歌，正在寄来</small><strong>{data.attachment_status === 'failed' ? '这张卡片暂未完成' : '正在核对歌曲资料…'}</strong><span>{data.attachment_status === 'failed' ? data.attachment_error : '完成后会出现在这里，不会自动播放'}</span></div></div>{error && <p className="music-error">{error}</p>}</article>;
}
