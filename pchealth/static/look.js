"use strict";
// Applies this device's chosen look before the page is drawn, so it never flashes the other one.
// "simple" (the default) is flat and still and costs the least to draw; "glass" adds the backdrop light,
// frosted panels and glass bars. The choice is made in Settings on the page and kept on this device only.
(() => {
  let look = "simple";
  try { if (localStorage.getItem("pch-look") === "glass") look = "glass"; } catch (e) { /* private mode: the default */ }
  document.documentElement.dataset.look = look;
})();
