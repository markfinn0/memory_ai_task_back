import json
import uuid
import hashlib
import base64
import math
import boto3
import os
import time
import io
from datetime import datetime
from decimal import Decimal
from boto3.dynamodb.conditions import Attr

try:
    import requests as _requests
except ImportError:
    from urllib.request import Request, urlopen
    _requests = None

try:
    import pdfplumber as _pdfplumber
except ImportError:
    _pdfplumber = None

try:
    import docx as _docx
except ImportError:
    _docx = None

# ---------------------------------------------------------------------------
# AWS clients & config
# ---------------------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
DOC_TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "document_tables_memory")
CHAT_TABLE_NAME = os.environ.get("CHAT_TABLE", "chat_memory_user")
SEARCH_TABLE_NAME = os.environ.get("SEARCH_TABLE", "tabela_search")
doc_table = dynamodb.Table(DOC_TABLE_NAME)
chat_table = dynamodb.Table(CHAT_TABLE_NAME)
search_table = dynamodb.Table(SEARCH_TABLE_NAME)

s3 = boto3.client("s3", region_name=REGION)
S3_BUCKET = os.environ.get("S3_BUCKET", "memoryaitest")

DELETE_PASSWORD = os.environ.get("DELETE_PASSWORD", "memory_ai_delete_2024")

# OpenAI
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
OPENAI_CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small default

# Retry config
_MAX_RETRIES = int(os.environ.get("OPENAI_MAX_RETRIES", "3"))
_RETRY_BACKOFF = 1  # seconds, doubles each retry

# ElastiCache / Valkey (Redis-compatible, serverless with TLS)
ELASTICACHE_HOST = os.environ.get(
    "ELASTICACHE_HOST",
    "memoryai-5ngs3u.serverless.use1.cache.amazonaws.com",
)
ELASTICACHE_PORT = int(os.environ.get("ELASTICACHE_PORT", "6379"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "86400"))  # 1 day

# Elasticsearch
ELASTICSEARCH_HOST = os.environ.get("ELASTICSEARCH_HOST", "")
ELASTICSEARCH_INDEX = os.environ.get("ELASTICSEARCH_INDEX", "memory_ai_interactions")

# Similarity threshold: only send file content to AI when context score >= this
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.3"))

# Threshold for reusing cached answers from tabela_search
SEARCH_CACHE_THRESHOLD = float(os.environ.get("SEARCH_CACHE_THRESHOLD", "0.85"))

# Max chars of extracted content to store in DynamoDB (to stay under 400KB item limit)
MAX_CONTENT_CHARS = int(os.environ.get("MAX_CONTENT_CHARS", "100000"))

# ---------------------------------------------------------------------------
# Lazy-initialised singletons
# ---------------------------------------------------------------------------

_redis_client = None


def _get_redis():
    """Return a Valkey/Redis client, or None if unavailable."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if not ELASTICACHE_HOST:
        return None
    try:
        import redis as _redis_mod
        _redis_client = _redis_mod.Redis(
            host=ELASTICACHE_HOST,
            port=ELASTICACHE_PORT,
            decode_responses=True,
            ssl=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        _redis_client.ping()
        return _redis_client
    except Exception as exc:
        print(f"[cache] Valkey/Redis unavailable: {exc}")
        _redis_client = None
        return None


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json",
}


def response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, default=str),
    }


def convert_floats_to_decimal(obj):
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: convert_floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_floats_to_decimal(i) for i in obj]
    return obj


def convert_decimals_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: convert_decimals_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_decimals_to_float(i) for i in obj]
    return obj


MIME_MAP = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".json": "application/json",
    ".xml": "application/xml",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------


def upload_file_to_s3(doc_id: str, file_data_url: str, file_type: str) -> str:
    if "," in file_data_url:
        header, encoded = file_data_url.split(",", 1)
    else:
        encoded = file_data_url
        header = ""
    file_bytes = base64.b64decode(encoded)
    content_type = "application/octet-stream"
    if header.startswith("data:"):
        content_type = header.split(":")[1].split(";")[0]
    else:
        content_type = MIME_MAP.get(file_type, content_type)
    s3_key = f"documents/{doc_id}/{doc_id}{file_type}"
    s3.put_object(Bucket=S3_BUCKET, Key=s3_key, Body=file_bytes, ContentType=content_type)
    return s3_key


def get_s3_presigned_url(s3_key: str, expires_in: int = 3600) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=expires_in,
    )


def delete_s3_file(s3_key: str) -> None:
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        print(f"Warning: Failed to delete S3 object {s3_key}: {e}")


# ---------------------------------------------------------------------------
# Document content extraction helpers
# ---------------------------------------------------------------------------


def _decode_file_data_url(file_data_url: str) -> bytes:
    """Decode a base64 data-URL (or raw base64) into bytes."""
    if "," in file_data_url:
        _, encoded = file_data_url.split(",", 1)
    else:
        encoded = file_data_url
    return base64.b64decode(encoded)


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    if _pdfplumber is None:
        print("[extract] pdfplumber not available, skipping PDF extraction")
        return ""
    try:
        text_parts = []
        with _pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        extracted = "\n".join(text_parts)
        print(f"[extract] PDF: extracted {len(extracted)} chars from {len(text_parts)} pages")
        return extracted
    except Exception as exc:
        print(f"[extract] PDF extraction error: {exc}")
        return ""


def extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract text from DOCX bytes using python-docx."""
    if _docx is None:
        print("[extract] python-docx not available, skipping DOCX extraction")
        return ""
    try:
        doc = _docx.Document(io.BytesIO(file_bytes))
        text_parts = [para.text for para in doc.paragraphs if para.text.strip()]
        extracted = "\n".join(text_parts)
        print(f"[extract] DOCX: extracted {len(extracted)} chars from {len(text_parts)} paragraphs")
        return extracted
    except Exception as exc:
        print(f"[extract] DOCX extraction error: {exc}")
        return ""


