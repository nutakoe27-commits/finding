(() => {
  let selected;
  try { selected = localStorage.getItem("matchlab:theme:v1"); } catch {}
  document.documentElement.dataset.theme = selected === "light" || selected === "dark" ? selected : matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
})();
