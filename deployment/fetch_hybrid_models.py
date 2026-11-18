"""Download and verify the pinned ONNX models used by the retrieval benchmarks."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parent / "models"
MODELS = {
    "full-embedding": (
        "onnx-community/bge-m3-ONNX",
        "25b9af8e87a38eb120cfe87125383677b9cd309e",
        {
            "onnx/model_quantized.onnx": "2237f770aad5c71bbc1fc2d361a57f9a37400574cc9eff32626f0cdb49234730",
            "tokenizer.json": "249df0778f236f6ece390de0de746838ef25b9d6954b68c2ee71249e0a9d8fd4",
            "config.json": "70dae5884ced999af00244f776ac9eaa71538d68497d3d6a6091e0318cd32905",
        },
    ),
    "full-reranker": (
        "onnx-community/bge-reranker-v2-m3-ONNX",
        "6f5ff65298512715a1e669753bc754d2bc8f367b",
        {
            "onnx/model_quantized.onnx": "912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d",
            "tokenizer.json": "8bf8afbfd11306bd872018c53bfdf2e160a56f8edbcf49933324404791c148d3",
            "config.json": "122e922dcfed6503c8721e6fe1daf090340c3d95ca7f3aa3a72730b321a51cfd",
        },
    ),
}


def verify(path, expected):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(
                f"SHA-256 mismatch for {path}. Move this file aside and retry."
            )


def fetch(entry):
    directory, repository, revision, filename, digest = entry
    path = ROOT / directory / Path(filename).name
    if path.exists():
        verify(path, digest)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            dir=path.parent, suffix=".part", delete=False
        ) as stream:
            partial = Path(stream.name)
        try:
            subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "--retry",
                    "2",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    "900",
                    f"https://huggingface.co/{repository}/resolve/{revision}/{filename}",
                    "-o",
                    str(partial),
                ],
                check=True,
            )
            verify(partial, digest)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    print(directory, filename, path.stat().st_size, "SHA-256 verified", flush=True)


def main():
    entries = [
        (directory, repository, revision, filename, digest)
        for directory, (repository, revision, files) in MODELS.items()
        for filename, digest in files.items()
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for _ in pool.map(fetch, entries):
            pass


if __name__ == "__main__":
    main()