def extract_text_from_file(file_data_url: str, file_type: str) -> str:
    """Extract readable text content from a file based on its type.

    Supports PDF (.pdf) and Word (.docx) files.
    For other types, returns empty string (caller should use the raw content field).
    """
    if not file_data_url:
        return ""
    file_bytes = _decode_file_data_url(file_data_url)
    file_type_lower = file_type.lower()
    if file_type_lower == ".pdf":
        return extract_text_from_pdf(file_bytes)
    if file_type_lower in (".docx",):
        return extract_text_from_docx(file_bytes)
    # For plain text types, decode as UTF-8
    if file_type_lower in (".txt", ".csv", ".json", ".xml"):
        try:
            return file_bytes.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


# ---------------------------------------------------------------------------
# OpenAI helpers
# ---------------------------------------------------------------------------


def _openai_request(endpoint: str, payload: dict, timeout: int = 30) -> dict:
    """Make a POST request to the OpenAI API with retry logic."""
    url = f"{OPENAI_BASE_URL}/{endpoint}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            if _requests is not None:
                resp = _requests.post(url, json=payload, headers=headers, timeout=timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = _RETRY_BACKOFF * (2 ** attempt)
                    print(f"[openai] {resp.status_code} on attempt {attempt + 1}, retrying in {wait}s")
                    time.sleep(wait)
                    last_exc = Exception(f"HTTP {resp.status_code}: {resp.text}")
                    continue
                resp.raise_for_status()
                return resp.json()
            else:
                # Fallback to urllib if requests is not available
                data = json.dumps(payload).encode()
                req = Request(url, data=data, headers=headers, method="POST")
                with urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                wait = _RETRY_BACKOFF * (2 ** attempt)
                print(f"[openai] Error on attempt {attempt + 1}: {exc}, retrying in {wait}s")
                time.sleep(wait)
    raise last_exc


def generate_embedding(text: str) -> list:
    """Call OpenAI Embeddings API and return a vector."""
    if not OPENAI_API_KEY:
        print("[embed] No OPENAI_API_KEY, returning empty vector")
        return []
    try:
        result = _openai_request("embeddings", {
            "input": text[:8000],
            "model": OPENAI_EMBED_MODEL,
        })
        data = result.get("data", [])
        if data:
            return data[0].get("embedding", [])
        return []
    except Exception as exc:
        print(f"[embed] OpenAI embedding error: {exc}")
        return []


def generate_ai_response(question: str, context_docs: list, chat_history: list) -> str:
    """Call OpenAI Chat Completions API to generate a chat answer."""
    if not OPENAI_API_KEY:
        return (
            f'I received your question: "{question}". '
            "However, the AI service is not configured yet. "
            "Please set the OPENAI_API_KEY environment variable."
        )

    ctx_parts = []
    for i, doc in enumerate(context_docs, 1):
        meta = doc.get("metadata", {})
        ctx_parts.append(
            f"--- Document {i}: {meta.get('fileName', 'unknown')} ---\n"
            f"Author: {meta.get('author', 'N/A')}\n"
            f"Context: {meta.get('context', 'N/A')}\n"
            f"Content:\n{doc.get('content', '')[:3000]}\n"
        )
    context_block = "\n".join(ctx_parts) if ctx_parts else "No relevant documents found."

    history_parts = []
    for msg in chat_history[-10:]:
        role = msg.get("role", "user")
        prefix = "User" if role == "user" else "Assistant"
        history_parts.append(f"{prefix}: {msg.get('content', '')}")
    history_block = "\n".join(history_parts) if history_parts else ""

    system_prompt = (
        "You are a helpful AI assistant for the Memory AI system. "
        "Answer the user's question based on the provided document context. "
        "Be concise, accurate, and cite specific documents when possible. "
        "If the context doesn't contain enough information, say so honestly.\n\n"
        f"DOCUMENT CONTEXT:\n{context_block}\n"
    )
    if history_block:
        system_prompt += f"\nCHAT HISTORY:\n{history_block}\n"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    try:
        result = _openai_request("chat/completions", {
            "model": OPENAI_CHAT_MODEL,
            "messages": messages,
            "max_tokens": 1024,
            "temperature": 0.7,
        })
        choices = result.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "No response generated.")
        return "No response generated by the AI model."
    except Exception as exc:
        print(f"[openai] Chat error: {exc}")
        return f"I encountered an error while processing your question. Error: {exc}"


