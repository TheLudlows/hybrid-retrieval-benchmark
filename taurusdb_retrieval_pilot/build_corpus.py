"""Fetch or import official HTML and build an evidence-checked retrieval corpus."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
COUNTS = {"exact": 8, "paraphrase": 8, "confusable": 8, "multi_evidence": 10, "no_answer": 6}
SPACE = re.compile(r"\s+")
BOILERPLATE = re.compile(r"(?i)breadcrumb|footer|navigation|sidebar|feedback|bottom-nav|related-link")
TRANSLATION = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "～": "~"})


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalized_with_offsets(text):
    parts = []
    positions = []
    for index, character in enumerate(text):
        for normalized in unicodedata.normalize("NFKC", character).translate(TRANSLATION):
            if not normalized.isspace() and normalized != "`":
                parts.append(normalized)
                positions.append(index)
    return "".join(parts), positions


def normalize(text):
    return normalized_with_offsets(text)[0]


def evidence_quote(text, anchor):
    normalized_text, offsets = normalized_with_offsets(text)
    normalized_anchor = normalize(anchor)
    index = normalized_text.find(normalized_anchor)
    if index < 0:
        raise ValueError(f"anchor missing: {anchor!r}")
    return text[offsets[index]:offsets[index + len(normalized_anchor) - 1] + 1]


def load_labels():
    sources = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    questions = read_jsonl(ROOT / "questions.jsonl")
    updates = read_jsonl(ROOT / "updates.jsonl")
    ids = {source["document_id"] for source in sources}
    if len(ids) != len(sources) or len({source["url"] for source in sources}) != len(sources):
        raise ValueError("duplicate source ID or URL")
    for source in sources:
        address = urlsplit(source["url"])
        if address.scheme != "https" or address.hostname != "support.huaweicloud.com":
            raise ValueError(f"unexpected source URL: {source['url']}")
        if not re.fullmatch(r"[a-z0-9_]+", source["document_id"]):
            raise ValueError(f"invalid source ID: {source['document_id']}")
    if len({q["id"] for q in questions}) != len(questions) or len({q["query"] for q in questions}) != len(questions):
        raise ValueError("duplicate question ID or query")
    if {kind: sum(q["category"] == kind for q in questions) for kind in COUNTS} != COUNTS or len(questions) != sum(COUNTS.values()):
        raise ValueError("unexpected question counts")
    for question in questions:
        evidence = question["evidence"]
        if not evidence:
            if question["category"] != "no_answer" or not question.get("missing_evidence"):
                raise ValueError(f"no-answer rationale missing: {question['id']}")
            continue
        if question["category"] == "no_answer" or any(e["document_id"] not in ids or not normalize(e["quote"]) for e in evidence):
            raise ValueError(f"bad evidence: {question['id']}")
        if len({e["id"] for e in evidence}) != len(evidence):
            raise ValueError(f"duplicate evidence ID: {question['id']}")
        if question["category"] == "multi_evidence" and len({e["document_id"] for e in evidence}) < 2:
            raise ValueError(f"cross-document case needs multiple sources: {question['id']}")
    if len(updates) != 5 or len({u["document_id"] for u in updates}) != 5 or ids.intersection(u["document_id"] for u in updates):
        raise ValueError("expected five independent update fixtures")
    for update in updates:
        old, new = update["before"], update["after"]
        if (old["version"] != "v1" or new["version"] != "v2"
                or normalize(old["quote"]) in normalize(new["text"])
                or normalize(new["quote"]) in normalize(old["text"])):
            raise ValueError(f"ambiguous version change: {update['document_id']}")
        evidence_quote(old["text"], old["quote"])
        evidence_quote(new["text"], new["quote"])
    return sources, questions, updates


def extract_article(payload, source):
    from lxml import html

    tree = html.fromstring(payload.decode("utf-8-sig"))
    for node in tree.xpath("//script | //style | //noscript | //svg | //nav | //header[not(.//h1)] | //footer | //aside | //form"):
        node.drop_tree()
    for node in tree.xpath("//*[@class or @id]"):
        if node.getparent() is not None and BOILERPLATE.search(" ".join((node.get("class", ""), node.get("id", "")))):
            node.drop_tree()
    headings = tree.xpath("//h1")
    expected_heading = normalize(source.get("heading", source["title"]))
    title = next((h for h in headings if expected_heading in normalize(h.text_content())), None)
    if title is None:
        raise ValueError("expected article heading not found")
    topics = title.xpath(
        "ancestor-or-self::*[self::article or self::main or @role='main' or "
        "contains(concat(' ',normalize-space(@class),' '),' topic ') or "
        "@id='content' or @id='main-content'][1]"
    )
    if topics:
        article = topics[0]
    else:
        article = next((parent for parent in title.iterancestors()
                        if parent.tag != "html" and parent.xpath(".//p | .//table | .//pre")), None)
    if article is None:
        raise ValueError("article body not found")

    blocks = [f"# {source['title']}"]
    block_tags = {"p", "li", "dt", "dd", "pre", "tr", "h2", "h3", "h4", "h5", "h6"}
    for node in article.iter():
        tag = node.tag
        if tag not in block_tags:
            continue
        parents = []
        for parent in node.iterancestors():
            if parent is article:
                break
            parents.append(parent.tag)
        if "tr" in parents or "pre" in parents or (tag == "li" and node.xpath(".//p | .//pre | .//tr | .//li")):
            continue
        value = SPACE.sub(" ", node.text_content()).strip()
        if tag == "tr":
            value = " | ".join(SPACE.sub(" ", cell.text_content()).strip() for cell in node.xpath("./th|./td"))
        if not value:
            continue
        if tag == "pre":
            value = "```\n" + node.text_content().strip() + "\n```"
        elif tag in {"h2", "h3", "h4", "h5", "h6"}:
            value = "#" * int(tag[1]) + " " + value
        elif tag == "li":
            value = "- " + value
        blocks.append(value)
    if len(normalize("\n".join(blocks[1:]))) < 80:
        raise ValueError("article body is unexpectedly short")
    return "\n\n".join(blocks) + "\n"


def fetch_source(source, html_dir, cache_html=None):
    try:
        if html_dir is None:
            request = Request(source["url"], headers={"User-Agent": "Mozilla/5.0 TaurusDB-retrieval-pilot"})
            with urlopen(request, timeout=20) as response:
                if urlsplit(response.geturl()).hostname != "support.huaweicloud.com":
                    raise ValueError("unexpected redirect host")
                raw = response.read()
        else:
            raw = (html_dir / f"{source['document_id']}.html").read_bytes()
        article = extract_article(raw, source)
        if cache_html is not None:
            (cache_html / f"{source['document_id']}.html").write_bytes(raw)
        return article, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError) as error:
        raise ValueError(f"{source['document_id']}: {error}") from error


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def build(output, html_dir, sources, questions, updates, cache_html=None):
    if output.exists():
        raise ValueError(f"choose a new output directory: {output}")
    if cache_html is not None:
        cache_html.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=5) as executor:
        payloads = list(executor.map(lambda source: fetch_source(source, html_dir, cache_html), sources))
    articles = {source["document_id"]: data[0] for source, data in zip(sources, payloads, strict=True)}
    missing = []
    for question in questions:
        for evidence in question["evidence"]:
            try:
                evidence["quote"] = evidence_quote(articles[evidence["document_id"]], evidence["quote"])
            except ValueError as error:
                missing.append(f"{question['id']} / {evidence['document_id']}: {error}")
    if missing:
        raise ValueError("\n".join(missing))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        stage = Path(directory) / "dataset"
        corpus = stage / "corpus"
        corpus.mkdir(parents=True)
        manifest = []
        for source, (_, html_hash) in zip(sources, payloads, strict=True):
            doc = source["document_id"]
            body = articles[doc].encode("utf-8")
            (corpus / f"{doc}.md").write_bytes(body)
            manifest.append({**source, "html_sha256": html_hash, "markdown_sha256": hashlib.sha256(body).hexdigest(),
                             "characters": len(articles[doc])})
        write_jsonl(stage / "questions.jsonl", questions)
        write_jsonl(stage / "updates.jsonl", updates)
        (stage / "corpus-manifest.json").write_text(json.dumps({
            "built_at_utc": datetime.now(timezone.utc).isoformat(), "documents": manifest
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.move(stage, output)
    print(f"Built {len(sources)} documents, {len(questions)} questions and {len(updates)} update fixtures in {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("taurusdb-pilot-corpus"))
    parser.add_argument("--html-dir", type=Path, help="Read saved HTML named <document_id>.html")
    parser.add_argument("--cache-html", type=Path, help="Save the fetched official HTML snapshots for repeat builds")
    parser.add_argument("--check-labels", action="store_true", help="Validate the fixture files without fetching HTML")
    args = parser.parse_args()
    try:
        sources, questions, updates = load_labels()
        if args.check_labels:
            print(f"Validated {len(sources)} source records, {len(questions)} questions and {len(updates)} update fixtures")
        else:
            build(args.output, args.html_dir, sources, questions, updates, args.cache_html)
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
