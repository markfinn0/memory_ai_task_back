import json
import uuid
import random
import base64
import boto3
import os
from datetime import datetime
from decimal import Decimal
from boto3.dynamodb.conditions import Attr

# DynamoDB setup
dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "document_tables_memory")
table = dynamodb.Table(TABLE_NAME)

# S3 setup
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))
S3_BUCKET = os.environ.get("S3_BUCKET", "memoryaitest")

# Delete password (set via environment variable)
DELETE_PASSWORD = os.environ.get("DELETE_PASSWORD", "memory_ai_delete_2024")

# ---------- helpers ----------

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
    """Convert float values to Decimal for DynamoDB storage."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: convert_floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_floats_to_decimal(i) for i in obj]
    return obj


def convert_decimals_to_float(obj):
    """Convert Decimal values back to float for API responses."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: convert_decimals_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_decimals_to_float(i) for i in obj]
    return obj


# ---------- action handlers ----------


def upload_file_to_s3(doc_id: str, file_data_url: str, file_type: str) -> str:
    """Upload file data (base64 data URL) to S3 and return the S3 key."""
    # Parse the data URL: "data:<mime>;base64,<data>"
    if "," in file_data_url:
        header, encoded = file_data_url.split(",", 1)
    else:
        encoded = file_data_url
        header = ""

    file_bytes = base64.b64decode(encoded)

    # Determine content type from header or file extension
    content_type = "application/octet-stream"
    if header.startswith("data:"):
        content_type = header.split(":")[1].split(";")[0]
    else:
        mime_map = {
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
        content_type = mime_map.get(file_type, content_type)

    s3_key = f"documents/{doc_id}/{doc_id}{file_type}"

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=file_bytes,
        ContentType=content_type,
    )

    return s3_key


def get_s3_presigned_url(s3_key: str, expires_in: int = 3600) -> str:
    """Generate a presigned URL for an S3 object."""
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": s3_key},
        ExpiresIn=expires_in,
    )


def delete_s3_file(s3_key: str) -> None:
    """Delete a file from S3."""
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
    except Exception as e:
        print(f"Warning: Failed to delete S3 object {s3_key}: {e}")


def upload_document(data: dict) -> dict:
    """Store a document record in DynamoDB with file data in S3."""
    required = ["fileName", "author", "context", "uploadedBy"]
    for field in required:
        if field not in data:
            return response(400, {"error": f"Missing required field: {field}"})

    doc_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat() + "Z"
    file_type = data.get("fileType", ".txt")

    # Build embedding info (mock for now, will be replaced with real embeddings)
    embedding = {
        "model": "text-embedding-ada-002",
        "dimensions": 1536,
        "vector": [round(random.uniform(-1, 1), 6) for _ in range(10)],
        "tokenCount": random.randint(50, 300),
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
        "content": data.get("content", ""),
        "embedding": embedding,
        "createdAt": now,
    }

    # Upload file to S3 if fileDataUrl is provided
    if "fileDataUrl" in data and data["fileDataUrl"]:
        s3_key = upload_file_to_s3(doc_id, data["fileDataUrl"], file_type)
        item["s3Key"] = s3_key
        # Generate a presigned URL for immediate access
        item["fileUrl"] = get_s3_presigned_url(s3_key)

    item = convert_floats_to_decimal(item)
    table.put_item(Item=item)

    result = convert_decimals_to_float(item)
    return response(201, {"message": "Document uploaded successfully", "document": result})


def get_documents(data: dict) -> dict:
    """List all documents from DynamoDB."""
    scan_kwargs = {
        "FilterExpression": Attr("record_type").eq("document"),
    }

    items = []
    done = False
    start_key = None

    while not done:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey", None)
        done = start_key is None

    # Sort by createdAt descending
    items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)

    # Generate fresh presigned URLs for documents with S3 keys
    for item in items:
        if "s3Key" in item:
            item["fileUrl"] = get_s3_presigned_url(item["s3Key"])

    items = convert_decimals_to_float(items)
    return response(200, {"documents": items, "count": len(items)})


