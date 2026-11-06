// Applies the saved theme before first paint (external file — the CSP forbids inline scripts).
(function () {
  var theme = "dark";
  var density = "comfortable";
  try {
    var prefs = JSON.parse(localStorage.getItem("qst.prefs.v2") || "{}");
    if (prefs.theme === "light" || prefs.theme === "dark") theme = prefs.theme;
    else if (prefs.theme === "system") theme = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
    if (prefs.density === "compact") density = "compact";
  } catch (e) { /* storage blocked */ }
  document.documentElement.setAttribute("data-theme", theme);
  document.documentElement.setAttribute("data-density", density);
})();