# ---------------------------------------------------------------------------
# DynamoDB vector search helpers
# ---------------------------------------------------------------------------


def _cosine_similarity(vec_a: list, vec_b: list) -> float:
    """Compute cosine similarity between two vectors."""
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def semantic_search(query_embedding: list, k: int = 5) -> list:
    """Scan all documents in DynamoDB and rank by cosine similarity.

    Uses ProjectionExpression to avoid downloading large content fields
    during the scan. Full content is fetched only for the top-k matches.
    """
    if not query_embedding:
        return []

    # Convert query embedding floats for comparison
    query_vec = [float(v) for v in query_embedding]

    # Only fetch fields needed for similarity comparison (skip large content)
    scan_kwargs = {
        "FilterExpression": Attr("record_type").eq("document"),
        "ProjectionExpression": "ID, embedding, metadata",
    }
    items = []
    start_key = None
    while True:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        resp = doc_table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break

    scored = []
    for item in items:
        embedding_data = item.get("embedding", {})
        doc_vec = embedding_data.get("vector", [])
        if not doc_vec:
            continue
        # Convert Decimal to float
        doc_vec = [float(v) for v in doc_vec]
        if len(doc_vec) != len(query_vec):
            continue
        score = _cosine_similarity(query_vec, doc_vec)
        meta = item.get("metadata", {})
        scored.append({
            "doc_id": item.get("ID", ""),
            "score": round(score, 4),
            "metadata": {
                "fileName": meta.get("fileName", ""),
                "author": meta.get("author", ""),
                "context": meta.get("context", ""),
                "tags": meta.get("tags", []),
            },
        })

    # Sort by score descending and keep top-k
    scored.sort(key=lambda x: x["score"], reverse=True)
    top_results = scored[:k]

    # Fetch full content only for matched documents
    for result in top_results:
        doc_id = result.get("doc_id")
        if doc_id:
            try:
                full_doc = doc_table.get_item(
                    Key={"ID": doc_id},
                    ProjectionExpression="content",
                )
                result["content"] = full_doc.get("Item", {}).get("content", "")
            except Exception:
                result["content"] = ""
        else:
            result["content"] = ""

    return top_results


# ---------------------------------------------------------------------------
# ElastiCache / Valkey helpers (legacy, used as fallback)
# ---------------------------------------------------------------------------


def _cache_key(question: str) -> str:
    return "memoryai:answer:" + hashlib.sha256(question.strip().lower().encode()).hexdigest()


def get_cached_response(question: str):
    """Look up a cached AI response by question hash (Redis fallback)."""
    rc = _get_redis()
    if rc is None:
        return None
    try:
        val = rc.get(_cache_key(question))
        if val:
            return json.loads(val)
    except Exception as exc:
        print(f"[cache] GET error: {exc}")
    return None


def cache_response(question: str, data: dict):
    """Cache an AI response with TTL (Redis fallback)."""
    rc = _get_redis()
    if rc is None:
        return
    try:
        rc.setex(_cache_key(question), CACHE_TTL, json.dumps(data, default=str))
    except Exception as exc:
        print(f"[cache] SET error: {exc}")


# ---------------------------------------------------------------------------
# Elasticsearch helpers
# ---------------------------------------------------------------------------


