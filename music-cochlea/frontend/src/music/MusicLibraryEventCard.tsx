import './MusicLibraryEventCard.css';

/** A verified action is a small timeline event, not another chat bubble. */
export default function MusicLibraryEventCard({ content }: { content: string }) {
  const boundary = content.indexOf('。');
  const title = boundary >= 0 ? content.slice(0, boundary + 1) : content;
  const note = boundary >= 0 ? content.slice(boundary + 1).trim() : '';
  if (!note) return <div className="music-library-event-row">
    <div className="music-library-event music-library-event-static"><span className="music-library-event-icon" aria-hidden="true">♫</span><span>{title}</span></div>
  </div>;
  return <div className="music-library-event-row">
    <details className="music-library-event">
      <summary><span className="music-library-event-icon" aria-hidden="true">♫</span><span>{title}</span><small>当时的心情 ›</small></summary>
      <p>{note}</p>
    </details>
  </div>;
}
