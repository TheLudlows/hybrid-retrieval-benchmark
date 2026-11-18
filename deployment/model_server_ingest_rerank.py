"""Expose the shared BGE models and meter a dedicated OpenAI-compatible import LLM."""

import asyncio
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request as UpstreamRequest, urlopen

from fastapi import FastAPI, HTTPException, Request

import model_server as accounting
from model_server_hybrid import app as hybrid_app

app = FastAPI()
MODEL = os.environ["BENCH_INGEST_LLM_MODEL"]
UPSTREAM = os.environ["BENCH_INGEST_LLM_URL"].rstrip("/")


def complete(body):
    headers = {"Content-Type": "application/json"}
    if key := os.environ.get("BENCH_INGEST_LLM_API_KEY"):
        headers["Authorization"] = "Bearer " + key
    request = UpstreamRequest(
        UPSTREAM + "/chat/completions",
        json.dumps({**body, "model": MODEL, "stream": False}).encode(),
        headers,
    )
    with urlopen(request, timeout=600) as response:
        return json.load(response)


@app.get("/{product}/v1/models")
def models(product: str):
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@app.post("/{product}/v1/chat/completions")
async def chat(product: str, request: Request):
    body = await request.json()
    if product not in {"contextbase", "weknora"} or body.get("stream"):
        raise HTTPException(400, "Use a benchmark product and a non-streaming request")
    context_path = accounting.MEASUREMENT_DIR / "measurement-context.json"
    context = json.loads(context_path.read_text()).get(product, {})
    start = time.perf_counter()
    if context.get("phase") not in {"ingest", "preflight"}:
        accounting.record(product, "chat", {"calls": 1, "status": "blocked"}, MODEL, 0)
        raise HTTPException(409, "Generation is only allowed during benchmark import")
    try:
        result = await asyncio.to_thread(complete, body)
        usage = result["usage"]
        prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
        if (
            type(prompt) is not int
            or type(completion) is not int
            or min(prompt, completion) < 0
        ):
            raise ValueError("Invalid upstream token counts")
    except (
        KeyError,
        ValueError,
        TypeError,
        HTTPError,
        URLError,
        TimeoutError,
    ) as error:
        accounting.record(
            product,
            "chat",
            {"calls": 1, "status": "usage_unavailable"},
            MODEL,
            time.perf_counter() - start,
        )
        raise HTTPException(
            502, f"Import LLM request failed ({type(error).__name__})"
        ) from None
    accounting.record(
        product,
        "chat",
        {
            "calls": 1,
            "status": "observed",
            "input_tokens": prompt,
            "output_tokens": completion,
            "upstream_model": result.get("model"),
        },
        MODEL,
        time.perf_counter() - start,
    )
    return result


# Keep existing embedding and native TEI/Cohere routes, including token metering.
app.mount("/", hybrid_app)
