import { useEffect, useState } from 'react';
import WanderApp from './WanderApp';
import LocalApp from './local/LocalApp';
import { request, type LocalState } from './local/api';
export default function App() {
  const [state, setState] = useState<LocalState | null>(null);
  const [standalone, setStandalone] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => { request('/api/local/state').then(setState).catch(e => {
    if (e.status === 404) setStandalone(true); else setError(e.message);
  }); }, []);
  if (standalone) return <WanderApp />;
  if (state) return <LocalApp initial={state} />;
  return <main className="public-wander-shell"><h1>MIRROW</h1><p role="status">{error || '连接本地后端…'}</p></main>;
}
