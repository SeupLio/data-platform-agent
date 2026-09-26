"""本地自测台：一个不依赖任何外部库的网页控制台。

存在的理由：面试/演示时「能点」比「能讲」有说服力。
它**不重写任何逻辑**，只把 CLI 后面的同一批函数包了层 HTTP，
所以页面上的数字和命令行必然一致（`tests/test_studio.py` 有差分测试盯着）。
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .agents import analyze, ask, govern
from .agents.tools import ToolRegistry
from .eval.harness import run_eval
from .eval.metrics import DIMENSION_LABELS
from .knowledge.index import load_index
from .warehouse.engine import Warehouse

PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>数据平台 Agent · 自测台</title>
<style>
body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:#f7f8fa;
color:#222;margin:0;padding:24px;}
.wrap{max-width:960px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#777;font-size:12px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e5e6eb;border-radius:8px;padding:14px 16px;margin-bottom:14px}
label{font-size:12px;color:#666;display:block;margin-bottom:6px}
input,textarea{width:100%;box-sizing:border-box;border:1px solid #d9d9d9;border-radius:6px;
padding:8px 10px;font-size:13px;font-family:inherit}
textarea{height:70px;resize:vertical}
button{background:#fb7299;color:#fff;border:0;border-radius:6px;padding:8px 16px;font-size:13px;
cursor:pointer;margin-right:8px}
button.alt{background:#fff;color:#fb7299;border:1px solid #fb7299}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
pre{background:#f2f3f5;border-radius:6px;padding:12px;font-size:12px;white-space:pre-wrap;
word-break:break-all;margin:10px 0 0}
.kind{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;margin-left:8px}
.k-answer{background:#e6f4ea;color:#1a7f37}
.k-clarify{background:#fff4e5;color:#b26a00}
.k-refuse{background:#fdeaea;color:#c1121f}
table{border-collapse:collapse;width:100%;font-size:12px;margin-top:10px}
th,td{border:1px solid #e5e6eb;padding:5px 8px;text-align:left}
th{background:#f2f3f5}
.muted{color:#888;font-size:12px}
</style></head><body><div class="wrap">
<h1>数据平台 Agent · 自测台</h1>
<div class="sub">三个 Agent（智能问答 / 自动分析 / 治理助手）跑在同一套知识库与离线数仓上；
页面上的数字与命令行完全一致（不重写逻辑，只包一层 HTTP）。</div>

<div class="card">
  <label>问个数（智能问答）</label>
  <input id="q1" value="最近7天播放量是多少">
  <div class="row">
    <button onclick="run('ask', document.getElementById('q1').value, 'o1')">提问</button>
    <button class="alt" onclick="document.getElementById('q1').value='留存率'">换成歧义问法</button>
    <button class="alt" onclick="document.getElementById('q1').value='帮我导出所有用户的手机号'">换成越权问法</button>
  </div>
  <pre id="o1">（点上面的按钮）</pre>
</div>

<div class="card">
  <label>问个原因（自动分析）</label>
  <input id="q2" value="2026-09-04 到 2026-09-10 播放量为什么降了">
  <div class="row">
    <button onclick="run('analyze', document.getElementById('q2').value, 'o2')">分析</button>
    <button class="alt" onclick="document.getElementById('q2').value='2026-09-10 到 2026-09-16 DAU 为什么涨了'">DAU 异动</button>
  </div>
  <pre id="o2">（点上面的按钮）</pre>
</div>

<div class="card">
  <label>查一段 SQL（治理助手）</label>
  <textarea id="q3">SELECT * FROM dws_video_daily</textarea>
  <div class="row">
    <button onclick="run('govern_sql', document.getElementById('q3').value, 'o3')">检查 SQL</button>
    <button class="alt" onclick="document.getElementById('q3').value='SELECT up_id FROM dws_up_daily'">换成敏感字段</button>
  </div>
  <pre id="o3">（点上面的按钮）</pre>
</div>

<div class="card">
  <label>跑一遍评测（143 条用例 · 离线 · 不调模型）</label>
  <div class="row"><button onclick="runEval()">开始跑</button></div>
  <div id="o4" class="muted">（大约几秒）</div>
</div>

<script>
const LABEL = %LABELS%;
async function run(mode, value, out){
  const el = document.getElementById(out);
  el.textContent = '…';
  const r = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({mode, value})});
  const j = await r.json();
  el.innerHTML = '<span class="kind k-'+j.kind+'">'+j.kind+'</span>' + escapeHtml(j.text);
}
async function runEval(){
  const el = document.getElementById('o4');
  el.textContent = '跑批中…';
  const r = await fetch('/api/eval', {method:'POST'});
  const j = await r.json();
  let rows = '';
  for (const [k,v] of Object.entries(j.dimension_means)) {
    rows += '<tr><td>'+(LABEL[k]||k)+'</td><td>'+v.toFixed(4)+'</td></tr>';
  }
  let kinds = '';
  for (const [k,v] of Object.entries(j.by_kind)) {
    kinds += '<tr><td>'+k+'</td><td>'+v.count+'</td><td>'+v.overall.toFixed(4)+'</td></tr>';
  }
  el.innerHTML = '<b>综合 '+j.overall.toFixed(4)+' · 用例 '+j.total+' 条</b>'
    + '<table>'+rows+'</table><table><tr><th>类型</th><th>条数</th><th>综合</th></tr>'+kinds+'</table>';
}
function escapeHtml(s){return (s||'').replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
</script>
</div></body></html>
"""


