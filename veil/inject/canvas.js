// veilglass shroud: canvas. 2D and WebGL canvas reads come back with a faint,
// per-page noise, so the image hash no longer follows you between sites.
(() => {
  const V = window.__veilglass_shroud__;
  if (!V) return;
  const BIG = 2_000_000; // leave large canvases (editors, games) untouched

  V.installers.push((w) => {
    const C2D = w.CanvasRenderingContext2D && w.CanvasRenderingContext2D.prototype;
    const OC2D = w.OffscreenCanvasRenderingContext2D && w.OffscreenCanvasRenderingContext2D.prototype;
    const rawGetImageData = C2D && C2D.getImageData;

    for (const proto of [C2D, OC2D]) {
      V.hookMethod(proto, "getImageData", (orig, ctx, args) => {
        const img = orig.apply(ctx, args);
        if (img.width * img.height <= BIG) V.noisePixels(img.data, img.width);
        return img;
      });
    }

    // Draw the canvas onto a scratch 2D canvas, noise it, and export that.
    function shrouded(canvas) {
      const wd = canvas.width, ht = canvas.height;
      if (!wd || !ht || wd * ht > BIG) return null;
      const doc = w.document;
      const copy = doc.createElement("canvas");
      copy.width = wd; copy.height = ht;
      const cx = copy.getContext("2d");
      cx.drawImage(canvas, 0, 0);
      const img = rawGetImageData.call(cx, 0, 0, wd, ht); // throws if tainted
      V.noisePixels(img.data, wd);
      cx.putImageData(img, 0, 0);
      return copy;
    }

    const HC = w.HTMLCanvasElement && w.HTMLCanvasElement.prototype;
    for (const name of ["toDataURL", "toBlob"]) {
      V.hookMethod(HC, name, (orig, canvas, args) => {
        let copy = null;
        try { copy = shrouded(canvas); } catch (_) { copy = null; }
        return orig.apply(copy || canvas, args); // tainted/huge: native behaviour
      });
    }

    const OC = w.OffscreenCanvas && w.OffscreenCanvas.prototype;
    V.hookMethod(OC, "convertToBlob", (orig, canvas, args) => {
      try {
        const wd = canvas.width, ht = canvas.height;
        if (wd && ht && wd * ht <= BIG) {
          const copy = new w.OffscreenCanvas(wd, ht);
          const cx = copy.getContext("2d");
          cx.drawImage(canvas, 0, 0);
          const img = cx.getImageData(0, 0, wd, ht); // hooked: already noised
          cx.putImageData(img, 0, 0);
          return orig.apply(copy, args);
        }
      } catch (_) {}
      return orig.apply(canvas, args);
    });
  });
})();
