import json
import uuid
import hashlib
import base64
import ssl
import boto3
import os
from datetime import datetime
from decimal import Decimal
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from boto3.dynamodb.conditions import Attr

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

# Gemini
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_EMBED_URL = "https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent"
GEMINI_CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
EMBEDDING_DIMENSIONS = 768

# OpenSearch
OPENSEARCH_ENDPOINT = os.environ.get(
    "OPENSEARCH_ENDPOINT",
    "https://search-memoryai-ebj55yqzcpswtjpb6lzmuknanu.us-east-1.es.amazonaws.com",
)
OPENSEARCH_INDEX = os.environ.get("OPENSEARCH_INDEX", "documents")

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

_ssl_ctx = ssl.create_default_context()

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
# Gemini helpers
# ---------------------------------------------------------------------------


def generate_embedding(text: str) -> list:
    """Call Gemini text-embedding-004 and return the vector (768-d)."""
    if not GEMINI_API_KEY:
        print("[embed] No GEMINI_API_KEY, returning empty vector")
        return []
    url = f"{GEMINI_EMBED_URL}?key={GEMINI_API_KEY}"
    payload = json.dumps({
        "model": "models/text-embedding-004",
        "content": {"parts": [{"text": text[:8000]}]},
    }).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=15, context=_ssl_ctx) as resp:
            data = json.loads(resp.read())
        return data.get("embedding", {}).get("values", [])
    except Exception as exc:
        print(f"[embed] Gemini embedding error: {exc}")
        return []


def generate_ai_response(question: str, context_docs: list, chat_history: list) -> str:
    """Call Gemini to generate a chat answer given context documents."""
    if not GEMINI_API_KEY:
        return (
            f'I received your question: "{question}". '
            "However, the AI service is not configured yet. "
            "Please set the GEMINI_API_KEY environment variable."
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

    url = f"{GEMINI_CHAT_URL}?key={GEMINI_API_KEY}"
    payload = json.dumps({
        "contents": [
            {"role": "user", "parts": [{"text": system_prompt + "\n\nQuestion: " + question}]},
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 1024,
        },
    }).encode()

    req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=30, context=_ssl_ctx) as resp:
            data = json.loads(resp.read())
        candidates = data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            if parts:
                return parts[0].get("text", "No response generated.")
        return "No response generated by the AI model."
    except Exception as exc:
        print(f"[gemini] Chat error: {exc}")
        return f"I encountered an error while processing your question. Error: {exc}"


# ---------------------------------------------------------------------------
# OpenSearch helpers
# ---------------------------------------------------------------------------


