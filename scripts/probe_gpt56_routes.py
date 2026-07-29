from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any

import httpx


LEVELS = ("instant", "medium", "high", "xhigh", "pro")


def payload(level: str, marker: str, *, stream: bool = False) -> dict[str, Any]:
    return {
        "model": "gpt-5-web",
        "messages": [{"role": "user", "content": f"只回复：{marker}"}],
        "stream": stream,
        "turtle_model_version": "gpt-5-5" if level == "instant" else "latest",
        "turtle_thinking_level": level,
    }


def nonstream_probe(client: httpx.Client, level: str) -> dict[str, Any]:
    marker = f"GPT56_{level.upper()}_OK"
    started = time.perf_counter()
    response = client.post("/chat/completions", json=payload(level, marker))
    elapsed_ms = (time.perf_counter() - started) * 1000
    content = ""
    if response.is_success:
        body = response.json()
        choices = body.get("choices") or []
        if choices:
            content = str(choices[0].get("message", {}).get("content") or "")
    return {
        "level": level,
        "http_status": response.status_code,
        "total_ms": round(elapsed_ms, 1),
        "marker_matched": marker in content,
        "content_preview": content[:80],
    }


def stream_probe(client: httpx.Client, level: str = "xhigh") -> dict[str, Any]:
    marker = f"GPT56_{level.upper()}_SSE_OK"
    started = time.perf_counter()
    first_content_ms: float | None = None
    pieces: list[str] = []
    event_count = 0
    done = False
    status = 0
    with client.stream("POST", "/chat/completions", json=payload(level, marker, stream=True)) as response:
        status = response.status_code
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                done = True
                continue
            event_count += 1
            event = json.loads(data)
            choices = event.get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            content = delta.get("content")
            if content:
                if first_content_ms is None:
                    first_content_ms = (time.perf_counter() - started) * 1000
                pieces.append(str(content))
    content = "".join(pieces)
    return {
        "level": level,
        "http_status": status,
        "ttft_ms": round(first_content_ms, 1) if first_content_ms is not None else None,
        "total_ms": round((time.perf_counter() - started) * 1000, 1),
        "events": event_count,
        "done_marker": done,
        "marker_matched": marker in content,
        "content_preview": content[:80],
    }


def multi_turn_probe(client: httpx.Client) -> dict[str, Any]:
    marker = "GPT56_HISTORY_7319"
    first_payload = payload("medium", "已记住")
    first_payload["messages"] = [
        {"role": "user", "content": f"请记住代号 {marker}，只回复：已记住"}
    ]
    first_response = client.post("/chat/completions", json=first_payload)
    first_content = ""
    if first_response.is_success:
        first_choices = first_response.json().get("choices") or []
        if first_choices:
            first_content = str(first_choices[0].get("message", {}).get("content") or "")

    second_payload = payload("medium", marker)
    second_payload["messages"] = [
        first_payload["messages"][0],
        {"role": "assistant", "content": first_content},
        {"role": "user", "content": "只回复我刚才让你记住的代号"},
    ]
    second_response = client.post("/chat/completions", json=second_payload)
    second_content = ""
    if second_response.is_success:
        second_choices = second_response.json().get("choices") or []
        if second_choices:
            second_content = str(second_choices[0].get("message", {}).get("content") or "")

    return {
        "level": "medium",
        "first_http_status": first_response.status_code,
        "second_http_status": second_response.status_code,
        "first_acknowledged": "已记住" in first_content,
        "history_marker_matched": marker in second_content,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run sanitized GPT-5.6 web-route acceptance probes")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--levels", nargs="+", choices=LEVELS, default=list(LEVELS))
    parser.add_argument("--stream-level", choices=LEVELS, default="xhigh")
    args = parser.parse_args()

    key = os.environ.get("GATEWAY_API_KEY", "")
    if not key:
        raise SystemExit("GATEWAY_API_KEY is required; it is never printed")

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(900.0, connect=20.0),
    ) as client:
        report = {
            "model_family": "gpt-5-web",
            "version": "latest",
            "nonstream": [nonstream_probe(client, level) for level in args.levels],
            "stream": stream_probe(client, args.stream_level),
            "multi_turn": multi_turn_probe(client),
        }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
