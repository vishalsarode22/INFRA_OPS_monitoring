"""
Review fix 5: IB.me() never caches a failed /api/me answer, a POST answered
401/403 refetches /api/me once and retries once, and an error report waits
for the CSRF token (or is skipped). Both copies: dashboard/static/shell.js
and the Classic view's helper in dashboard/static/index.html. Run in node
with a fake fetch.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def _shell_ib() -> str:
    src = open(os.path.join(ROOT, "dashboard", "static", "shell.js"), encoding="utf-8").read()
    return src[:src.index("async function _refreshScheduler")]


def _index_ib() -> str:
    src = open(os.path.join(ROOT, "dashboard", "static", "index.html"), encoding="utf-8").read()
    m = re.search(r"const IB = \{.*?\n\};\n", src, re.S)
    assert m
    return m.group(0)


HARNESS = r"""
const calls = [];
let meAnswers = [];          // queue of {status, body} for /api/me
let postAnswers = [];        // queue of statuses for the POST
globalThis.location = { pathname: "/x" };
globalThis.document = { getElementById: () => null, createElement: () => ({ style: {} }),
                        head: { appendChild() {} }, body: { appendChild() {} } };
globalThis.window = { addEventListener() {} };
globalThis.fetch = async (url, opts) => {
  calls.push({ url, method: (opts && opts.method) || "GET",
               csrf: opts && opts.headers && opts.headers["X-CSRF-Token"] });
  if (url === "/api/me") {
    const a = meAnswers.shift() || { status: 200, body: { csrf_token: "tok" } };
    if (a.throw) throw new Error("network");
    return { ok: a.status === 200, status: a.status, json: async () => a.body };
  }
  const st = postAnswers.shift() || 200;
  return { ok: st < 400, status: st, json: async () => ({ detail: "x" }), headers: { get: () => null } };
};
__IB__
if (IB.busy === undefined) IB.busy = { start() {}, stop() {} };
(async () => {
  const out = {};
  // 1. /api/me fails once (restart): nothing cached, the next call asks again.
  meAnswers = [{ status: 503, body: {} }];
  out.first = await IB.csrf();
  out.cachedAfterFail = IB._me;
  out.second = await IB.csrf();
  // 2. POST answered 401 (missing token) -> /api/me refetched, POST retried once.
  IB._me = { csrf_token: "stale" };
  calls.length = 0;
  postAnswers = [401, 200];
  meAnswers = [{ status: 200, body: { csrf_token: "fresh" } }];
  let r = await IB.post("/api/thing");
  out.retryStatus = r.status;
  out.retryCalls = calls.map(c => c.url + ":" + (c.csrf || ""));
  // 3. a 403 is retried once too, and only once.
  calls.length = 0;
  postAnswers = [403, 403];
  try { r = await IB.post("/api/thing"); out.twice = r.status; } catch (e) { out.twice = "threw"; }
  out.posts403 = calls.filter(c => c.url === "/api/thing").length;
  // 4. error report before /api/me loaded: waits for the token, or skips.
  if (IB.reportError) {
    IB._me = null; IB._reported = new Set(); calls.length = 0;
    meAnswers = [{ status: 200, body: { csrf_token: "tok2" } }];
    IB.reportError("JavaScriptError", "boom", "a.js:1", "");
    await new Promise(res => setTimeout(res, 20));
    out.report = calls.filter(c => c.url === "/api/errors/client").map(c => c.csrf);
    IB._me = null; calls.length = 0;
    meAnswers = [{ status: 401, body: {} }];
    IB.reportError("JavaScriptError", "boom2", "a.js:2", "");
    await new Promise(res => setTimeout(res, 20));
    out.reportSkipped = calls.filter(c => c.url === "/api/errors/client").length;
  }
  console.log(JSON.stringify(out));
})();
"""


def _run(ib_source: str) -> dict:
    script = HARNESS.replace("__IB__", ib_source)
    res = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("source", [_shell_ib, _index_ib], ids=["shell.js", "index.html"])
def test_me_failure_not_cached_and_post_retried_once(source):
    out = _run(source())
    assert out["first"] == "" and out["cachedAfterFail"] is None
    assert out["second"] == "tok"
    assert out["retryStatus"] == 200
    assert out["retryCalls"] == ["/api/thing:stale", "/api/me:", "/api/thing:fresh"]
    assert out["posts403"] == 2


def test_error_report_waits_for_the_token_or_skips():
    out = _run(_shell_ib())
    assert out["report"] == ["tok2"]
    assert out["reportSkipped"] == 0