def _question_hash(question: str) -> str:
    """Deterministic hash for a question string."""
    return hashlib.sha256(question.strip().lower().encode()).hexdigest()


def _es_request(method: str, path: str, body: dict = None, timeout: int = 10):
    """Make an HTTP request to the Elasticsearch REST API."""
    if not ELASTICSEARCH_HOST:
        return None
    url = f"{ELASTICSEARCH_HOST.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json"}
    try:
        if _requests is not None:
            resp = _requests.request(
                method, url, json=body, headers=headers, timeout=timeout,
            )
            if resp.status_code >= 400:
                print(f"[elasticsearch] {method} {path} -> {resp.status_code}: {resp.text[:300]}")
                return None
            return resp.json()
        else:
            data = json.dumps(body).encode() if body else None
            req = Request(url, data=data, headers=headers, method=method.upper())
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
    except Exception as exc:
        print(f"[elasticsearch] {method} {path} error: {exc}")
        return None


_es_index_verified = False


def _ensure_es_index():
    """Create the Elasticsearch index if it does not exist (cached check)."""
    global _es_index_verified
    if not ELASTICSEARCH_HOST or _es_index_verified:
        return
    result = _es_request("GET", f"/{ELASTICSEARCH_INDEX}/_settings")
    if result is None:
        mapping = {
            "mappings": {
                "properties": {
                    "question": {"type": "text"},
                    "question_hash": {"type": "keyword"},
                    "answer": {"type": "text"},
                    "confidence": {"type": "float"},
                    "documents_used": {"type": "keyword"},
                    "chat_id": {"type": "keyword"},
                    "created_at": {"type": "date"},
                }
            }
        }
        _es_request("PUT", f"/{ELASTICSEARCH_INDEX}", body=mapping)
        print(f"[elasticsearch] Created index {ELASTICSEARCH_INDEX}")
    _es_index_verified = True


