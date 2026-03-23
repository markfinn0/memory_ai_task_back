# Memory AI - Backend (Lambda)

AWS Lambda function that serves as the backend for the Memory AI POC application.

## Architecture

- **AWS Lambda** - Single function handling all application logic
- **API Gateway** - Single POST route `/interaction` that invokes the Lambda
- **DynamoDB** - Table `document_tables_memory` with partition key `ID` (UUID)
- **S3** - Bucket `memoryaitest` for file storage (PDFs, CSVs, etc.)
- **OpenAI API** - Text Embedding 3 Small for vector search + GPT-4o Mini for chat generation

## API

**Endpoint:** `POST /interaction`

All requests go to the same endpoint. The `action` field in the request body determines which function to execute.

### Available Actions

| Action | Description |
|--------|-------------|
| `upload_document` | Upload a document with metadata |
| `get_documents` | List all documents |
| `get_document` | Get a single document by ID |
| `search_documents` | Search documents by query |
| `create_chat` | Create a new chat session |
| `get_chats` | List all chat sessions |
| `get_chat` | Get a single chat with messages |
| `send_message` | Send a message and get AI response |
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
  "content": "File text content...",
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

Table: `document_tables_memory`
- **PK:** `ID` (String - UUID)
- **record_type:** `"document"` or `"chat"` (used for filtering)

### Document Record
```json
{
  "ID": "uuid",
  "record_type": "document",
  "metadata": { "fileName": "...", "author": "...", ... },
  "content": "extracted text",
  "s3Key": "documents/uuid/uuid.pdf",
  "fileUrl": "https://... (presigned URL)",
  "embedding": { "model": "...", "vector": [...], ... },
  "createdAt": "ISO timestamp"
}
```

### Chat Record
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

## Deployment

1. Zip the `lambda/lambda_function.py` file
2. Upload to your AWS Lambda function
3. Set environment variables:
   - `DYNAMODB_TABLE`: `document_tables_memory` (default)
   - `S3_BUCKET`: `memoryaitest` (default)
   - `DELETE_PASSWORD`: your chosen password for delete operations
   - `AWS_REGION`: `us-east-1` (default)
   - `OPENAI_API_KEY`: your OpenAI API key
   - `OPENAI_CHAT_MODEL`: `gpt-4o-mini` (default)
   - `OPENAI_EMBED_MODEL`: `text-embedding-3-small` (default)
4. Ensure the Lambda execution role has:
   - DynamoDB read/write permissions for the table
   - S3 read/write permissions for the bucket (`s3:PutObject`, `s3:GetObject`, `s3:DeleteObject`)
5. Install the `requests` package in your Lambda layer (see `requirements.txt`)

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DYNAMODB_TABLE` | `document_tables_memory` | DynamoDB table name |
| `S3_BUCKET` | `memoryaitest` | S3 bucket for file storage |
| `DELETE_PASSWORD` | `memory_ai_delete_2024` | Password for delete operations |
| `AWS_REGION` | `us-east-1` | AWS region |
| `OPENAI_API_KEY` | *(required)* | Your OpenAI API key |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | OpenAI embedding model |
| `OPENAI_CHAT_MODEL` | `gpt-4o-mini` | OpenAI chat model |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI API base URL |
| `OPENAI_MAX_RETRIES` | `3` | Max retry attempts for transient errors |

> **Note:** Embedding dimensions changed to 1536 (text-embedding-3-small default). Existing documents must be re-uploaded to generate new embeddings compatible with the updated model.
