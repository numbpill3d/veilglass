// veilglass shroud: audio. The rendered-waveform fingerprint (OfflineAudioContext
// + oscillator + compressor) gets a sub-audible, per-page wobble.
(() => {
  const V = window.__veilglass_shroud__;
  if (!V) return;

  V.installers.push((w) => {
    const done = new WeakMap(); // AudioBuffer -> Set of channels already shrouded

    function shroud(data, ch) {
      for (let i = 0; i < data.length; i += 1) {
        const h = V.mix(i, ch, 0xa0d10);
        if ((h & 63) !== 0) continue;
        data[i] += ((h >>> 6) % 3 - 1) * 1e-7;
      }
    }

    const AB = w.AudioBuffer && w.AudioBuffer.prototype;
    V.hookMethod(AB, "getChannelData", (orig, buf, args) => {
      const data = orig.apply(buf, args);
      let set = done.get(buf);
      if (!set) done.set(buf, (set = new Set()));
      const ch = args[0] | 0;
      if (!set.has(ch)) { set.add(ch); shroud(data, ch); }
      return data;
    });
    V.hookMethod(AB, "copyFromChannel", (orig, buf, args) => {
      try { AB.getChannelData.call(buf, args[1] | 0); } catch (_) {}
      return orig.apply(buf, args);
    });

    const AN = w.AnalyserNode && w.AnalyserNode.prototype;
    V.hookMethod(AN, "getFloatFrequencyData", (orig, node, args) => {
      const r = orig.apply(node, args);
      const arr = args[0];
      if (arr) for (let i = 0; i < arr.length; i++) {
        const h = V.mix(i, 0xf4e9);
        if ((h & 15) === 0) arr[i] += ((h >>> 4) % 3 - 1) * 1e-4;
      }
      return r;
    });
  });
})();
