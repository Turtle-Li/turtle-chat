from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import struct
import time
import zlib
from typing import Any

import httpx


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def red_png_data_url(size: int = 16) -> str:
    rows = b"".join(b"\x00" + (b"\xff\x00\x00" * size) for _ in range(size))
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(rows))
        + _png_chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def request_completion(
    client: httpx.Client,
    model: str,
    messages: list[dict[str, Any]],
) -> tuple[httpx.Response, float]:
    started = time.perf_counter()
    response = client.post(
        "/chat/completions",
        json={"model": model, "messages": messages, "stream": False},
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    return response, elapsed_ms


def content_from(response: httpx.Response) -> str:
    payload = response.json()
    return str(payload["choices"][0]["message"]["content"])


def streaming_probe(client: httpx.Client, model: str) -> dict[str, Any]:
    started = time.perf_counter()
    first_content_ms: float | None = None
    pieces: list[str] = []
    done = False
    status = 0
    with client.stream(
        "POST",
        "/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "请用一句简短中文确认流式输出正常。"}],
            "stream": True,
        },
    ) as response:
        status = response.status_code
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                done = True
                continue
            event = json.loads(data)
            choices = event.get("choices") or []
            delta = choices[0].get("delta", {}) if choices else {}
            content = delta.get("content")
            if content:
                if first_content_ms is None:
                    first_content_ms = (time.perf_counter() - started) * 1000
                pieces.append(str(content))
    total_ms = (time.perf_counter() - started) * 1000
    return {
        "http_status": status,
        "ttft_ms": round(first_content_ms, 1) if first_content_ms is not None else None,
        "total_ms": round(total_ms, 1),
        "done_marker": done,
        "content_chars": len("".join(pieces)),
        "content_preview": "".join(pieces)[:80],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run sanitized real-upstream Gateway probes")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--model", default="gpt-5-web")
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()

    key = os.environ.get("GATEWAY_API_KEY", "")
    if not key:
        raise SystemExit("GATEWAY_API_KEY is required; it is never printed")

    report: dict[str, Any] = {
        "real_upstream": True,
        "base_url": args.base_url,
        "model": args.model,
    }
    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {key}"},
        timeout=httpx.Timeout(900.0, connect=20.0),
    ) as client:
        health = client.get(args.base_url.removesuffix("/v1") + "/healthz")
        models = client.get("/models")
        report["health"] = health.json()
        report["models"] = [item["id"] for item in models.json().get("data", [])]

        short_response, short_ms = request_completion(
            client,
            args.model,
            [{"role": "user", "content": "只回复：真实网关成功"}],
        )
        report["short_text"] = {
            "http_status": short_response.status_code,
            "total_ms": round(short_ms, 1),
            "content_preview": content_from(short_response)[:80],
        }

        long_response, long_ms = request_completion(
            client,
            args.model,
            [{"role": "user", "content": "写一段约500个中文字符的连贯文字，主题是可靠的软件验证。"}],
        )
        long_content = content_from(long_response)
        report["long_text"] = {
            "http_status": long_response.status_code,
            "total_ms": round(long_ms, 1),
            "content_chars": len(long_content),
        }

        repeat_timings: list[float] = []
        repeat_success = 0
        for index in range(args.repeat):
            response, elapsed_ms = request_completion(
                client,
                args.model,
                [{"role": "user", "content": f"只回复 OK-{index + 1}"}],
            )
            repeat_timings.append(elapsed_ms)
            if response.status_code == 200 and f"OK-{index + 1}" in content_from(response):
                repeat_success += 1
        report["repeated_requests"] = {
            "attempts": args.repeat,
            "successes": repeat_success,
            "success_rate": round(repeat_success / args.repeat, 4) if args.repeat else None,
            "latency_ms": [round(value, 1) for value in repeat_timings],
            "mean_ms": round(statistics.mean(repeat_timings), 1) if repeat_timings else None,
        }

        report["streaming"] = streaming_probe(client, args.model)

        multi_response, multi_ms = request_completion(
            client,
            args.model,
            [
                {"role": "user", "content": "请记住我的名字是海棠。"},
                {"role": "assistant", "content": "好的，我会记住。"},
                {"role": "user", "content": "我的名字是什么？只回答名字。"},
            ],
        )
        multi_content = content_from(multi_response)
        report["multi_turn_full_history"] = {
            "http_status": multi_response.status_code,
            "total_ms": round(multi_ms, 1),
            "recalled": "海棠" in multi_content,
            "content_preview": multi_content[:80],
            "mechanism": "client-supplied full messages history",
        }

        image_response, image_ms = request_completion(
            client,
            args.model,
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "图片的主色是什么？只回答一个中文颜色词。"},
                        {"type": "image_url", "image_url": {"url": red_png_data_url()}},
                    ],
                }
            ],
        )
        image_content = content_from(image_response) if image_response.is_success else ""
        report["image_input"] = {
            "http_status": image_response.status_code,
            "total_ms": round(image_ms, 1),
            "recognized_red": "红" in image_content,
            "content_preview": image_content[:80],
        }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
