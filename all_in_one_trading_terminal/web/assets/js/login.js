// Login page: posts the password and returns to the requested page (same-origin paths only).

const form = document.getElementById("login-form");
const input = document.getElementById("password");
const errorBox = document.getElementById("login-error");
const submit = document.getElementById("login-submit");
const toggle = document.getElementById("pw-toggle");

function safeNext() {
  const next = new URLSearchParams(location.search).get("next") || "/";
  return next.startsWith("/") && !next.startsWith("//") && !next.includes("\\") ? next : "/";
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

toggle.addEventListener("click", () => {
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  toggle.setAttribute("aria-label", show ? "Hide password" : "Show password");
  input.focus();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = input.value;
  if (!password) {
    showError("Enter the password.");
    input.focus();
    return;
  }
  showError("");
  submit.classList.add("loading");
  submit.disabled = true;
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ password }),
    });
    let data = null;
    try { data = await response.json(); } catch { data = null; }
    if (response.ok && data?.ok) {
      location.replace(safeNext());
      return;
    }
    showError(data?.error ? `${data.error.charAt(0).toUpperCase()}${data.error.slice(1)}.` : `Sign-in failed (HTTP ${response.status}).`);
    input.select();
  } catch {
    showError("Cannot reach the terminal server — is it running?");
  } finally {
    submit.classList.remove("loading");
    submit.disabled = false;
  }
});