def save_to_elasticsearch(question: str, answer: str, confidence: float,
                          documents_used: list, chat_id: str):
    """Save an AI interaction (question + answer) to Elasticsearch."""
    if not ELASTICSEARCH_HOST:
        return
    _ensure_es_index()
    doc = {
        "question": question,
        "question_hash": _question_hash(question),
        "answer": answer,
        "confidence": confidence,
        "documents_used": documents_used,
        "chat_id": chat_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    result = _es_request("POST", f"/{ELASTICSEARCH_INDEX}/_doc", body=doc)
    if result:
        print(f"[elasticsearch] Saved interaction {result.get('_id', '?')}")


def get_from_elasticsearch(question: str):
    """Look up a cached AI response from Elasticsearch by question hash."""
    if not ELASTICSEARCH_HOST:
        return None
    query = {
        "query": {
            "term": {
                "question_hash": _question_hash(question)
            }
        },
        "sort": [{"created_at": {"order": "desc"}}],
        "size": 1,
    }
    result = _es_request("POST", f"/{ELASTICSEARCH_INDEX}/_search", body=query)
    if not result:
        return None
    hits = result.get("hits", {})
    total = hits.get("total", {})
    count = total.get("value", 0) if isinstance(total, dict) else total
    if count > 0 and hits.get("hits"):
        src = hits["hits"][0]["_source"]
        return {
            "content": src.get("answer", ""),
            "confidence": src.get("confidence", 0.95),
            "documentsUsed": src.get("documents_used", []),
        }
    return None


# ---------------------------------------------------------------------------
# tabela_search helpers (DynamoDB-based AI response cache)
# ---------------------------------------------------------------------------


def save_to_search_table(question: str, answer: str, question_embedding: list,
                         confidence: float, documents_used: list, chat_id: str):
    """Save an AI interaction to the tabela_search DynamoDB table."""
    try:
        item = {
            "id": str(uuid.uuid4()),
            "question": question,
            "question_hash": _question_hash(question),
            "answer": answer,
            "confidence": confidence,
            "documents_used": documents_used,
            "chat_id": chat_id,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        if question_embedding:
            item["embedding"] = {
                "model": OPENAI_EMBED_MODEL,
                "dimensions": EMBEDDING_DIMENSIONS,
                "vector": question_embedding,
            }
        item = convert_floats_to_decimal(item)
        search_table.put_item(Item=item)
        print(f"[tabela_search] Saved interaction {item['id']}")
    except Exception as exc:
        print(f"[tabela_search] Error saving: {exc}")


def search_in_search_table(question: str, question_embedding: list):
    """Search tabela_search for a cached AI response.

    First tries exact match by question hash, then falls back to
    semantic similarity search using embeddings.
    Returns a dict with content/confidence/documentsUsed or None.
    """
    # --- Exact match by question hash ---
    q_hash = _question_hash(question)
    try:
        items = []
        start_key = None
        while True:
            scan_kwargs = {
                "FilterExpression": Attr("question_hash").eq(q_hash),
            }
            if start_key:
                scan_kwargs["ExclusiveStartKey"] = start_key
            scan_resp = search_table.scan(**scan_kwargs)
            items.extend(scan_resp.get("Items", []))
            if items:
                break
            start_key = scan_resp.get("LastEvaluatedKey")
            if not start_key:
                break
        if items:
            src = items[0]
            print(f"[tabela_search] Exact hash HIT: {src.get('id')}")
            return {
                "content": src.get("answer", ""),
                "confidence": float(src.get("confidence", 0.95)),
                "documentsUsed": src.get("documents_used", []),
            }
    except Exception as exc:
        print(f"[tabela_search] Hash lookup error: {exc}")

    # --- Semantic similarity search ---
    if not question_embedding:
        return None
    query_vec = [float(v) for v in question_embedding]
    try:
        # Only fetch id and embedding for comparison (skip large answer text)
        all_items = []
        start_key = None
        while True:
            scan_kwargs = {
                "ProjectionExpression": "id, embedding",
            }
            if start_key:
                scan_kwargs["ExclusiveStartKey"] = start_key
            resp = search_table.scan(**scan_kwargs)
            all_items.extend(resp.get("Items", []))
            start_key = resp.get("LastEvaluatedKey")
            if not start_key:
                break

        best_score = 0.0
        best_id = None
        for item in all_items:
            emb = item.get("embedding", {})
            item_vec = emb.get("vector", [])
            if not item_vec:
                continue
            item_vec = [float(v) for v in item_vec]
            if len(item_vec) != len(query_vec):
                continue
            score = _cosine_similarity(query_vec, item_vec)
            if score > best_score:
                best_score = score
                best_id = item.get("id")

        if best_id and best_score >= SEARCH_CACHE_THRESHOLD:
            # Fetch the full item only for the best match
            full_item = search_table.get_item(Key={"id": best_id}).get("Item", {})
            print(f"[tabela_search] Semantic HIT: {best_id} score={best_score:.4f}")
            return {
                "content": full_item.get("answer", ""),
                "confidence": round(best_score, 4),
                "documentsUsed": full_item.get("documents_used", []),
            }
        print(f"[tabela_search] No match above threshold (best={best_score:.4f})")
    except Exception as exc:
        print(f"[tabela_search] Semantic search error: {exc}")
    return None


# ---------------------------------------------------------------------------
# Document actions
# ---------------------------------------------------------------------------


def get_upload_presigned_url(data: dict) -> dict:
    file_type = data.get("fileType", ".txt")
    doc_id = data.get("docId") or str(uuid.uuid4())
    s3_key = f"documents/{doc_id}/{doc_id}{file_type}"
    content_type = MIME_MAP.get(file_type, "application/octet-stream")
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key, "ContentType": content_type},
        ExpiresIn=600,
    )
    return response(200, {
        "uploadUrl": upload_url,
        "s3Key": s3_key,
        "docId": doc_id,
        "contentType": content_type,
    })


