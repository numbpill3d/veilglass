// veilglass shroud: battery. Every machine looks plugged in and full.
(() => {
  const V = window.__veilglass_shroud__;
  if (!V) return;
  V.installers.push((w) => {
    const B = w.BatteryManager && w.BatteryManager.prototype;
    V.hookGetter(B, "charging", () => true);
    V.hookGetter(B, "level", () => 1);
    V.hookGetter(B, "chargingTime", () => 0);
    V.hookGetter(B, "dischargingTime", () => Infinity);
  });
})();
