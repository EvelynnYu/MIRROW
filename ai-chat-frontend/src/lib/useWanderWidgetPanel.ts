import { useEffect, useState } from 'react';
import { Capacitor, registerPlugin, type PluginListenerHandle } from '@capacitor/core';

type Panel = 'mood' | 'activity';
const mobile = registerPlugin<{
  addListener(name: 'wanderWidgetPanel', listener: (event: { panel: Panel }) => void): Promise<PluginListenerHandle>;
}>('MobileTool');

export function useWanderWidgetPanel(view: string, goToChat: (view: 'chat') => void,
  showMood: (open: boolean) => void, showLog: (open: boolean) => void, showSidebar: (open: boolean) => void) {
  const [panel, setPanel] = useState<Panel | null>(null);
  useEffect(() => {
    if (!Capacitor.isNativePlatform()) return;
    const listener = mobile.addListener('wanderWidgetPanel', event => {
      if (event.panel === 'activity' || event.panel === 'mood') setPanel(event.panel);
    });
    return () => { void listener.then(handle => handle.remove()); };
  }, []);

  useEffect(() => {
    if (!panel) return;
    if (view !== 'chat') { goToChat('chat'); return; }
    showSidebar(panel === 'activity');
    showLog(panel === 'activity');
    showMood(panel === 'mood');
    setPanel(null);
  }, [panel, view, goToChat, showMood, showLog, showSidebar]);
}