def _os_request(method: str, path: str, body: dict = None) -> dict:
    """Make a signed request to OpenSearch using IAM (SigV4)."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    import botocore.session

    url = f"{OPENSEARCH_ENDPOINT}/{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}

    session = botocore.session.get_session()
    credentials = session.get_credentials()
    if credentials is None:
        print("[opensearch] No AWS credentials for SigV4")
        return {}
    frozen = credentials.get_frozen_credentials()
    aws_req = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(frozen, "es", REGION).add_auth(aws_req)

    req = Request(url, data=data, method=method)
    for k, v in dict(aws_req.headers).items():
        req.add_header(k, v)

    try:
        with urlopen(req, timeout=10, context=_ssl_ctx) as resp:
            return json.loads(resp.read())
    except HTTPError as exc:
        body_bytes = exc.read() if hasattr(exc, "read") else b""
        print(f"[opensearch] {method} {path} -> {exc.code}: {body_bytes[:500]}")
        return {"error": True, "status": exc.code, "body": body_bytes.decode("utf-8", errors="replace")}
    except Exception as exc:
        print(f"[opensearch] {method} {path} error: {exc}")
        return {"error": True, "message": str(exc)}


_os_index_checked = False


def ensure_opensearch_index():
    """Create the OpenSearch index with kNN mapping if it does not exist."""
    global _os_index_checked
    if _os_index_checked:
        return
    result = _os_request("GET", OPENSEARCH_INDEX)
    if result.get("error") and result.get("status") == 404:
        mapping = {
            "settings": {"index": {"knn": True}},
            "mappings": {
                "properties": {
                    "doc_id": {"type": "keyword"},
                    "content": {"type": "text"},
                    "context": {"type": "text"},
                    "file_name": {"type": "keyword"},
                    "author": {"type": "keyword"},
                    "tags": {"type": "keyword"},
                    "embedding": {
                        "type": "knn_vector",
                        "dimension": EMBEDDING_DIMENSIONS,
                        "method": {
                            "name": "hnsw",
                            "engine": "nmslib",
                            "space_type": "cosinesimil",
                        },
                    },
                    "created_at": {"type": "date"},
                },
            },
        }
        res = _os_request("PUT", OPENSEARCH_INDEX, mapping)
        print(f"[opensearch] Index created: {res}")
    _os_index_checked = True


def index_document_opensearch(doc_id: str, content: str, embedding: list, metadata: dict):
    """Index a document in OpenSearch for semantic search."""
    if not embedding:
        print(f"[opensearch] Skipping index for {doc_id}: no embedding")
        return
    ensure_opensearch_index()
    body = {
        "doc_id": doc_id,
        "content": content[:10000],
        "context": metadata.get("context", ""),
        "file_name": metadata.get("fileName", ""),
        "author": metadata.get("author", ""),
        "tags": metadata.get("tags", []),
        "embedding": embedding,
        "created_at": datetime.utcnow().isoformat(),
    }
    result = _os_request("PUT", f"{OPENSEARCH_INDEX}/_doc/{doc_id}", body)
    print(f"[opensearch] Indexed {doc_id}: {result.get('result', result)}")


def delete_document_opensearch(doc_id: str):
    """Remove a document from OpenSearch."""
    _os_request("DELETE", f"{OPENSEARCH_INDEX}/_doc/{doc_id}")


def semantic_search(query_embedding: list, k: int = 5) -> list:
    """Perform kNN semantic search in OpenSearch."""
    if not query_embedding:
        return []
    body = {
        "size": k,
        "query": {"knn": {"embedding": {"vector": query_embedding, "k": k}}},
        "_source": ["doc_id", "content", "context", "file_name", "author", "tags"],
    }
    result = _os_request("POST", f"{OPENSEARCH_INDEX}/_search", body)
    hits = result.get("hits", {}).get("hits", [])
    docs = []
    for hit in hits:
        src = hit.get("_source", {})
        docs.append({
            "doc_id": src.get("doc_id"),
            "content": src.get("content", ""),
            "score": hit.get("_score", 0),
            "metadata": {
                "fileName": src.get("file_name", ""),
                "author": src.get("author", ""),
                "context": src.get("context", ""),
                "tags": src.get("tags", []),
            },
        })
    return docs


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
    """Upload document: save metadata to DynamoDB, generate embedding, index in OpenSearch."""
    required = ["fileName", "author", "context", "uploadedBy"]
    for field in required:
        if field not in data:
            return response(400, {"error": f"Missing required field: {field}"})

    doc_id = data.get("docId") or str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    file_type = data.get("fileType", ".txt")
    content = data.get("content", "")

    # Generate real embedding via Gemini
    embed_text = f"{data.get('context', '')} {data.get('fileName', '')} {content}"
    vector = generate_embedding(embed_text)

    embedding = {
        "model": "text-embedding-004",
        "dimensions": EMBEDDING_DIMENSIONS,
        "vector": vector[:20] if vector else [],
        "fullVectorSize": len(vector),
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

    # Index in OpenSearch (non-fatal if it fails)
    try:
        index_document_opensearch(doc_id, content, vector, data)
    except Exception as exc:
        print(f"[upload] OpenSearch indexing failed (non-fatal): {exc}")

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

    # Try semantic search first via OpenSearch
    query_vec = generate_embedding(query)
    if query_vec:
        os_hits = semantic_search(query_vec, k=10)
        if os_hits:
            results = []
            for hit in os_hits:
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
    try:
        delete_document_opensearch(doc_id)
    except Exception as exc:
        print(f"[delete] OpenSearch removal failed (non-fatal): {exc}")
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
        # Step 2: Semantic search in OpenSearch
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
