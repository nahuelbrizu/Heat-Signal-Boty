// Render basico de una vista de la SPA en node, con un DOM minimo (sin jsdom ni red).
// uso: node render_view.mjs <vista.js> <datos.json>   -> imprime JSON {overview, detail, tags}
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const created = [];
class Node {
  constructor() { this.childNodes = []; this.parentNode = null; }
  append(...cs) { for (const c of cs) { const n = c instanceof Node ? c : new Text(String(c)); n.parentNode = this; this.childNodes.push(n); } }
  replaceChildren(...cs) { this.childNodes = []; this.append(...cs); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
  set textContent(v) { this.childNodes = [new Text(String(v))]; }
}
class Text extends Node { constructor(t) { super(); this.data = t; } get textContent() { return this.data; } }
class Element extends Node {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.attributes = {}; this.style = {}; this.dataset = {}; this.className = ""; created.push(tag); }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  addEventListener() {}
  closest() { return null; }
  get classList() { const self = this; return { add: (c) => { self.className += " " + c; }, remove() {}, toggle() { return false; }, contains: (c) => self.className.split(" ").includes(c) }; }
}
globalThis.Node = Node;
globalThis.document = {
  createElement: (t) => new Element(t), createElementNS: (_ns, t) => new Element(t), createTextNode: (t) => new Text(t),
  documentElement: new Element("html"), hidden: false,
};
globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, LightweightCharts: null };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#888" });
globalThis.location = { href: "http://panel/", protocol: "http:", hash: "" };
globalThis.requestAnimationFrame = (f) => f();

const [view, dataPath] = process.argv.slice(2);
const mod = await import(pathToFileURL(view).href);
const data = JSON.parse(readFileSync(dataPath, "utf8"));
const href = (n, parts = []) => "#/" + [n, ...parts].join("/");
const ovNodes = mod.renderOverview(data.overview, href, null);
const holder = new Element("div");
holder.append(...ovNodes);
const det = mod.renderDetail(data.detail);
process.stdout.write(JSON.stringify({ overview: holder.textContent, detail: det.textContent, tags: [...new Set(created)] }));
