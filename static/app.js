document.addEventListener("DOMContentLoaded", () => {
  const menu = document.getElementById("mobileMenu");
  const sidebar = document.getElementById("sidebar");
  if (menu && sidebar) menu.addEventListener("click", () => sidebar.classList.toggle("open"));
  document.addEventListener("click", (e) => {
    if (sidebar && sidebar.classList.contains("open") && !sidebar.contains(e.target) && !e.target.closest("#mobileMenu")) sidebar.classList.remove("open");
  });
});