#!/usr/bin/env python3
"""只读诊断 OCR JSONL；不请求模型、不重试、不裁决合入门禁。"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


def failure_kind(reason):
    text = reason.lower()
    if "compression" in text and "threshold" in text:
        return "CONTEXT_COMPRESSION"
    if "deadline" in text or "timed out" in text or "timeout" in text:
        return "DEADLINE_OR_TIMEOUT"
    if "budget" in text:
        return "TOKEN_BUDGET"
    if "resume rejected" in text or "identity changed" in text:
        return "RESUME_IDENTITY"
    if "cancel" in text:
        return "CANCELLED_CAUSE_UNPROVEN"
    return "UNKNOWN"


def item_key(item):
    # 同一路径的不同版本不能相互补齐覆盖。
    return (item.get("item_id") or item.get("path"), item.get("fingerprint"))


def diagnose(path):
    events = []
    parse_errors = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            events.append(event)
        except (ValueError, json.JSONDecodeError):
            # 在途文件最后一行可能尚未写完；仍不推断进程已经退出。
            parse_errors.append({"line": number, "last_line": number == len(lines)})

    ends = [e for e in events if e.get("type") == "session_end"]
    manifest = ends[-1].get("run_manifest") if ends else None
    manifest = manifest if isinstance(manifest, dict) else {}
    coverage = manifest.get("coverage", {})
    groups = {k: coverage.get(k, []) for k in
              ("selected", "completed", "reused", "failed", "waived")}
    selected = {item_key(i) for i in groups["selected"]}
    covered = {item_key(i) for i in groups["completed"] + groups["reused"]}
    remaining = [i.get("path") for i in groups["selected"] if item_key(i) not in covered]
    identities_valid = all(i.get("path") and i.get("fingerprint")
                           for i in groups["selected"] + groups["completed"] + groups["reused"])
    session_ids = sorted({e["sessionId"] for e in events if e.get("sessionId")})
    scope_complete = bool(
        manifest.get("schema_version") == "ocr.run-manifest/v1"
        and manifest.get("terminal_state") == "complete"
        and selected and identities_valid and not remaining
        and len(selected) == len(groups["selected"])
        and covered <= selected and not groups["failed"] and not groups["waived"]
        and not parse_errors and len(session_ids) == 1
    )
    failures = [{"path": i.get("path"), "classification": i.get("classification"),
                 "reason": i.get("reason", ""),
                 "diagnosis": failure_kind(i.get("reason", ""))}
                for i in groups["failed"]]
    # item_failed 的 round 文案只是症状；manifest 的 reason 优先。
    item_errors = Counter(e.get("error", "") for e in events
                          if e.get("type") == "review_item_failed")
    llm_errors = Counter((e.get("taskType"), e.get("error", "")) for e in events
                         if e.get("type") == "llm_error")
    tools = Counter()
    request_sizes = []
    usages = []
    for e in events:
        if e.get("type") == "tool_call":
            args = e.get("arguments", "")
            try:
                obj = json.loads(args) if isinstance(args, str) else args
                canonical = json.dumps(obj, sort_keys=True, ensure_ascii=False)
            except (ValueError, TypeError):
                canonical = str(args)
            digest = hashlib.sha256(canonical.encode()).hexdigest()[:16]
            tools[(e.get("filePath"), e.get("tool_name"), digest)] += 1
        if e.get("type") == "llm_request":
            messages = e.get("messages", [])
            sizes = [len(str(m.get("content") or "")) for m in messages]
            request_sizes.append({"task": e.get("taskType"), "characters": sum(sizes),
                                  "frozen_first_two_characters": sum(sizes[:2])})
        if e.get("type") == "llm_response" and isinstance(e.get("usage"), dict):
            usages.append(e["usage"])
    advice = []
    if not manifest or not ends:
        advice.append("没有完整session_end/manifest；先核对实际PID及start identity，不能判停止或通过。")
    if parse_errors:
        advice.append("日志含不完整或非法JSON行；覆盖结果不完整。末行可能仍在写入，请稍后重读。")
    kinds = {i["diagnosis"] for i in failures}
    if "CONTEXT_COMPRESSION" in kinds:
        advice.append("调整真实输入与有效上下文空间；max-tokens不会拆单文件，增超时/low/resume不消除压缩根因。")
    if "DEADLINE_OR_TIMEOUT" in kinds:
        advice.append("区分任务截止与单请求超时，并核对同窗服务端证据；不凭此认定网络或排队。")
    if "TOKEN_BUDGET" in kinds:
        advice.append("退出0仍可能部分失败；保留失败范围，仅复用身份匹配的有效文件覆盖。")
    if any("cancel" in error.lower() for _, error in llm_errors):
        advice.append("context canceled可能由客户端压缩/退出触发；需额外进程或服务端证据才能归因。")
    if any(count > 1 for count in tools.values()):
        advice.append("存在相同参数的重复工具调用；核对是否必要，不直接判模型无能力。")
    if scope_complete:
        advice.append("本session文件覆盖完整；仍须核对批次base/head、排除/跨文件覆盖、发现裁决及测试，不能自动PASS。")
    elif manifest:
        advice.append("文件覆盖不完整或终态不成功；零comments不能放行。")
    return {
        "session_file": str(path.resolve()), "session_ids": session_ids,
        "last_event": ({k: events[-1].get(k) for k in ("type", "timestamp")} if events else None),
        "terminal_state": manifest.get("terminal_state"),
        "manifest_schema": manifest.get("schema_version"),
        "input": manifest.get("input", {}),
        "ocr_version": manifest.get("execution", {}).get("ocr_version"),
        "coverage_counts": {k: len(v) for k, v in groups.items()},
        "coverage_complete": scope_complete, "remaining_paths": remaining,
        "gate_decision": "NOT_EVALUATED", "parse_errors": parse_errors,
        "manifest_failures": failures, "item_error_messages": dict(item_errors),
        "llm_errors": [{"task": task, "error": error, "count": count}
                       for (task, error), count in llm_errors.items()],
        "repeated_tools": [{"file": file, "tool": tool, "arguments_sha256_prefix": digest, "count": n}
                           for (file, tool, digest), n in tools.items() if n > 1],
        "request_characters": {
            "count": len(request_sizes),
            "max": max((s["characters"] for s in request_sizes), default=0),
            "max_frozen_first_two": max((s["frozen_first_two_characters"] for s in request_sizes), default=0),
            "note": "字符统计只用于观察增长，不是OCR本地token估计或模型上下文长度。",
        },
        "model_usage": {
            "responses": len(usages),
            "total_prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usages),
            "total_completion_tokens": sum(u.get("completion_tokens", 0) for u in usages),
            "max_prompt_tokens": max((u.get("prompt_tokens", 0) for u in usages), default=0),
        },
        "next_steps": advice,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sessions", type=Path, nargs="+", help="一个或多个OCR session JSONL路径")
    args = parser.parse_args()
    results = []
    for path in args.sessions:
        try:
            results.append(diagnose(path))
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # 不回显原始prompt或工具结果。
            results.append({"session_file": str(path), "inspection_error": type(exc).__name__,
                            "coverage_complete": False, "gate_decision": "NOT_EVALUATED"})
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 2 if any(r.get("inspection_error") or r.get("parse_errors") for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
