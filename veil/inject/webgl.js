// veilglass shroud: webgl. Blurs the exact GPU model to its family and adds
// faint noise to pixel reads. Firefox already generalises the renderer string,
// so there only the pixel noise applies.
(() => {
  const V = window.__veilglass_shroud__;
  if (!V) return;
  const UNMASKED_VENDOR = 0x9245, UNMASKED_RENDERER = 0x9246;

  // Keep vendor and API in the string, drop the model, so the claim still
  // agrees with the extensions and limits the card really reports.
  function blur(renderer) {
    if (typeof renderer !== "string") return renderer;
    const fam = [
      [/intel/i, "Intel(R) HD Graphics"],
      [/nvidia|geforce|quadro|rtx/i, "NVIDIA GeForce GTX 1060"],
      [/amd|radeon|ati /i, "AMD Radeon Graphics"],
      [/apple/i, "Apple GPU"],
      [/mali/i, "Mali-G Series"],
      [/adreno/i, "Adreno (TM) 600 Series"],
    ].find(([re]) => re.test(renderer));
    if (!fam) return renderer;
    const m = renderer.match(/^ANGLE \(([^,]+),\s*([^,]+?)(\s*\(0x[0-9a-f]+\))?\s*(Direct3D[^,]*|OpenGL[^,]*|Vulkan[^,]*|Metal[^,]*)?,\s*(.*)\)$/i);
    if (m) return `ANGLE (${m[1]}, ${fam[1]}${m[4] ? " " + m[4].trim() : ""}, ${m[5]})`;
    return fam[1];
  }

  V.installers.push((w) => {
    for (const C of [w.WebGLRenderingContext, w.WebGL2RenderingContext]) {
      if (!C) continue;
      const proto = C.prototype;
      if (!V.isFirefox) {
        V.hookMethod(proto, "getParameter", (orig, gl, args) => {
          const v = orig.apply(gl, args);
          return args[0] === UNMASKED_RENDERER ? blur(v) : v;
        });
      }
      V.hookMethod(proto, "readPixels", (orig, gl, args) => {
        const r = orig.apply(gl, args);
        const out = args[6];
        if (out && out.BYTES_PER_ELEMENT === 1 && out.length <= 8e6) V.noisePixels(out, args[2]);
        return r;
      });
    }
  });
})();