def get_document(data: dict) -> dict:
    """Get a single document by ID."""
    doc_id = data.get("documentId")
    if not doc_id:
        return response(400, {"error": "Missing required field: documentId"})

    resp = table.get_item(Key={"ID": doc_id})
    item = resp.get("Item")

    if not item or item.get("record_type") != "document":
        return response(404, {"error": "Document not found"})

    # Generate a fresh presigned URL if the document has an S3 key
    if "s3Key" in item:
        item["fileUrl"] = get_s3_presigned_url(item["s3Key"])

    item = convert_decimals_to_float(item)
    return response(200, {"document": item})


def search_documents(data: dict) -> dict:
    """Search documents by query string across name, author, context, tags, content."""
    query = data.get("query", "").lower().strip()
    if not query:
        return get_documents(data)

    # Get all documents first, then filter in memory
    # (For production, use DynamoDB GSI or Elasticsearch)
    scan_kwargs = {
        "FilterExpression": Attr("record_type").eq("document"),
    }

    items = []
    done = False
    start_key = None

    while not done:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey", None)
        done = start_key is None

    # Filter by query
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

    # Generate fresh presigned URLs for documents with S3 keys
    for item in filtered:
        if "s3Key" in item:
            item["fileUrl"] = get_s3_presigned_url(item["s3Key"])

    filtered = convert_decimals_to_float(filtered)
    return response(200, {"documents": filtered, "count": len(filtered)})


def create_chat(data: dict) -> dict:
    """Create a new chat session."""
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

    table.put_item(Item=item)

    return response(201, {
        "message": "Chat created successfully",
        "chat": convert_decimals_to_float(item),
    })


def get_chats(data: dict) -> dict:
    """List all chat sessions."""
    scan_kwargs = {
        "FilterExpression": Attr("record_type").eq("chat"),
    }

    items = []
    done = False
    start_key = None

    while not done:
        if start_key:
            scan_kwargs["ExclusiveStartKey"] = start_key
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey", None)
        done = start_key is None

    items.sort(key=lambda x: x.get("createdAt", ""), reverse=True)

    # Remove authorToken from public listing for security
    safe_items = []
    for item in items:
        safe_item = {k: v for k, v in item.items() if k != "authorToken"}
        safe_item["messageCount"] = len(item.get("messages", []))
        safe_items.append(safe_item)

    safe_items = convert_decimals_to_float(safe_items)
    return response(200, {"chats": safe_items, "count": len(safe_items)})


def get_chat(data: dict) -> dict:
    """Get a single chat session by ID."""
    chat_id = data.get("chatId")
    if not chat_id:
        return response(400, {"error": "Missing required field: chatId"})

    resp = table.get_item(Key={"ID": chat_id})
    item = resp.get("Item")

    if not item or item.get("record_type") != "chat":
        return response(404, {"error": "Chat not found"})

    # Include authorToken so frontend can compare with cookie
    item = convert_decimals_to_float(item)
    return response(200, {"chat": item})


def send_message(data: dict) -> dict:
    """Send a message in a chat and get AI response."""
    chat_id = data.get("chatId")
    message_content = data.get("message")
    author_token = data.get("authorToken")

    if not chat_id:
        return response(400, {"error": "Missing required field: chatId"})
    if not message_content:
        return response(400, {"error": "Missing required field: message"})
    if not author_token:
        return response(400, {"error": "Missing required field: authorToken"})

    # Get the chat
    resp = table.get_item(Key={"ID": chat_id})
    item = resp.get("Item")

    if not item or item.get("record_type") != "chat":
        return response(404, {"error": "Chat not found"})

    # Verify author
    if item.get("authorToken") != author_token:
        return response(403, {"error": "Only the chat author can send messages"})

    now = datetime.utcnow().isoformat() + "Z"
    messages = item.get("messages", [])

    # Add user message
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": message_content,
        "timestamp": now,
    }
    messages.append(user_msg)

    # Generate mock AI response
    ai_response = generate_mock_ai_response(message_content)
    ai_msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": ai_response["content"],
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source": ai_response["source"],
    }
    messages.append(ai_msg)

    # Update chat in DynamoDB
    table.update_item(
        Key={"ID": chat_id},
        UpdateExpression="SET messages = :msgs",
        ExpressionAttributeValues={":msgs": convert_floats_to_decimal(messages)},
    )

    return response(200, {
        "userMessage": convert_decimals_to_float(user_msg),
        "aiMessage": convert_decimals_to_float(ai_msg),
    })


