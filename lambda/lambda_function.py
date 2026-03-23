import json
import uuid
import hashlib
import base64
import math
import boto3
import os
import time
from datetime import datetime
from decimal import Decimal
from boto3.dynamodb.conditions import Attr

try:
    import requests as _requests
except ImportError:
    from urllib.request import Request, urlopen
    _requests = None

# ---------------------------------------------------------------------------
# AWS clients & config
# ---------------------------------------------------------------------------

REGION = os.environ.get("AWS_REGION", "us-east-1")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
DOC_TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "document_tables_memory")
CHAT_TABLE_NAME = os.environ.get("CHAT_TABLE", "chat_memory_user")
doc_table = dynamodb.Table(DOC_TABLE_NAME)
chat_table = dynamodb.Table(CHAT_TABLE_NAME)

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
    """Scan all documents in DynamoDB and rank by cosine similarity."""
    if not query_embedding:
        return []

    # Convert query embedding floats for comparison
    query_vec = [float(v) for v in query_embedding]

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
            "content": item.get("content", ""),
            "score": round(score, 4),
            "metadata": {
                "fileName": meta.get("fileName", ""),
                "author": meta.get("author", ""),
                "context": meta.get("context", ""),
                "tags": meta.get("tags", []),
            },
        })

    # Sort by score descending and return top-k
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


# ---------------------------------------------------------------------------
# ElastiCache / Valkey helpers
# ---------------------------------------------------------------------------


def _cache_key(question: str) -> str:
    return "memoryai:answer:" + hashlib.sha256(question.strip().lower().encode()).hexdigest()


def get_cached_response(question: str):
    """Look up a cached AI response by question hash."""
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
    """Cache an AI response with TTL."""
    rc = _get_redis()
    if rc is None:
        return
    try:
        rc.setex(_cache_key(question), CACHE_TTL, json.dumps(data, default=str))
    except Exception as exc:
        print(f"[cache] SET error: {exc}")


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
    """Upload document: save metadata and full embedding to DynamoDB."""
    required = ["fileName", "author", "context", "uploadedBy"]
    for field in required:
        if field not in data:
            return response(400, {"error": f"Missing required field: {field}"})

    doc_id = data.get("docId") or str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    file_type = data.get("fileType", ".txt")
    content = data.get("content", "")

    # Generate embedding via OpenAI
    embed_text = f"{data.get('context', '')} {data.get('fileName', '')} {content}"
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
    elif "fileDataUrl" in data and data["fileDataUrl"]:
        s3_key = upload_file_to_s3(doc_id, data["fileDataUrl"], file_type)
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
    """Memory-augmented chat: cache -> semantic search -> Gemini -> cache."""
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

    # Step 1: Check ElastiCache / Valkey
    cached_resp = get_cached_response(message_content)
    if cached_resp:
        ai_content = cached_resp["content"]
        source_type = "reused"
        confidence = cached_resp.get("confidence", 0.95)
        documents_used = cached_resp.get("documentsUsed", [])
        cached = True
        print("[chat] Cache HIT for question hash")
    else:
        # Step 2: Semantic search in DynamoDB
        query_vec = generate_embedding(message_content)
        context_docs = semantic_search(query_vec, k=5) if query_vec else []
        documents_used = [d.get("doc_id", "") for d in context_docs if d.get("doc_id")]

        if context_docs:
            max_score = max(d.get("score", 0) for d in context_docs)
            confidence = round(min(max_score, 1.0), 4)
        else:
            confidence = 0.5

        # Step 3: Generate answer via Gemini
        ai_content = generate_ai_response(message_content, context_docs, messages[:-1])

        # Step 4: Cache the response
        cache_response(message_content, {
            "content": ai_content,
            "confidence": confidence,
            "documentsUsed": documents_used,
        })
        print("[chat] Cache MISS - generated new response, cached it")

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
