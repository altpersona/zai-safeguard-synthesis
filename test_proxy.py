"""Offline test suite for zai-rewrite-proxy safeguard synthesis.

Run from the repo root:  python3 test_proxy.py
Covers: safeguards detection, model rewrite, non-streaming body injection,
SSE event rewriting under awkward chunk splits, text-only turns, and the
never-overwrite-a-real-verdict rule.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "zrp", os.path.join(HERE, "zai-rewrite-proxy.py"))
zrp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zrp)

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# --- rewrite_model: sg flag + model rewrite
b, sg = zrp.rewrite_model(json.dumps({
    "model": "claude-sonnet-4-6[1m]",
    "messages": [],
    "safeguards": [{"type": "dangerous_tool_use", "classifier_context": "x"}],
}).encode())
check("sg detected", sg)
check("model rewritten", json.loads(b)["model"] == "glm-5.3")

b, sg = zrp.rewrite_model(json.dumps({"model": "claude-haiku-4-5", "messages": []}).encode())
check("no sg when absent", not sg)
check("haiku rewritten", json.loads(b)["model"] == "glm-4.7")

# --- inject_body (non-streaming)
body = json.dumps({"type": "message", "content": [
    {"type": "text", "text": "hi"},
    {"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {}}]})
out, n = zrp.inject_body(body.encode())
o = json.loads(out)
check("body ids", n == 1)
check("body top-level field", o["safeguard_results"][0]["type"] == "dangerous_tool_use")
st = o["safeguard_results"][0]["status"]
check("body status available", st["type"] == "available")
check("body tool map", st["tool_uses"]["toolu_01"] == {"type": "evaluated", "outcome": "not_flagged"})

err = json.dumps({"type": "error", "error": {"message": "x"}}).encode()
check("error body untouched", zrp.inject_body(err)[0] == err)
done = json.dumps({"type": "message", "content": [], "safeguard_results": [{"x": 1}]}).encode()
check("existing results untouched", zrp.inject_body(done)[0] == done)

# --- SSE rewriter
sse = (b"event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {"role": "assistant", "content": []}}).encode() + b"\n\n"
       + b"event: content_block_start\ndata: " + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}).encode() + b"\n\n"
       + b"event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "run it"}}).encode() + b"\n\n"
       + b"event: content_block_start\ndata: " + json.dumps({"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "toolu_02", "name": "Bash", "input": {}}}).encode() + b"\n\n"
       + b"event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"c\""}}).encode() + b"\n\n"
       + b"event: content_block_stop\ndata: " + json.dumps({"type": "content_block_stop", "index": 1}).encode() + b"\n\n"
       + b"event: message_delta\ndata: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 12}}).encode() + b"\n\n"
       + b"event: message_stop\ndata: " + json.dumps({"type": "message_stop"}).encode() + b"\n\n")

feed, finish = zrp.make_stream_rewriter(lambda n, t: None)
out_events = []
for i in range(0, len(sse), 7):          # awkward chunk splits on purpose
    out_events += feed(sse[i:i + 7])
out_events += finish()
joined = b"".join(out_events)

delta = [json.loads(l[5:]) for l in joined.split(b"\n") if l.startswith(b"data: ")]
md = [d for d in delta if isinstance(d, dict) and d.get("type") == "message_delta"]
check("one message_delta", len(md) == 1)
sgr = md[0]["delta"]["safeguard_results"][0]
check("sse verdict type", sgr["type"] == "dangerous_tool_use")
check("sse id collected", sgr["status"]["tool_uses"]["toolu_02"]["outcome"] == "not_flagged")
check("sse stop_reason kept", md[0]["delta"]["stop_reason"] == "tool_use")
starts = [d for d in delta if d.get("type") == "content_block_start"]
check("block starts unchanged", len(starts) == 2 and starts[1]["content_block"]["id"] == "toolu_02")
check("event line preserved", b"event: message_delta" in joined)

# text-only stream -> empty tool_uses map (must still answer)
sse2 = (b"event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {"role": "assistant", "content": []}}).encode() + b"\n\n"
        + b"event: message_delta\ndata: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}}).encode() + b"\n\n")
feed, finish = zrp.make_stream_rewriter(lambda n, t: None)
evs = feed(sse2) + finish()
d = [json.loads(l[5:]) for l in b"".join(evs).split(b"\n") if l.startswith(b"data: ")]
md2 = [x for x in d if x.get("type") == "message_delta"][0]
check("text-only gets empty map", md2["delta"]["safeguard_results"][0]["status"]["tool_uses"] == {})

# pre-existing server verdict never overwritten
sse3 = (b"event: message_delta\ndata: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn", "safeguard_results": [{"type": "dangerous_tool_use", "status": {"type": "unsupported"}}]}}).encode() + b"\n\n")
feed, finish = zrp.make_stream_rewriter(lambda n, t: None)
evs = feed(sse3) + finish()
d3 = json.loads([l for l in b"".join(evs).split(b"\n") if l.startswith(b"data: ")][0][5:])
check("real verdict untouched", d3["delta"]["safeguard_results"][0]["status"]["type"] == "unsupported")

print(f"\n{len(fails)} failures" if fails else "\nALL PASS")
sys.exit(1 if fails else 0)
