// veilglass shroud: core. Runs in the page's own world at document_start,
// before any page script. Holds the shared helpers for the module files that
// follow it; seal.js removes the handoff before the page can see it.
(() => {
  "use strict";
  const KEY = "__veilglass_shroud__";
  if (window[KEY]) return;

  const nativeToString = Function.prototype.toString;
  const disguise = new WeakMap(); // patched fn -> original fn
  const installers = [];
  const shrouded = new WeakSet();

  // toString of any patched function reports the original native source.
  // A patched function from a sibling frame's shroud is not in our map, so
  // ask the toString of each same-origin frame until one of them knows it.
  let asking = false;
  const FAKE_MARK = /return impl\(orig, this\b|const orig = typeof this === "function" && disguise\.get/;
  function askFrames(fn, src) {
    if (asking) return src;
    asking = true;
    try {
      const seen = new Set();
      const walk = (w) => {
        if (!w || seen.has(w)) return null;
        seen.add(w);
        try {
          const ts = w.Function.prototype.toString;
          if (ts !== toStringImpl) {
            const r = ts.call(fn);
            if (r !== src) return r;
          }
        } catch (_) { return null; } // cross-origin
        for (let i = 0; i < w.frames.length; i++) {
          const r = walk(w.frames[i]);
          if (r) return r;
        }
        return null;
      };
      return walk(window.top) || src;
    } catch (_) {
      return src;
    } finally {
      asking = false;
    }
  }
  const toStringImpl = {
    toString() {
      const orig = typeof this === "function" && disguise.get(this);
      const src = nativeToString.call(orig || this);
      return orig || !FAKE_MARK.test(src) ? src : askFrames(this, src);
    },
  }.toString;
  disguise.set(toStringImpl, nativeToString);

  // One seed per top document, latched on first use, so repeated reads inside
  // a page agree with each other. A reload draws a new shroud.
  let seed = 0;
  function getSeed() {
    if (!seed) {
      const a = new Uint32Array(1);
      crypto.getRandomValues(a);
      seed = a[0] || 1;
    }
    return seed;
  }

  // Small fast integer hash (xorshift-multiply).
  function mix(...vals) {
    let h = getSeed() ^ 0x9e3779b9;
    for (const v of vals) {
      h ^= v | 0;
      h = Math.imul(h ^ (h >>> 16), 0x85ebca6b);
      h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35);
      h ^= h >>> 16;
    }
    return h >>> 0;
  }

  function strHash(s) {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
    return h >>> 0;
  }

  function finish(fake, orig) {
    disguise.set(fake, orig);
    try { Object.defineProperty(fake, "length", { value: orig.length }); } catch (_) {}
    try { Object.defineProperty(fake, "name", { value: orig.name }); } catch (_) {}
    return fake;
  }

  // Replace a method on a prototype. impl(orig, thisArg, args) does the work.
  function hookMethod(proto, name, impl) {
    const desc = proto && Object.getOwnPropertyDescriptor(proto, name);
    if (!desc || typeof desc.value !== "function") return;
    const orig = desc.value;
    const fake = {
      [name](...args) { return impl(orig, this, args); },
    }[name]; // method shorthand: no prototype, not constructible, like natives
    Object.defineProperty(proto, name, { ...desc, value: finish(fake, orig) });
  }

  // Replace a getter on a prototype. impl(origGetter, thisArg) returns the value.
  function hookGetter(proto, name, impl) {
    const desc = proto && Object.getOwnPropertyDescriptor(proto, name);
    if (!desc || typeof desc.get !== "function") return;
    const orig = desc.get;
    const fake = Object.getOwnPropertyDescriptor(
      { get [name]() { return impl(orig, this); } }, name).get;
    Object.defineProperty(proto, name, { ...desc, get: finish(fake, orig) });
  }

  // Flip the low bit of a sparse, content-dependent set of pixel channels.
  // Same seed + same pixels = same output, so double reads match.
  function noisePixels(data, width) {
    const n = data.length;
    if (n === 0 || n > 16e6) return data;
    let content = 0;
    for (let i = 0; i < n; i += 97) content = (content * 31 + data[i]) | 0;
    for (let i = 0; i < n; i += 4) {
      if (data[i + 3] === 0) continue; // leave transparent pixels alone
      const h = mix(i, content, width | 0);
      if ((h & 31) !== 0) continue; // about 1 pixel in 32
      const c = i + ((h >>> 5) % 3);
      data[c] ^= 1;
    }
    return data;
  }

  function install(w) {
    if (!w || shrouded.has(w)) return;
    shrouded.add(w);
    // A same-origin child frame may already carry its own shroud (its own
    // content script ran first). Our captured native toString is the one copy
    // the page can never reach, so use it to tell, and never stack two veils.
    try {
      if (!nativeToString.call(w.Function.prototype.toString).includes("[native code]")) return;
    } catch (_) { return; }
    try { w.Function.prototype.toString = toStringImpl; } catch (_) { return; }
    for (const fn of installers) {
      try { fn(w); } catch (_) { /* a broken module must not break the page */ }
    }
  }

  // Fingerprinters borrow clean prototypes from fresh about:blank iframes.
  // Shroud a child window the moment the parent reaches into it.
  installers.push((w) => {
    const reach = (orig, el, viaDoc) => {
      const v = orig.call(el);
      try {
        const child = viaDoc ? v && v.defaultView : v;
        if (child && child !== w) install(child);
      } catch (_) {}
      return v;
    };
    for (const C of [w.HTMLIFrameElement, w.HTMLFrameElement, w.HTMLObjectElement]) {
      if (!C) continue;
      hookGetter(C.prototype, "contentWindow", (o, el) => reach(o, el, false));
      hookGetter(C.prototype, "contentDocument", (o, el) => reach(o, el, true));
    }
  });

  window[KEY] = {
    hookMethod, hookGetter, noisePixels, mix, strHash, installers,
    isFirefox: /Firefox\//.test(navigator.userAgent),
    run() { install(window); },
  };
})();
