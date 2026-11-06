// 404 view rendered inside the app shell.

import { h, icon } from "../ui.js";

export async function mount(root) {
  root.append(h("div", { class: "notfound" },
    h("div", { class: "code" }, "404"),
    h("h2", { style: { margin: "0" } }, "This page doesn't exist"),
    h("p", { class: "muted", style: { margin: "0" } }, "The link may be outdated — head back to the dashboard."),
    h("a", { class: "btn btn-primary mt-12", href: "/dashboard", "data-link": "" }, icon("dashboard", 15), "Go to dashboard")));
}