def _build_state() -> dict[str, Any]:
    index = load_index()
    wh = Warehouse(index=index)
    return {"index": index, "wh": wh, "registry": ToolRegistry(index, wh), "today": wh.today}


class _State:
    data: dict[str, Any] | None = None
    lock = threading.Lock()

    @classmethod
    def get(cls) -> dict[str, Any]:
        with cls.lock:
            if cls.data is None:
                cls.data = _build_state()
            return cls.data


def _dispatch(mode: str, value: str) -> dict[str, Any]:
    st = _State.get()
    if mode == "ask":
        res = ask(value, st["registry"], st["today"])
    elif mode == "analyze":
        res = analyze(value, st["registry"], st["today"], max_steps=8)
    elif mode == "govern_sql":
        res = govern(value, st["registry"], kind="sql")
    elif mode == "govern_table":
        res = govern(value, st["registry"], kind="table")
    else:
        return {"kind": "error", "text": f"未知模式 {mode}"}
    return {"kind": res.kind, "text": res.text, "data": res.data}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 安静一点
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/stats"):
            st = _State.get()
            self._send(
                json.dumps(
                    {"kb": st["index"].kb.summary(), "today": st["today"]}, ensure_ascii=False
                ).encode("utf-8"),
                "application/json",
            )
            return
        page = PAGE.replace("%LABELS%", json.dumps(DIMENSION_LABELS, ensure_ascii=False))
        self._send(page.encode("utf-8"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
        if self.path == "/api/eval":
            _, summary = run_eval(progress=False)
            self._send(json.dumps(summary.to_dict(), ensure_ascii=False).encode("utf-8"),
                       "application/json")
            return
        mode = payload.get("mode", "ask")
        value = payload.get("value", "")
        try:
            out = _dispatch(mode, value)
        except Exception as exc:  # noqa: BLE001
            out = {"kind": "error", "text": f"{type(exc).__name__}: {exc}"}
        self._send(json.dumps(out, ensure_ascii=False).encode("utf-8"), "application/json")


def serve(port: int = 8765, open_browser: bool = True) -> int:
    _State.get()  # 预热（建库 + 装知识库），避免第一次点按钮卡住
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"自测台已启动：{url}  （Ctrl+C 退出）")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
