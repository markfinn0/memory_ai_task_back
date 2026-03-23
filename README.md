# Memory AI - Backend (Lambda)

AWS Lambda function that serves as the backend for the Memory AI POC application.

## Architecture

- **AWS Lambda** - Single function handling all application logic
- **API Gateway** - Single POST route `/interaction` that invokes the Lambda
- **DynamoDB** - Tables: `document_tables_memory` (PK: `ID`), `chat_memory_user` (PK: `ID`), `tabela_search` (PK: `id`)
- **S3** - Bucket `memoryaitest` for file storage (PDFs, CSVs, etc.)
- **OpenAI API** - Text Embedding 3 Small for vector search + GPT-4o Mini for chat generation

## API

**Endpoint:** `POST /interaction`

All requests go to the same endpoint. The `action` field in the request body determines which function to execute.

### Available Actions

| Action | Description |
|--------|-------------|
| `upload_document` | Upload a document with metadata (auto-extracts text from PDF/DOCX) |
| `get_documents` | List all documents |
| `get_document` | Get a single document by ID |
| `search_documents` | Search documents by query |
| `create_chat` | Create a new chat session |
| `get_chats` | List all chat sessions |
| `get_chat` | Get a single chat with messages |
| `send_message` | Send a message and get AI response (smart search: cache → documents → AI) |
| `delete_document` | Delete a document (requires password) |
| `delete_chat` | Delete a chat session (requires password) |

### Request Examples

#### Upload Document
```json
{
  "action": "upload_document",
  "fileName": "report.pdf",
  "fileType": ".pdf",
  "fileSize": 2048576,
  "author": "John Doe",
  "context": "Quarterly financial report",
  "tags": ["finance", "report"],
  "uploadedBy": "john.doe",
  "content": "Optional manual text (auto-extracted from PDF/DOCX if fileDataUrl provided)",
  "fileDataUrl": "data:application/pdf;base64,..."
}
```

#### Get All Documents
```json
{
  "action": "get_documents"
}
```

#### Get Single Document
```json
{
  "action": "get_document",
  "documentId": "uuid-here"
}
```

#### Search Documents
```json
{
  "action": "search_documents",
  "query": "finance"
}
```

#### Create Chat
```json
{
  "action": "create_chat",
  "title": "Revenue Discussion",
  "createdBy": "John Doe"
}
```

#### Get All Chats
```json
{
  "action": "get_chats"
}
```

#### Get Single Chat
```json
{
  "action": "get_chat",
  "chatId": "uuid-here"
}
```

#### Send Message
```json
{
  "action": "send_message",
  "chatId": "uuid-here",
  "message": "What is the revenue for Q4?",
  "authorToken": "token-from-cookie"
}
```

#### Delete Document
```json
{
  "action": "delete_document",
  "documentId": "uuid-here",
  "password": "your-delete-password"
}
```

#### Delete Chat
```json
{
  "action": "delete_chat",
  "chatId": "uuid-here",
  "password": "your-delete-password"
}
```

## DynamoDB Table Structure

### Table: `document_tables_memory` (PK: `ID`)

#### Document Record
```json
{
  "ID": "uuid",
  "record_type": "document",
  "metadata": { "fileName": "...", "author": "...", ... },
  "content": "extracted text from PDF/DOCX (not raw file data)",
  "s3Key": "documents/uuid/uuid.pdf",
  "fileUrl": "https://... (presigned URL)",
  "embedding": { "model": "...", "vector": [...], ... },
  "createdAt": "ISO timestamp"
}
```

### Table: `chat_memory_user` (PK: `ID`)

#### Chat Record
```json
{
  "ID": "uuid",
  "record_type": "chat",
  "title": "Chat Title",
  "createdBy": "Author Name",
  "authorToken": "uuid-token",
  "isPublic": true,
  "messages": [{ "id": "...", "role": "user|assistant", "content": "...", ... }],
  "createdAt": "ISO timestamp"
}
```

