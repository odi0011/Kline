// Run with node; serves a fully isolated fixture, no App/hub and no disk build output.
import { build } from "esbuild"
import { createServer } from "node:http"
import { fileURLToPath } from "node:url"

const root = fileURLToPath(new URL("../../../../", import.meta.url))
const fixture = `
import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import NewsFeed from ${JSON.stringify(fileURLToPath(new URL("./news-feed.jsx", import.meta.url)))};
const makeItem = n => ({ url: "https://www.coindesk.com/news/" + n, title: "News " + n, ts: 1800000000000 + n * 1000, source: "CoinDesk", desc: "Summary " + n });
const state = window.newsFixture = { reads: [], analyses: [], makeItem };
const onRead = item => new Promise((resolve, reject) => state.reads.push({ item, resolve, reject }));
const onAnalyze = (item, mode, content) => new Promise((resolve, reject) => state.analyses.push({ item, mode, content, resolve, reject }));
function Fixture() {
  const [items, setItems] = useState(Array.from({length: 100}, (_, i) => makeItem(99 - i)));
  const [tasks, setTasks] = useState([]);
  state.setItems = value => flushSync(() => setItems(value));
  state.setTasks = value => flushSync(() => setTasks(value));
  return React.createElement(NewsFeed, { items, tasks, onRead, onAnalyze });
}
const root = createRoot(document.getElementById("fixture"));
state.unmount = () => flushSync(() => root.unmount());
flushSync(() => root.render(React.createElement(React.StrictMode, null, React.createElement(Fixture))));
const frame = () => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
const viewport = () => document.querySelector('[role="region"]');
const row = n => [...document.querySelectorAll('button[aria-haspopup="dialog"]')].find(el => el.title.endsWith('News ' + n));
const button = text => [...document.querySelectorAll('dialog button')].find(el => el.textContent === text);
const close = () => document.querySelector('button[aria-label="关闭新闻正文"]').click();
window.runNewsBrowserTests = async () => {
  const results = [];
  const check = (name, condition) => { if (!condition) throw new Error(name); results.push(name); };
  await frame();
  let node = viewport();
  check('initial layout is at bottom', Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) < 1);
  check('newest row is rendered at bottom', !!row(99));
  const ascending = Array.from({length: 100}, (_, i) => makeItem(i));
  state.setItems([...ascending, makeItem(100)]);
  await frame();
  check('new data follows bottom', Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) < 1);
  document.getElementById('fixture').style.height = '330px';
  await frame();
  check('resize follows bottom', Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) < 1);
  const beforeWheel = node.scrollTop;
  node.dispatchEvent(new WheelEvent('wheel', {deltaY: -30, bubbles: true}));
  state.setItems([...ascending, makeItem(100), makeItem(101)]);
  await frame();
  check('wheel intent wins a simultaneous data update', node.scrollTop === beforeWheel);
  node.scrollTop = 445;
  node.dispatchEvent(new Event('scroll', {bubbles: true}));
  await frame();
  check('scrolling up shows latest', [...document.querySelectorAll('button')].some(el => el.textContent.includes('最新')));
  state.setItems([...ascending, makeItem(100), makeItem(101)]);
  await frame();
  check('new data does not steal scroll', node.scrollTop === 445);
  document.getElementById('fixture').style.height = '180px';
  await frame();
  check('resize does not steal scroll', node.scrollTop === 445);
  state.setItems([...ascending.slice(1), makeItem(100), makeItem(101), makeItem(102)]);
  await frame();
  check('same-size rolling snapshot preserves anchor', node.scrollTop === 423);
  [...document.querySelectorAll('button')].find(el => el.textContent.includes('最新')).click();
  await frame();
  check('latest restores follow', Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop) < 1);
  const opener = row(102);
  opener.focus(); opener.click();
  await frame();
  check('reader is native modal in body portal', document.querySelector('body > dialog')?.matches(':modal'));
  check('reader locks document overflow', document.body.style.overflow === 'hidden' && document.documentElement.style.overflow === 'hidden');
  check('focus starts inside modal', document.activeElement?.getAttribute('aria-label') === '关闭新闻正文');
  check('reader displays loading', document.querySelector('dialog').textContent.includes('正在读取正文'));
  const oldRead = state.reads.at(-1);
  close(); await frame();
  check('closing restores opener focus', document.activeElement === opener);
  check('closing restores overflow', document.body.style.overflow === '' && document.documentElement.style.overflow === '');
  row(101).click(); await frame();
  const newRead = state.reads.at(-1);
  oldRead.resolve({ok: true, content: 'STALE BODY', partial: false});
  newRead.resolve({ok: true, content: '<img src=x onerror=alert(1)> NEW BODY', partial: true});
  await frame();
  check('stale reads do not overwrite another article', !document.querySelector('dialog').textContent.includes('STALE BODY'));
  check('body is text, never active markup', !document.querySelector('dialog img') && document.querySelector('dialog').textContent.includes('<img src=x'));
  check('partial notice is visible', document.querySelector('dialog').textContent.includes('并非全文'));
  button('AI 解读').click(); button('影响分析').click(); button('风险核查').click();
  await frame();
  check('three AI modes reach callback', state.analyses.map(x => x.mode).join(',') === 'interpret,impact,risk');
  check('AI receives partial context explicitly', state.analyses.every(x => x.content.startsWith('【仅摘要/部分正文')));
  close(); await frame();
  state.analyses[2].resolve({ok: true, id: 'risk-real'});
  state.analyses[0].resolve({ok: true, id: 'read-real'});
  state.analyses[1].resolve({ok: true, id: 'impact-real'});
  state.setTasks([{id: 'wrong', status: 'succeeded', result: 'WRONG RESULT'}, {id: 'read-real', status: 'running', progress: '2/3'}, {id: 'impact-real', status: 'succeeded', result: 'REAL IMPACT'}, {id: 'risk-real', status: 'failed', error: 'REAL ERROR'}]);
  await frame();
  row(101).click(); await frame();
  state.reads.at(-1).reject(new Error('READER OFFLINE'));
  await frame();
  let text = document.querySelector('dialog').textContent;
  check('closed-modal submissions retain task IDs', text.includes('read-real') && text.includes('impact-real') && text.includes('risk-real'));
  check('actual server progress/results/errors are displayed', text.includes('2/3') && text.includes('REAL IMPACT') && text.includes('REAL ERROR'));
  check('unrelated task result is excluded', !text.includes('WRONG RESULT'));
  check('read error falls back to labeled summary', text.includes('READER OFFLINE') && text.includes('Summary 101') && text.includes('并非全文'));
  state.setTasks([{id: 'read-real', status: 'succeeded', result: 'FINAL REAL RESULT'}]);
  await frame();
  check('live tasks update without rereading', document.querySelector('dialog').textContent.includes('FINAL REAL RESULT'));
  close(); await frame();
  check('all async operations leave no modal behind', !document.querySelector('dialog'));
  return results;
};
`
const bundle = await build({
  stdin: { contents: fixture, resolveDir: root, loader: "jsx" },
  bundle: true, write: false, format: "esm", jsx: "automatic", outfile: "news-test.js",
  alias: { "@": fileURLToPath(new URL("../../../", import.meta.url)) },
  define: { "process.env.NODE_ENV": '"development"' },
})
const js = bundle.outputFiles.find(file => file.path.endsWith(".js")).text
const css = bundle.outputFiles.find(file => file.path.endsWith(".css"))?.text || ""
const html = '<!doctype html><html><head><meta charset="UTF-8"><link rel="stylesheet" href="/fixture.css"><style>body{font-family:Arial;margin:30px}#fixture{display:flex;width:min(700px,90vw);height:220px;border:1px solid #ddd}</style></head><body><h1>Isolated news reader test</h1><div id="fixture"></div><script type="module" src="/fixture.js"></script></body></html>'
createServer((req, res) => {
  const type = req.url === "/fixture.js" ? "text/javascript" : req.url === "/fixture.css" ? "text/css" : "text/html"
  res.writeHead(200, { "Content-Type": type + "; charset=utf-8", "Cache-Control": "no-store" })
  res.end(req.url === "/fixture.js" ? js : req.url === "/fixture.css" ? css : html)
}).listen(5179, "127.0.0.1", () => console.log("News browser tests: http://127.0.0.1:5179/ (run window.runNewsBrowserTests())"))
