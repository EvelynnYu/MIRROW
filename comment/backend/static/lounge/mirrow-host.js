/* The parent hosts appearance/navigation only; no access to friend credentials. */
(() => {
  const parentOrigin = document.referrer ? new URL(document.referrer).origin : location.origin;
  window.navigateSubPageBack = () => window.parent.postMessage({type:'mirrow-lounge-close'}, parentOrigin);
  window.openFederatedRoom = () => {
    if (window.parent === window) {
      location.assign('/api/lounge/federated-page');
      return;
    }
    window.parent.postMessage({type:'mirrow-lounge-open-federated'}, parentOrigin);
  };
  window.openLoungeBoard = view => {
    const tab = view === 'friends' ? 'friends' : 'home';
    if (window.parent === window) {
      location.assign('/api/lounge/board-page?tab=' + tab);
      return;
    }
    window.parent.postMessage({type:'mirrow-lounge-open-board', tab}, parentOrigin);
  };
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.origin !== parentOrigin) return;
    if (event.data?.type === 'mirrow-lounge-theme') {
      document.documentElement.dataset.theme = event.data.theme === 'dark' ? 'dark' : 'light';
    }
  });
})();
