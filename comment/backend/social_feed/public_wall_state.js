/* Transient, per-login UI state. No credentials or drafts enter browser storage. */
(() => {
  const views = new Map(), actions = new Map();
  const key = (...parts) => JSON.stringify(parts);
  window.MirrowWallState = {
    view(actor, id, detail) {
      const identifier = key(actor, id, detail);
      if (!views.has(identifier)) views.set(identifier, { text: '', replyId: null, expanded: null });
      return views.get(identifier);
    },
    begin(actor, operation, payload) {
      const identifier = key(actor, operation), signature = JSON.stringify(payload);
      const previous = actions.get(identifier);
      if (previous?.busy) return null;
      const ticket = { identifier, signature, busy: true,
        requestId: previous?.signature === signature ? previous.requestId : crypto.randomUUID() };
      actions.set(identifier, ticket);
      return ticket;
    },
    finish(ticket, confirmed) {
      if (actions.get(ticket.identifier) !== ticket) return;
      if (confirmed) actions.delete(ticket.identifier);
      else ticket.busy = false; // Manual retry of the same draft reuses its receipt ID.
    },
    clear() { views.clear(); actions.clear(); },
  };
})();
