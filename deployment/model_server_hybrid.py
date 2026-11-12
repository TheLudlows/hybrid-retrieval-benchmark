"""Local ONNX embedding and rerank models for the hybrid benchmark."""
from pathlib import Path
import asyncio
import os
import time

from fastapi import Request
import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

import model_server as base

ROOT = Path(__file__).resolve().parent / "models"
app = base.app


class OnnxTextModel:
    def __init__(self, name):
        directory = ROOT / name
        self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.no_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = int(os.environ.get("BENCH_ONNX_THREADS", "6"))
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / "model_quantized.onnx"),
                                           sess_options=options, providers=["CPUExecutionProvider"])
        self.inputs = {item.name for item in self.session.get_inputs()}

    def tokenize(self, texts):
        return self.tokenizer.encode_batch(texts)

    def predict(self, encodings):
        results = [None] * len(encodings)
        order = sorted(range(len(encodings)), key=lambda index: len(encodings[index].ids))
        for start in range(0, len(order), 8):
            indexes = order[start:start + 8]
            batch = [encodings[index] for index in indexes]
            width = max(len(item.ids) for item in batch)
            inputs = {
                "input_ids": np.asarray([item.ids + [1] * (width - len(item.ids)) for item in batch], dtype=np.int64),
                "attention_mask": np.asarray([item.attention_mask + [0] * (width - len(item.ids)) for item in batch], dtype=np.int64),
            }
            if "token_type_ids" in self.inputs:
                inputs["token_type_ids"] = np.zeros_like(inputs["input_ids"])
            values = self.session.run(None, inputs)[0]
            for index, value in zip(indexes, values, strict=True):
                results[index] = value[0].copy() if values.ndim == 3 else float(value.item())
        return results


class BGEEmbedding:
    def __init__(self):
        self.model = OnnxTextModel("full-embedding")

    def embed(self, texts):
        for vector in self.model.predict(self.model.tokenize(texts)):
            yield vector / np.linalg.norm(vector)


base.embedder = BGEEmbedding()
reranker = OnnxTextModel("full-reranker")

# Optional transport batching: native product requests and per-request usage
# remain intact; concurrent embedding inputs share the existing ONNX batches.
batch_window_ms = float(os.environ.get("BENCH_EMBEDDING_BATCH_WINDOW_MS", "0"))
pending_embeddings = []
embedding_worker = None


async def flush_embeddings():
    global embedding_worker
    await asyncio.sleep(batch_window_ms / 1000)
    requests = pending_embeddings[:]
    pending_embeddings.clear()
    try:
        def predict():
            with base.model_lock:
                return list(base.embedder.embed([text for texts, _ in requests for text in texts]))
        vectors = await asyncio.to_thread(predict)
        offset = 0
        for texts, future in requests:
            future.set_result(vectors[offset:offset + len(texts)])
            offset += len(texts)
    except Exception as error:
        for _, future in requests:
            future.set_exception(error)
    finally:
        embedding_worker = None
        if pending_embeddings:
            embedding_worker = asyncio.create_task(flush_embeddings())


async def embed_batched(texts):
    global embedding_worker
    future = asyncio.get_running_loop().create_future()
    pending_embeddings.append((texts, future))
    if embedding_worker is None:
        embedding_worker = asyncio.create_task(flush_embeddings())
    return await future


if batch_window_ms > 0:
    base.embed_async = embed_batched


@app.get("/hybrid/info")
def hybrid_info():
    return {"onnxruntime": ort.__version__,
            "embedding_intra_op_threads": base.embedder.model.session.get_session_options().intra_op_num_threads,
            "reranker_intra_op_threads": reranker.session.get_session_options().intra_op_num_threads,
            "inter_op_threads": reranker.session.get_session_options().inter_op_num_threads,
            "embedding_providers": base.embedder.model.session.get_providers(),
            "reranker_providers": reranker.session.get_providers(),
            "embedding_batch_window_ms": batch_window_ms,
            "embedding_truncation": base.embedder.model.tokenizer.truncation,
            "reranker_truncation": reranker.tokenizer.truncation}


@app.get("/contextbase/tei/info")
def tei_info():
    return {"model_id": "BAAI/bge-reranker-v2-m3"}


def rerank(product, query, texts):
    start = time.perf_counter()
    with base.model_lock:
        encodings = reranker.tokenize([(query, text) for text in texts])
        logits = np.asarray(reranker.predict(encodings))
    scores = (1 / (1 + np.exp(-np.clip(logits, -40, 40)))).tolist()
    base.record(product, "rerank", {
        "input_tokens": sum(sum(item.attention_mask) for item in encodings),
        "output_tokens": 0, "calls": 1, "pairs": len(texts), "status": "observed",
    }, "BAAI/bge-reranker-v2-m3", time.perf_counter() - start)
    return sorted(enumerate(scores), key=lambda item: (-item[1], item[0]))


@app.post("/contextbase/tei/rerank")
async def rerank_tei(request: Request):
    payload = await request.json()
    return [{"index": index, "score": score} for index, score in
            rerank("contextbase", payload["query"], payload["texts"])]


@app.post("/weknora/v1/rerank")
async def rerank_cohere(request: Request):
    payload = await request.json()
    texts = [item["text"] if isinstance(item, dict) else item for item in payload["documents"]]
    return {"results": [{"index": index, "relevance_score": score} for index, score in
                        rerank("weknora", payload["query"], texts)[:payload.get("top_n") or len(texts)]]}
