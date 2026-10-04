import { useEffect, useRef, type ReactNode } from 'react';

export default function SocialAvatarDialog({ name, avatar, onClose, children }: {
  name: string; avatar: ReactNode; onClose: () => void; children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);
  return <dialog ref={dialog} className="social-avatar-dialog" aria-label={`${name}的头像`}
    onCancel={onClose} onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="social-avatar-dialog-inner">
      <header><h3>{name}</h3><button type="button" onClick={onClose} aria-label="关闭头像预览" autoFocus>×</button></header>
      <div className="social-avatar-enlarged">{avatar}</div>
      <div className="social-avatar-dialog-actions">{children}</div>
    </div>
  </dialog>;
}
