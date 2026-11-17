// veilglass shroud: navigator. Reports the most common values for core count
// and memory instead of the machine's own.
(() => {
  const V = window.__veilglass_shroud__;
  if (!V) return;
  V.installers.push((w) => {
    const N = w.Navigator && w.Navigator.prototype;
    V.hookGetter(N, "hardwareConcurrency", () => 4);
    V.hookGetter(N, "deviceMemory", () => 8);
    const WN = w.WorkerNavigator && w.WorkerNavigator.prototype;
    V.hookGetter(WN, "hardwareConcurrency", () => 4);
  });
})();
