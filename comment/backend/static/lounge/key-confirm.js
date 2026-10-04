/* Confirm credential replacement twice before the existing reception API mutates it. */
(() => {
  'use strict';
  window.MirrowVisitorKeyConfirm = (action, name, hasKey = true) => {
    const identity = name || '这位朋友';
    if (action === 'rotate') {
      const effect = hasKey
        ? `将为「${identity}」签发一把新 Key，旧 Key 会立即失效。`
        : `将为「${identity}」签发一把 Key。`;
      return window.confirm(`${effect}\n新 Key 只会显示一次，关闭后无法找回。是否继续？`)
        && window.confirm(`最后确认：现在${hasKey ? '轮换' : '签发'}「${identity}」的 Key？`);
    }
    if (action === 'revoke') {
      return window.confirm(`将暂停「${identity}」的入站访问，会客与共域暂不可进入。Key 和名册都保留，恢复身份后可继续使用未失效的原 Key。是否继续？`)
        && window.confirm(`最后确认：暂停「${identity}」的访问权？这不会删除 Key。`);
    }
    return false;
  };
})();