def upload_document(data: dict) -> dict:
    """Upload document: extract text content, save metadata and embedding to DynamoDB."""
    required = ["fileName", "author", "context", "uploadedBy"]
    for field in required:
        if field not in data:
            return response(400, {"error": f"Missing required field: {field}"})

    doc_id = data.get("docId") or str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    file_type = data.get("fileType", ".txt")
    content = data.get("content", "")

    # Extract real text content from the uploaded file (PDF, DOCX, etc.)
    # instead of storing raw base64 data which has no value for AI.
    file_data_url = data.get("fileDataUrl", "")
    if file_data_url and file_type.lower() in (".pdf", ".docx", ".txt", ".csv", ".json", ".xml"):
        extracted_text = extract_text_from_file(file_data_url, file_type)
        if extracted_text:
            # Truncate to stay within DynamoDB 400KB item size limit
            if len(extracted_text) > MAX_CONTENT_CHARS:
                print(f"[upload] Truncating content from {len(extracted_text)} to {MAX_CONTENT_CHARS} chars")
                extracted_text = extracted_text[:MAX_CONTENT_CHARS]
            content = extracted_text
            print(f"[upload] Extracted {len(content)} chars from {file_type} file")

    # Generate embedding based on extracted content + context + metadata
    # so semantic search can find documents by their actual content.
    tags_text = " ".join(data.get("tags", []))
    embed_text = f"{data.get('context', '')} {data.get('fileName', '')} {tags_text}"
    if content:
        # Include a portion of the extracted content in the embedding
        embed_text += f" {content[:4000]}"
    vector = generate_embedding(embed_text)

    embedding = {
        "model": OPENAI_EMBED_MODEL,
        "dimensions": EMBEDDING_DIMENSIONS,
        "vector": vector,
        "tokenCount": len(embed_text.split()),
        "createdAt": now,
    }

    item = {
        "ID": doc_id,
        "record_type": "document",
        "metadata": {
            "id": doc_id,
            "fileName": data["fileName"],
            "fileType": file_type,
            "fileSize": data.get("fileSize", 0),
            "author": data["author"],
            "context": data["context"],
            "tags": data.get("tags", []),
            "uploadedAt": now,
            "uploadedBy": data["uploadedBy"],
        },
        "content": content,
        "embedding": embedding,
        "createdAt": now,
    }

    if "s3Key" in data and data["s3Key"]:
        item["s3Key"] = data["s3Key"]
        item["fileUrl"] = get_s3_presigned_url(data["s3Key"])
    elif file_data_url:
        s3_key = upload_file_to_s3(doc_id, file_data_url, file_type)
        item["s3Key"] = s3_key
        item["fileUrl"] = get_s3_presigned_url(s3_key)

    if data.get("deletePassword"):
        item["deletePassword"] = data["deletePassword"]

    item = convert_floats_to_decimal(item)
    doc_table.put_item(Item=item)

    result = convert_decimals_to_float(item)
    result.pop("deletePassword", None)
    return response(201, {"message": "Document uploaded successfully", "document": result})


def get_documents(data: dict) -> dict:
    scan_kwargs = {"FilterExpression": Attr("record_type").eq("document")}
    items = []
    start_key = None
    while True:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        resp = doc_table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
    items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    for item in items:
        if "s3Key" in item:
            item["fileUrl"] = get_s3_presigned_url(item["s3Key"])
        item["hasDeletePassword"] = bool(item.get("deletePassword"))
        item.pop("deletePassword", None)
    items = convert_decimals_to_float(items)
    return response(200, {"documents": items, "count": len(items)})


def get_document(data: dict) -> dict:
    doc_id = data.get("documentId")
    if not doc_id:
        return response(400, {"error": "Missing required field: documentId"})
    resp = doc_table.get_item(Key={"ID": doc_id})
    item = resp.get("Item")
    if not item or item.get("record_type") != "document":
        return response(404, {"error": "Document not found"})
    if "s3Key" in item:
        item["fileUrl"] = get_s3_presigned_url(item["s3Key"])
    item["hasDeletePassword"] = bool(item.get("deletePassword"))
    item.pop("deletePassword", None)
    item = convert_decimals_to_float(item)
    return response(200, {"document": item})


def search_documents(data: dict) -> dict:
    query = data.get("query", "").lower().strip()
    if not query:
        return get_documents(data)

    # Try semantic search via DynamoDB cosine similarity
    query_vec = generate_embedding(query)
    if query_vec:
        hits = semantic_search(query_vec, k=10)
        if hits:
            results = []
            for hit in hits:
                did = hit.get("doc_id")
                if not did:
                    continue
                r = doc_table.get_item(Key={"ID": did})
                item = r.get("Item")
                if item and item.get("record_type") == "document":
                    if "s3Key" in item:
                        item["fileUrl"] = get_s3_presigned_url(item["s3Key"])
                    item["hasDeletePassword"] = bool(item.get("deletePassword"))
                    item.pop("deletePassword", None)
                    item["searchScore"] = hit.get("score", 0)
                    results.append(item)
            if results:
                results = convert_decimals_to_float(results)
                return response(200, {"documents": results, "count": len(results), "searchType": "semantic"})

    # Fallback: text search in DynamoDB
    scan_kwargs = {"FilterExpression": Attr("record_type").eq("document")}
    items = []
    start_key = None
    while True:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        resp = doc_table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
    filtered = []
    for item in items:
        meta = item.get("metadata", {})
        searchable = " ".join([
            meta.get("fileName", ""),
            meta.get("author", ""),
            meta.get("context", ""),
            " ".join(meta.get("tags", [])),
            item.get("content", ""),
        ]).lower()
        if query in searchable:
            filtered.append(item)
    filtered.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    for item in filtered:
        if "s3Key" in item:
            item["fileUrl"] = get_s3_presigned_url(item["s3Key"])
        item["hasDeletePassword"] = bool(item.get("deletePassword"))
        item.pop("deletePassword", None)
    filtered = convert_decimals_to_float(filtered)
    return response(200, {"documents": filtered, "count": len(filtered), "searchType": "keyword"})


