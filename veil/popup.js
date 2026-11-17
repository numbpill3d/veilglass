const api = globalThis.browser || globalThis.chrome;
const $ = (s) => document.querySelector(s);
const DEFAULTS = {
  enabled: true,
  modules: { canvas: true, webgl: true, audio: true, fonts: true, nav: true, battery: true },
  webrtc: true,
  excluded: [],
};
let host = null;
let s = null;

async function load() {
  const got = await api.storage.local.get(DEFAULTS);
  s = { ...DEFAULTS, ...got, modules: { ...DEFAULTS.modules, ...(got.modules || {}) } };
  const [tab] = await api.tabs.query({ active: true, currentWindow: true });
  try {
    const u = new URL(tab.url);
    if (/^https?:$/.test(u.protocol)) host = u.hostname;
  } catch (_) {}
  render();
}

function render() {
  $("#enabled").checked = s.enabled;
  $("#webrtc").checked = s.webrtc;
  $("#host").textContent = host || "not a web page";
  $("#site").disabled = !host;
  $("#site").checked = !!host && !s.excluded.includes(host);
  for (const el of document.querySelectorAll("[data-mod]")) el.checked = s.modules[el.dataset.mod];
  const on = s.enabled && (!host || !s.excluded.includes(host));
  $("#state").textContent = on ? "veiled" : "bare";
  $("#state").classList.toggle("on", on);
}

async function save(hint) {
  await api.storage.local.set(s);
  render();
  $("#hint").textContent = hint ? "reload the page for this to take hold" : "";
}

$("#enabled").addEventListener("change", (e) => { s.enabled = e.target.checked; save(true); });
$("#webrtc").addEventListener("change", (e) => { s.webrtc = e.target.checked; save(false); });
$("#site").addEventListener("change", (e) => {
  s.excluded = s.excluded.filter((h) => h !== host);
  if (!e.target.checked) s.excluded.push(host);
  save(true);
});
for (const el of document.querySelectorAll("[data-mod]")) {
  el.addEventListener("change", () => { s.modules[el.dataset.mod] = el.checked; save(true); });
}
load();
