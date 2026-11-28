// The loading screen. The app moves the window to the engine's page once it answers,
// or back here with ?error=... if it couldn't start.
const error = new URLSearchParams(location.search).get("error");
if (error) {
  document.getElementById("msg").textContent = error;
  document.getElementById("msg").className = "bad";
  document.getElementById("hint").hidden = false;
}
