import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { apiFetch } from './api';
import { getApiBase } from './config';

const defaults = { aiName: 'AI', userName: '用户' };
const PersonaContext = createContext(defaults);

export function PersonaProvider({ children }: { children: ReactNode }) {
  const [names, setNames] = useState(defaults);
  useEffect(() => {
    let disposed = false;
    apiFetch(`${getApiBase()}/api/wander/persona`).then(async response => {
      if (!response.ok) return;
      const data = await response.json();
      if (!disposed) setNames({
        aiName: typeof data.ai_name === 'string' && data.ai_name.trim() ? data.ai_name : defaults.aiName,
        userName: typeof data.user_name === 'string' && data.user_name.trim() ? data.user_name : defaults.userName,
      });
    }).catch(() => { /* An older host uses neutral labels, never a private default. */ });
    return () => { disposed = true; };
  }, []);
  return <PersonaContext.Provider value={names}>{children}</PersonaContext.Provider>;
}

export const usePersona = () => useContext(PersonaContext);
