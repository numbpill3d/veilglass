// veilglass shroud: fonts. Text measurements wobble by a few millionths per
// font and string, which scrambles the installed-font fingerprint without
// moving anything visibly.
(() => {
  const V = window.__veilglass_shroud__;
  if (!V) return;
  V.installers.push((w) => {
    const TM = w.TextMetrics && w.TextMetrics.prototype;
    const C2D = w.CanvasRenderingContext2D && w.CanvasRenderingContext2D.prototype;
    const fontOf = new WeakMap();
    V.hookMethod(C2D, "measureText", (orig, ctx, args) => {
      const m = orig.apply(ctx, args);
      try { fontOf.set(m, V.strHash(ctx.font + "\u0000" + String(args[0]))); } catch (_) {}
      return m;
    });
    for (const k of ["width", "actualBoundingBoxLeft", "actualBoundingBoxRight"]) {
      V.hookGetter(TM, k, (orig, m) => {
        const v = orig.call(m);
        const h = fontOf.get(m);
        if (h === undefined || !v) return v;
        return v * (1 + ((V.mix(h, k.length) % 21) - 10) * 2e-6);
      });
    }
  });
})();
