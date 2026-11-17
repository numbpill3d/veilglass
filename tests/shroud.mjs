// End-to-end test for the shroud, driven over the Chrome DevTools Protocol.
// Run through tests/run.sh, which starts the glass in demo mode and a headless
// Chromium with veil/ loaded. With a 5th argument it also writes the README
// screenshots into that directory.
import { writeFileSync } from "node:fs";
const [port, base, extId, shots] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const json = async (p, m = "GET") => (await fetch(`http://127.0.0.1:${port}${p}`, { method: m })).json();

async function open(url) {
  const t = await json("/json/new?about:blank", "PUT");
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  let id = 0; const wait = new Map();
  ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); wait.get(m.id)?.(m); wait.delete(m.id); });
  const send = (method, params = {}) => new Promise((r) => { const i = ++id; wait.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
  const ev = async (expression) => (await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true })).result?.result?.value;
  await send("Page.enable");
  await send("Page.navigate", { url });
  await sleep(1200);
  return { send, ev };
}
const size = (tab, width, height) => tab.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
const shot = async (tab, file) => {
  if (!shots) return;
  const r = await tab.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(`${shots}/${file}`, Buffer.from(r.result.data, "base64"));
};
async function check(tab, reload = true) {
  if (reload) { await tab.send("Page.reload"); await sleep(500); }
  for (let k = 0; k < 60; k++) {
    const v = await tab.ev("document.body?.dataset.done ? document.getElementById('out').textContent : ''");
    if (v) return JSON.parse(v);
    await sleep(250);
  }
  throw new Error("veil-check never finished");
}

let failed = 0;
const ok = (name, cond, detail = "") => { if (!cond) failed++; console.log(`${cond ? "PASS" : "FAIL"}  ${name} ${detail}`); };

await sleep(3500); // let the extension register its page scripts
const pop = await open(`chrome-extension://${extId}/popup.html`);
const settle = (o) => pop.ev(`chrome.storage.local.set(${JSON.stringify(o)}).then(() => new Promise((r) => setTimeout(r, 700))).then(() => 1)`);
const rtc = () => pop.ev("chrome.privacy.network.webRTCIPHandlingPolicy.get({}).then((d) => d.value)");
ok("popup has extension APIs", (await pop.ev("typeof chrome.storage")) === "object");
ok("webrtc sealed", (await rtc()) === "disable_non_proxied_udp");

const page = await open(`${base}/veil-check`);
// bare baseline: exempt the site, read twice
await settle({ excluded: ["127.0.0.1"] });
const bare1 = await check(page), bare2 = await check(page);
ok("exempt site is bare and constant", bare1.canvasA === bare2.canvasA && bare1.audio === bare2.audio && bare1.webglPixels === bare2.webglPixels);
await settle({ excluded: [] });
const a = await check(page), b = await check(page);
ok("canvas veiled", a.canvasA !== bare1.canvasA && b.canvasA !== bare1.canvasA);
ok("audio + webgl veiled", a.audio !== bare1.audio && a.webglPixels !== bare1.webglPixels);
ok("fonts veiled", a.fontWidth !== bare1.fontWidth);
ok("same answer twice in one load", a.canvasA === a.canvasB && b.canvasA === b.canvasB);
ok("new shroud every load", a.canvasA !== b.canvasA && a.audio !== b.audio);
ok("blank iframe shares page seed", b.iframeCanvas === b.canvasA);
ok("patched functions look native (page + frame)", b.native && b.iframeNative);
ok("nothing left on navigator or window", b.ownProps === 0 && b.handoffGone);
if (shots) { await size(page, 900, 790); await sleep(300); await shot(page, "veil-check.png"); }

await settle({ modules: { canvas: false, webgl: true, audio: true, fonts: true, nav: true, battery: true } });
const c = await check(page);
ok("canvas switch off leaves other surfaces on", c.canvasA === bare1.canvasA && c.audio !== bare1.audio);
await settle({ enabled: false, modules: { canvas: true, webgl: true, audio: true, fonts: true, nav: true, battery: true } });
const d = await check(page);
ok("global off is bare and releases webrtc", d.canvasA === bare1.canvasA && (await rtc()) !== "disable_non_proxied_udp");
await settle({ enabled: true });
ok("back on", (await check(page)).canvasA !== bare1.canvasA);

if (shots) {
  await pop.ev("host = 'example.org'; render(); 1"); // as the popup reads on a real site
  await size(pop, 280, 600); await sleep(200);
  await size(pop, 280, await pop.ev("document.documentElement.scrollHeight")); await sleep(200);
  await shot(pop, "popup.png");
}

const glass = await open(`${base}/`);
await size(glass, 1200, 800);
await sleep(shots ? 26000 : 4000); // past the attunement quiet so omens show
ok("glass renders presences", (await glass.ev("document.body.innerText")).includes("presences"));
await shot(glass, "glass.png");

console.log(failed ? `${failed} failed` : "all passed");
process.exit(failed ? 1 : 0);
