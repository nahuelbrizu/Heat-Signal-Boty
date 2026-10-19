// Render de gapList (views/mercado.js) en node con un DOM minimo: chips de clase por aviso.
// uso: node gap_list.mjs <mercado.js> '<json items>'  -> imprime JSON {summary, items:[{chip, cls, text}]}
import { pathToFileURL } from "node:url";

class Node {
  constructor() { this.childNodes = []; }
  append(...cs) { for (const c of cs) this.childNodes.push(c instanceof Node ? c : new Text(String(c))); }
  replaceChildren(...cs) { this.childNodes = []; this.append(...cs); }
  get textContent() { return this.childNodes.map((c) => c.textContent).join(""); }
}
class Text extends Node { constructor(t) { super(); this.data = t; } get textContent() { return this.data; } }
class Element extends Node {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.attributes = {}; this.style = {}; this.dataset = {}; this.className = ""; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  addEventListener() {}
}
globalThis.Node = Node;
globalThis.document = { createElement: (t) => new Element(t), createElementNS: (_n, t) => new Element(t),
  createTextNode: (t) => new Text(t), documentElement: new Element("html"), hidden: false, addEventListener() {} };
globalThis.window = { matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, LightweightCharts: null };
globalThis.location = { href: "http://panel/", protocol: "http:", hash: "" };
globalThis.localStorage = { getItem: () => null, setItem() {} };

const [view, json] = process.argv.slice(2);
const { gapList } = await import(pathToFileURL(view).href);
const d = gapList(JSON.parse(json));
const [summary, ul] = d.childNodes;
console.log(JSON.stringify({
  open: "open" in d.attributes, summary: summary.textContent,
  items: ul.childNodes.map((li) => ({ chip: li.childNodes[0].textContent, chipCls: li.childNodes[0].className, liCls: li.className, text: li.textContent })),
}));
