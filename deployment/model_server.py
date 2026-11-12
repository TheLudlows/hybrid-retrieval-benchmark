"""Serve the same local models to both benchmark products and record usage."""

import json
import os
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

ROOT = Path(__file__).resolve().parent
MEASUREMENT_DIR = Path(os.environ.get("BENCH_MEASUREMENT_DIR", ROOT))
app = FastAPI()
model_lock = threading.Lock()
audit_lock = threading.Lock()
llm = None
embedder = None
embed_async = None


def record(product, kind, usage, model, elapsed):
    context_file = MEASUREMENT_DIR / "measurement-context.json"
    contexts = json.loads(context_file.read_text()) if context_file.exists() else {}
    context = contexts.get(product, {"phase": "unassigned", "id": "unassigned"})
    entry = {
        **context, "product": product, "kind": kind, "model": model,
        "elapsed_ms": elapsed * 1000, "timestamp": time.time(), **usage,
    }
    with audit_lock, (MEASUREMENT_DIR / "model-usage.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@app.get("/health")
def health():
    return {"ready": True}


@app.get("/{product}/v1/models")
def models(product: str):
    return {"object": "list", "data": [
        {"id": "qwen2.5-1.5b-local", "object": "model"},
        {"id": "multilingual-minilm", "object": "model"},
    ]}


@app.post("/{product}/v1/embeddings")
async def embeddings(product: str, request: Request):
    global embedder
    body = await request.json()
    texts = body["input"]
    if isinstance(texts, str):
        texts = [texts]
    start = time.perf_counter()
    if embed_async is not None:
        vectors = await embed_async(texts)
    else:
        with model_lock:
            if embedder is None:
                from fastembed import TextEmbedding
                embedder = TextEmbedding(
                    model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                    cache_dir=str(ROOT / "models/embedding"), threads=3,
                )
            vectors = list(embedder.embed(texts))
    tokens = sum(sum(encoding.attention_mask) for encoding in embedder.model.tokenize(texts))
    usage = {"input_tokens": tokens, "output_tokens": 0, "calls": 1, "status": "observed"}
    record(product, "embedding", usage, body.get("model"), time.perf_counter() - start)
    return {"object": "list", "model": body.get("model"), "data": [
        {"object": "embedding", "index": i, "embedding": vector.tolist()}
        for i, vector in enumerate(vectors)
    ], "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}


@app.post("/{product}/v1/chat/completions")
async def chat(product: str, request: Request):
    global llm
    body = await request.json()
    if body.get("stream"):
        raise HTTPException(400, "The benchmark model endpoint requires non-streaming requests")
    response_format = body.get("response_format")
    if response_format and response_format.get("type") == "json_schema":
        response_format = {"type": "json_object", "schema": response_format["json_schema"]["schema"]}
    start = time.perf_counter()
    with model_lock:
        if llm is None:
            from llama_cpp import Llama
            llm = Llama(model_path=str(ROOT / "models/qwen2.5-1.5b-instruct-q4_k_m.gguf"),
                        n_ctx=16384, n_threads=4, n_batch=256, verbose=False)
        result = llm.create_chat_completion(
            messages=body["messages"], temperature=body.get("temperature") or 0,
            max_tokens=min(body.get("max_tokens") or body.get("max_completion_tokens") or 2048, 4096),
            response_format=response_format,
        )
    usage = result["usage"]
    record(product, "chat", {
        "input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"],
        "calls": 1, "status": "observed",
    }, body.get("model"), time.perf_counter() - start)
    return result
