// veilglass shroud: background. Keeps the page-world content script
// registration in step with the settings, and seals WebRTC.
const api = globalThis.browser || globalThis.chrome;

const MODULES = ["canvas", "webgl", "audio", "fonts", "nav", "battery"];
const DEFAULTS = {
  enabled: true,
  modules: Object.fromEntries(MODULES.map((m) => [m, true])),
  webrtc: true,
  excluded: [], // hostnames left unshrouded
};
const SCRIPT_ID = "veilglass-shroud";

async function settings() {
  const got = await api.storage.local.get(DEFAULTS);
  return { ...DEFAULTS, ...got, modules: { ...DEFAULTS.modules, ...(got.modules || {}) } };
}

async function syncScripts(s) {
  try { await api.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] }); } catch (_) {}
  const mods = MODULES.filter((m) => s.modules[m]);
  if (!s.enabled || mods.length === 0) return;
  const base = {
    id: SCRIPT_ID,
    js: ["inject/core.js", ...mods.map((m) => `inject/${m}.js`), "inject/seal.js"],
    matches: ["<all_urls>"],
    excludeMatches: s.excluded.flatMap((h) => [`*://${h}/*`]),
    runAt: "document_start",
    allFrames: true,
    world: "MAIN",
    persistAcrossSessions: true,
  };
  if (base.excludeMatches.length === 0) delete base.excludeMatches;
  try {
    // Chromium: also cover about:blank / srcdoc / blob frames.
    await api.scripting.registerContentScripts([{ ...base, matchOriginAsFallback: true }]);
  } catch (_) {
    await api.scripting.registerContentScripts([base]);
  }
}

async function syncWebRTC(s) {
  const pol = api.privacy && api.privacy.network && api.privacy.network.webRTCIPHandlingPolicy;
  if (!pol) return;
  try {
    if (s.enabled && s.webrtc) await pol.set({ value: "disable_non_proxied_udp" });
    else await pol.clear({});
  } catch (_) {}
}

let pending = Promise.resolve();
function sync() {
  pending = pending.then(async () => {
    const s = await settings();
    await syncScripts(s);
    await syncWebRTC(s);
  }).catch((e) => console.warn("veilglass shroud sync failed", e));
  return pending;
}

api.runtime.onInstalled.addListener(sync);
api.runtime.onStartup.addListener(sync);
api.storage.onChanged.addListener((_c, area) => { if (area === "local") sync(); });