### Table: `tabela_search` (PK: `id`)

Caches AI responses with embeddings for smart reuse.

```json
{
  "id": "uuid",
  "question": "user question text",
  "question_hash": "sha256 hash for exact match",
  "answer": "AI response text",
  "embedding": { "model": "...", "dimensions": 1536, "vector": [...] },
  "confidence": 0.85,
  "documents_used": ["doc-uuid-1", "doc-uuid-2"],
  "chat_id": "chat-uuid",
  "created_at": "ISO timestamp"
}
```

## Smart Search Flow (send_message)

When a user sends a message, the system follows this pipeline:

1. **Check `tabela_search`** - Looks for a previously cached AI response (exact hash match or semantic similarity >= 0.85). If found, reuses it instantly.
2. **Search documents** - If no cache hit, searches uploaded documents by semantic similarity. Documents with score >= 0.3 are used as context.
3. **Generate AI response** - Calls OpenAI with document context (if any) and chat history.
4. **Save to `tabela_search`** - Stores the new response with its embedding for future reuse.

The response includes a `source` field so the frontend can show the user where the answer came from:
- `"tabela_search"` - Reused from a previous similar question
- `"document_context"` - Generated using uploaded document content
- `"new"` - Generated without any document context

## Document Content Extraction

When uploading documents, the system automatically extracts readable text:
- **PDF** (.pdf) - Extracted using `pdfplumber` (page by page)
- **Word** (.docx) - Extracted using `python-docx` (paragraph by paragraph)
- **Text files** (.txt, .csv, .json, .xml) - Decoded as UTF-8

The extracted text is stored in the `content` field and used for embedding generation, replacing raw file data which has no value for AI.

## Deployment

1. Zip the `lambda/lambda_function.py` file
2. Upload to your AWS Lambda function
3. Set environment variables:
   - `DYNAMODB_TABLE`: `document_tables_memory` (default)
   - `CHAT_TABLE`: `chat_memory_user` (default)
   - `SEARCH_TABLE`: `tabela_search` (default)
   - `S3_BUCKET`: `memoryaitest` (default)
   - `DELETE_PASSWORD`: your chosen password for delete operations
   - `AWS_REGION`: `us-east-1` (default)
   - `OPENAI_API_KEY`: your OpenAI API key
   - `OPENAI_CHAT_MODEL`: `gpt-4o-mini` (default)
   - `OPENAI_EMBED_MODEL`: `text-embedding-3-small` (default)
4. Ensure the Lambda execution role has:
   - DynamoDB read/write permissions for all three tables
   - S3 read/write permissions for the bucket (`s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`)
5. Install dependencies in your Lambda layer (see `requirements.txt`): `requests`, `pdfplumber`, `python-docx`

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DYNAMODB_TABLE` | `document_tables_memory` | DynamoDB document table name |
| `CHAT_TABLE` | `chat_memory_user` | DynamoDB chat table name |
| `SEARCH_TABLE` | `tabela_search` | DynamoDB search/cache table name |
| `S3_BUCKET` | `memoryaitest` | S3 bucket for file storage |
| `DELETE_PASSWORD` | `memory_ai_delete_2024` | Password for delete operations |
| `AWS_REGION` | `us-east-1` | AWS region |
| `OPENAI_API_KEY` | *(required)* | Your OpenAI API key |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | OpenAI embedding model |
| `OPENAI_CHAT_MODEL` | `gpt-4o-mini` | OpenAI chat model |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API base URL |
| `OPENAI_MAX_RETRIES` | `3` | Max retry attempts for transient errors |
| `SIMILARITY_THRESHOLD` | `0.3` | Min score for document context relevance |
| `SEARCH_CACHE_THRESHOLD` | `0.85` | Min score for reusing cached AI responses |

> **Note:** Embedding dimensions changed to 1536 (text-embedding-3-small default). Existing documents must be re-uploaded to generate new embeddings compatible with the updated model.
