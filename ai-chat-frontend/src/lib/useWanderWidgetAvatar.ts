import { useEffect } from 'react';

export function useWanderWidgetAvatar(avatar: string) {
  useEffect(() => {
    const plugin = (window as any).Capacitor?.Plugins?.MobileTool;
    if (!avatar || !plugin?.setWidgetAvatar) return;
    let current = true;
    async function sync() {
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 128;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.beginPath();
      ctx.arc(64, 64, 64, 0, 2 * Math.PI);
      ctx.clip();
      ctx.fillStyle = '#edd5c7';
      ctx.fillRect(0, 0, 128, 128);
      if (avatar.startsWith('data:image/')) {
        const image = new Image();
        image.src = avatar;
        await image.decode();
        const side = Math.min(image.width, image.height);
        ctx.drawImage(image, (image.width - side) / 2, (image.height - side) / 2, side, side, 0, 0, 128, 128);
      } else {
        ctx.font = '68px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillStyle = '#623c39';
        ctx.fillText(avatar, 64, 64, 112);
      }
      if (current) await plugin.setWidgetAvatar({ png: canvas.toDataURL('image/png').split(',')[1] });
    }
    void sync().catch(() => { /* An older APK or unavailable avatar keeps its last thumbnail. */ });
    return () => { current = false; };
  }, [avatar]);
}