def delete_document(data: dict) -> dict:
    doc_id = data.get("documentId")
    password = data.get("password")
    if not doc_id:
        return response(400, {"error": "Missing required field: documentId"})
    if not password:
        return response(400, {"error": "Missing required field: password"})
    resp = doc_table.get_item(Key={"ID": doc_id})
    item = resp.get("Item")
    if not item or item.get("record_type") != "document":
        return response(404, {"error": "Document not found"})
    doc_password = item.get("deletePassword")
    if doc_password:
        if password != doc_password:
            return response(403, {"error": "Invalid password"})
    else:
        if password != DELETE_PASSWORD:
            return response(403, {"error": "Invalid password"})
    if "s3Key" in item:
        delete_s3_file(item["s3Key"])
    doc_table.delete_item(Key={"ID": doc_id})
    return response(200, {"message": "Document deleted successfully", "documentId": doc_id})


# ---------------------------------------------------------------------------
# Chat actions  (uses chat_memory_user table)
# ---------------------------------------------------------------------------


def create_chat(data: dict) -> dict:
    required = ["title", "createdBy"]
    for field in required:
        if field not in data:
            return response(400, {"error": f"Missing required field: {field}"})
    chat_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    author_token = str(uuid.uuid4())
    item = {
        "ID": chat_id,
        "record_type": "chat",
        "title": data["title"],
        "createdAt": now,
        "createdBy": data["createdBy"],
        "authorToken": author_token,
        "isPublic": True,
        "messages": [],
    }
    chat_table.put_item(Item=item)
    return response(201, {"message": "Chat created successfully", "chat": convert_decimals_to_float(item)})


def get_chats(data: dict) -> dict:
    scan_kwargs = {"FilterExpression": Attr("record_type").eq("chat")}
    items = []
    start_key = None
    while True:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        resp = chat_table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
    items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)
    safe_items = []
    for item in items:
        safe_item = {k: v for k, v in item.items() if k != "authorToken"}
        safe_item["messageCount"] = len(item.get("messages", []))
        safe_items.append(safe_item)
    safe_items = convert_decimals_to_float(safe_items)
    return response(200, {"chats": safe_items, "count": len(safe_items)})


def get_chat(data: dict) -> dict:
    chat_id = data.get("chatid")
    if not chat_id:
        return response(400, {"error": "Missing required field: chatid"})
    resp = chat_table.get_item(Key={"ID": chat_id})
    item = resp.get("Item")
    if not item or item.get("record_type") != "chat":
        return response(404, {"error": "Chat not found"})
    item = convert_decimals_to_float(item)
    return response(200, {"chat": item})


