// veilglass shroud: seal. Applies every loaded module, then removes the
// handoff object before the first page script runs.
(() => {
  const V = window.__veilglass_shroud__;
  if (!V) return;
  try { V.run(); } finally { delete window.__veilglass_shroud__; }
})();