def generate_mock_ai_response(user_message: str) -> dict:
    """Generate a mock AI response based on keywords in the user message.

    This will be replaced with real AI integration (e.g., Bedrock, OpenAI) later.
    """
    lower_msg = user_message.lower()

    responses = {
        "revenue": {
            "content": "Based on the quarterly report, the revenue for Q4 2025 was $4.2M, which represents a 15% year-over-year growth. Cloud services revenue specifically grew by 28%.",
            "source": {
                "type": "reused",
                "confidence": 0.92,
                "originalChatId": None,
            },
        },
        "projection": {
            "content": "The projections for 2026 include expected revenue growth of 20-25%, planned expansion into European markets, and a new product line launch in Q2.",
            "source": {
                "type": "reused",
                "confidence": 0.90,
                "originalChatId": None,
            },
        },
        "feedback": {
            "content": "The main customer complaints revolve around performance issues, particularly loading times. Positive feedback highlights the new dashboard design and export feature.",
            "source": {
                "type": "reused",
                "confidence": 0.88,
                "originalChatId": None,
            },
        },
        "api": {
            "content": "The Memory Management API v2.0 has several endpoints including POST /documents for uploads, GET /documents/:id for retrieval, and POST /chat for AI interaction. Rate limits are 100 requests per minute and 10 document uploads per hour.",
            "source": {
                "type": "new",
                "confidence": 0.85,
                "documentsUsed": [],
            },
        },
    }

    for keyword, resp in responses.items():
        if keyword in lower_msg:
            return resp

    return {
        "content": f'I\'ve analyzed your question: "{user_message}". Based on the available documents and previous conversations, here is a synthesized response. This is a new analysis that hasn\'t been generated before, drawing from multiple sources in the knowledge base.',
        "source": {
            "type": "new",
            "confidence": 0.75,
            "documentsUsed": [],
        },
    }


def delete_document(data: dict) -> dict:
    """Delete a document by ID (requires password)."""
    doc_id = data.get("documentId")
    password = data.get("password")

    if not doc_id:
        return response(400, {"error": "Missing required field: documentId"})
    if not password:
        return response(400, {"error": "Missing required field: password"})
    if password != DELETE_PASSWORD:
        return response(403, {"error": "Invalid password"})

    # Get document to find S3 key before deleting
    resp = table.get_item(Key={"ID": doc_id})
    item = resp.get("Item")

    if not item or item.get("record_type") != "document":
        return response(404, {"error": "Document not found"})

    # Delete file from S3 if it exists
    if "s3Key" in item:
        delete_s3_file(item["s3Key"])

    # Delete from DynamoDB
    table.delete_item(Key={"ID": doc_id})

    return response(200, {"message": "Document deleted successfully", "documentId": doc_id})


def delete_chat(data: dict) -> dict:
    """Delete a chat session by ID (requires password)."""
    chat_id = data.get("chatId")
    password = data.get("password")

    if not chat_id:
        return response(400, {"error": "Missing required field: chatId"})
    if not password:
        return response(400, {"error": "Missing required field: password"})
    if password != DELETE_PASSWORD:
        return response(403, {"error": "Invalid password"})

    # Verify the chat exists
    resp = table.get_item(Key={"ID": chat_id})
    item = resp.get("Item")

    if not item or item.get("record_type") != "chat":
        return response(404, {"error": "Chat not found"})

    # Delete from DynamoDB
    table.delete_item(Key={"ID": chat_id})

    return response(200, {"message": "Chat deleted successfully", "chatId": chat_id})


# ---------- action router ----------

ACTION_MAP = {
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
    """Main Lambda handler - routes to action functions based on request body."""

    # Handle CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        return response(200, {"message": "OK"})

    try:
        # Parse body
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

        # Pass the data (everything except action) to the handler
        data = {k: v for k, v in body.items() if k != "action"}
        return handler(data)

    except json.JSONDecodeError:
        return response(400, {"error": "Invalid JSON in request body"})
    except Exception as e:
        print(f"Error processing request: {str(e)}")
        return response(500, {"error": "Internal server error", "details": str(e)})