def send_message(data: dict) -> dict:
    """Memory-augmented chat: tabela_search -> document context -> AI -> save.

    Flow (transparent to user via source field):
    1. Check tabela_search for a cached/similar previous AI response
    2. If no cache hit, search documents for related content
    3. Generate new AI response with document context (if any)
    4. Save the new response to tabela_search for future reuse
    """
    chat_id = data.get("chatid")
    message_content = data.get("message")
    author_token = data.get("authorToken")
    if not chat_id:
        return response(400, {"error": "Missing required field: chatid"})
    if not message_content:
        return response(400, {"error": "Missing required field: message"})
    if not author_token:
        return response(400, {"error": "Missing required field: authorToken"})
    resp = chat_table.get_item(Key={"ID": chat_id})
    item = resp.get("Item")
    if not item or item.get("record_type") != "chat":
        return response(404, {"error": "Chat not found"})
    if item.get("authorToken") != author_token:
        return response(403, {"error": "Only the chat author can send messages"})

    now = datetime.utcnow().isoformat() + "Z"
    messages = item.get("messages", [])

    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": message_content,
        "timestamp": now,
    }
    messages.append(user_msg)

    # ---- Memory pipeline ----
    source_type = "new"
    confidence = 0.0
    documents_used = []
    cached = False

    # Generate embedding for the question (used by both search steps)
    query_vec = generate_embedding(message_content)

    # Step 1: Check tabela_search for a cached/similar previous AI response
    search_resp = search_in_search_table(message_content, query_vec)
    if search_resp:
        ai_content = search_resp["content"]
        source_type = "tabela_search"
        confidence = search_resp.get("confidence", 0.95)
        documents_used = search_resp.get("documentsUsed", [])
        cached = True
        print("[chat] tabela_search HIT - reusing cached response")
    else:
        # Step 2: Search documents for related content
        context_docs = semantic_search(query_vec, k=5) if query_vec else []

        # Filter documents by similarity threshold
        relevant_docs = [
            d for d in context_docs
            if d.get("score", 0) >= SIMILARITY_THRESHOLD
        ]
        documents_used = [
            d.get("doc_id", "") for d in relevant_docs if d.get("doc_id")
        ]

        if relevant_docs:
            max_score = max(d.get("score", 0) for d in relevant_docs)
            confidence = round(min(max_score, 1.0), 4)
            source_type = "document_context"
            print(f"[chat] Found {len(relevant_docs)} relevant documents (best score={max_score:.4f})")
        else:
            confidence = 0.5
            source_type = "new"
            print("[chat] No relevant documents found, generating without context")

        # Step 3: Generate answer via AI with document context (if any)
        ai_content = generate_ai_response(
            message_content, relevant_docs, messages[:-1]
        )

        # Step 4: Save the new response to tabela_search for future reuse
        save_to_search_table(
            question=message_content,
            answer=ai_content,
            question_embedding=query_vec,
            confidence=confidence,
            documents_used=documents_used,
            chat_id=chat_id,
        )

        # Also save to Elasticsearch and ElastiCache (legacy, if configured)
        save_to_elasticsearch(
            question=message_content,
            answer=ai_content,
            confidence=confidence,
            documents_used=documents_used,
            chat_id=chat_id,
        )
        cache_response(message_content, {
            "content": ai_content,
            "confidence": confidence,
            "documentsUsed": documents_used,
        })
        print("[chat] New response saved to tabela_search")

    ai_msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": ai_content,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source": {
            "type": source_type,
            "confidence": confidence,
            "documentsUsed": documents_used,
            "cached": cached,
        },
    }
    messages.append(ai_msg)

    chat_table.update_item(
        Key={"ID": chat_id},
        UpdateExpression="SET messages = :msgs",
        ExpressionAttributeValues={":msgs": convert_floats_to_decimal(messages)},
    )

    return response(200, {
        "userMessage": convert_decimals_to_float(user_msg),
        "aiMessage": convert_decimals_to_float(ai_msg),
    })


def delete_chat(data: dict) -> dict:
    chat_id = data.get("chatid")
    password = data.get("password")
    if not chat_id:
        return response(400, {"error": "Missing required field: chatid"})
    if not password:
        return response(400, {"error": "Missing required field: password"})
    if password != DELETE_PASSWORD:
        return response(403, {"error": "Invalid password"})
    resp = chat_table.get_item(Key={"ID": chat_id})
    item = resp.get("Item")
    if not item or item.get("record_type") != "chat":
        return response(404, {"error": "Chat not found"})
    chat_table.delete_item(Key={"ID": chat_id})
    return response(200, {"message": "Chat deleted successfully", "chatId": chat_id})


# ---------------------------------------------------------------------------
# Action router
# ---------------------------------------------------------------------------

ACTION_MAP = {
    "get_upload_url": get_upload_presigned_url,
    "upload_document": upload_document,
    "get_documents": get_documents,
    "get_document": get_document,
    "search_documents": search_documents,
    "delete_document": delete_document,
    "create_chat": create_chat,
    "get_chats": get_chats,
    "get_chat": get_chat,
    "send_message": send_message,
    "delete_chat": delete_chat,
}


def lambda_handler(event, context):
    if event.get("httpMethod") == "OPTIONS":
        return response(200, {"message": "OK"})
    try:
        body = event.get("body", "{}")
        if isinstance(body, str):
            body = json.loads(body)
        action = body.get("action")
        if not action:
            return response(400, {
                "error": "Missing 'action' field in request body",
                "available_actions": list(ACTION_MAP.keys()),
            })
        handler = ACTION_MAP.get(action)
        if not handler:
            return response(400, {
                "error": f"Unknown action: {action}",
                "available_actions": list(ACTION_MAP.keys()),
            })
        data = {k: v for k, v in body.items() if k != "action"}
        return handler(data)
    except json.JSONDecodeError:
        return response(400, {"error": "Invalid JSON in request body"})
    except Exception as e:
        print(f"Error processing request: {str(e)}")
        return response(500, {"error": "Internal server error", "details": str(e)})
