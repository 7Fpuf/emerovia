/* pwa.js — Emerovia observer PWA helpers.
 *
 * Read-only: viewport sizing and service-worker registration only.
 * Pinch-to-zoom lives natively in index.html (see CHANGES.md); this file
 * never touches world state.
 */
(function () {
"use strict";

/* iOS Safari reports 100vh including the area under the browser chrome.
 * Expose the real visible height as --pwa-vh for the layout. */
function setVH() {
  var vh = window.innerHeight * 0.01;
  document.documentElement.style.setProperty("--pwa-vh", vh + "px");
}
setVH();
window.addEventListener("resize", setVH);
window.addEventListener("orientationchange", setVH);
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", setVH);
}

/* App-shell service worker. API traffic always bypasses the cache (see sw.js). */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js").catch(function () {
      /* registration failure is non-fatal: the site still works online */
    });
  });
}
})();
