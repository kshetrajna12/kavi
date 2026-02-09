# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
```

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds



### Hand-off Prompt (Step 7)

After closing a task and pushing, ALWAYS generate a **continuation prompt** for the next session. Format:

```
Continue epic <EPIC-ID> (<epic description>).

**Completed so far:**
- <Phase/task>: <brief description>
- ...

**Next tasks (now unblocked):**
- <TASK-ID> (P1/P2): <task title> - <one-line context>
- ...

**Then:**
- <lower priority items>

Run `bd ready | grep <EPIC-ID>` to see unblocked tasks.
Key docs: <relevant design docs or files>
```

This ensures continuity across sessions without losing context.

## Session Start
- Check current state:
  - `git status`
  - `git pull --rebase` (if appropriate)
- Run tests to establish baseline (pick the fastest meaningful suite):
  - `pytest -q` (or `uv run pytest -q` if using uv)
- Skim recent changes if continuing work:
  - `git log -n 10 --oneline`

## Session End
- If code changed:
  - Run formatting + lint + tests
  - Ensure no debug prints, no stray TODOs without context
- Commit with a tight message (see “Commit Preferences”)
- Push:
  - `git push`
- Verify:
  - `git status` should be clean

## Commit Preferences
- Do NOT include "Co-Authored-By: Claude" or "Generated with Claude Code"
- Prefer small commits that preserve bisectability:
  - one commit per coherent change (schema, extractor, resolver, view, etc.)

## Documentation Discipline
**At the end of every session**, check if documentation needs updating. Key docs to review:

| If you changed... | Update... |
|-------------------|-----------|
| API endpoints | `docs/api_v0.md` (spec + endpoint summary table), `README.md` (usage examples) |
| Predicates | `docs/predicate_registry_v0.md` |
| Database schema | `docs/database_schema.md` |
| Architecture/data flow | `docs/architecture.md` |
| View generators | `docs/materialized_views_design.md` |

Documentation updates are part of completing work, not a separate task.

---
<!-- SPARKSTATION-START -->
# Sparkstation Local LLM Gateway

This project has access to local LLM models through Sparkstation gateway.

## Available Models

- `gpt-oss-20b` - openai/gpt-oss-20b
- `bge-large` - BAAI/bge-large-en-v1.5
- `clip-vit` - openai/clip-vit-large-patch14
- `qwen3-vl-4b` - Qwen/Qwen3-VL-4B-Instruct-FP8
- `flux-dev` - black-forest-labs/FLUX.1-dev

## API Endpoint

- **Base URL**: `http://localhost:8000/v1`
- **Protocol**: OpenAI-compatible API
- **Authentication**: Use any string as API key (e.g., `"dummy-key"`)

## Usage with OpenAI Python SDK

```python
from openai import OpenAI

# Initialize client pointing to local Sparkstation gateway
client = OpenAI(
    api_key="dummy-key",  # Any value works
    base_url="http://localhost:8000/v1"
)

# Make a request
response = client.chat.completions.create(
    model="qwen3-vl-4b",  # or "gpt-oss-20b"
    messages=[
        {"role": "user", "content": "Hello!"}
    ]
)

print(response.choices[0].message.content)
```

## Usage with curl

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy-key" \
  -d '{
    "model": "qwen3-vl-4b",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Streaming

```python
stream = client.chat.completions.create(
    model="qwen3-vl-4b",
    messages=[{"role": "user", "content": "Tell me a story"}],
    stream=True
)

for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

## Vision (Image Analysis)

The `qwen3-vl-4b` model supports vision capabilities. You can pass images via URL or base64:

### With Image URL

```python
response = client.chat.completions.create(
    model="qwen3-vl-4b",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What's in this image?"},
                {"type": "image_url", "image_url": {"url": "https://example.com/image.jpg"}}
            ]
        }
    ]
)
```

### With Base64 Encoded Image

```python
import base64

with open("image.jpg", "rb") as f:
    image_data = base64.b64encode(f.read()).decode('utf-8')

response = client.chat.completions.create(
    model="qwen3-vl-4b",
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe this image"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}
            ]
        }
    ]
)
```

**Note**: Vision requests use more tokens (~5000+ tokens for image processing).

## Reasoning Models

The `gpt-oss-20b` model is a reasoning model that shows its thinking process. Access both the reasoning and final response:

```python
response = client.chat.completions.create(
    model="gpt-oss-20b",
    messages=[{"role": "user", "content": "What is 2+2?"}]
)

# Final answer
print(response.choices[0].message.content)
# Output: "4"

# Reasoning process (if available)
if hasattr(response.choices[0].message, 'reasoning_content'):
    print(response.choices[0].message.reasoning_content)
    # Output: "We need to add 2 and 2. That equals 4."
```

## Embeddings

Sparkstation provides both text and image embedding models for semantic search, RAG, and similarity tasks.

### Text Embeddings (bge-large)

Generate embeddings for text using the `bge-large` model:

```python
# Generate text embeddings
response = client.embeddings.create(
    model="bge-large",
    input="Hello world"
)

# Get embedding vector (1024 dimensions)
embedding = response.data[0].embedding
print(f"Embedding dimensions: {len(embedding)}")
```

### Image Embeddings (CLIP)

The `clip-vit` model generates embeddings for images using OpenAI's CLIP.

**Important**: CLIP embeddings use a structured array format (different from standard OpenAI embeddings API).

#### With Image URL
```python
response = client.embeddings.create(
    model="clip-vit",
    input=[{"image": "https://example.com/image.jpg"}]
)

embedding = response.data[0].embedding  # 768 dimensions
```

#### With Base64 Encoded Image
```python
import base64

with open("image.jpg", "rb") as f:
    image_data = base64.b64encode(f.read()).decode('utf-8')

# Option 1: Raw base64 (simplest)
response = client.embeddings.create(
    model="clip-vit",
    input=[{"image": image_data}]
)

# Option 2: With data URL prefix (also works)
response = client.embeddings.create(
    model="clip-vit",
    input=[{"image": f"data:image/jpeg;base64,{image_data}"}]
)

embedding = response.data[0].embedding  # 768 dimensions
```

**Note**: The input must be an array of objects with `"image"` keys, not flat strings.

### Batch Embeddings

Generate embeddings for multiple inputs at once:

```python
response = client.embeddings.create(
    model="bge-large",
    input=["First document", "Second document", "Third document"]
)

for i, data in enumerate(response.data):
    print(f"Document {i}: {len(data.embedding)} dimensions")
```

### Cross-Modal Search with CLIP

CLIP embeddings enable searching images with text or finding similar images:

```python
# Embed text query (text uses simple string format)
text_response = client.embeddings.create(
    model="clip-vit",
    input="a red car"
)
text_embedding = text_response.data[0].embedding

# Embed image (images use structured format)
image_response = client.embeddings.create(
    model="clip-vit",
    input=[{"image": "https://example.com/car.jpg"}]
)
image_embedding = image_response.data[0].embedding

# Compare via cosine similarity (both in same 768-dim embedding space)
from numpy import dot
from numpy.linalg import norm

similarity = dot(text_embedding, image_embedding) / (norm(text_embedding) * norm(image_embedding))
print(f"Similarity: {similarity}")
```

### Use Cases

- **Semantic Search**: Embed documents and queries, find similar content via cosine similarity
- **RAG (Retrieval Augmented Generation)**: Embed knowledge base for context retrieval
- **Image Search**: Use CLIP to search images by text description or find similar images
- **Cross-Modal Retrieval**: Search images with text queries or text with image queries
- **Classification**: Use embeddings as features for downstream ML tasks

## Image Generation

Sparkstation provides FLUX.1-dev for high-quality image generation via the OpenAI-compatible `/v1/images/generations` endpoint.

### Basic Image Generation

```python
import base64

# Generate an image
response = client.images.generate(
    model="flux-dev",
    prompt="A photorealistic image of a red robot in a garden",
    n=1,
    size="512x512",
    response_format="b64_json"
)

# Save the generated image
image_data = base64.b64decode(response.data[0].b64_json)
with open("generated_image.png", "wb") as f:
    f.write(image_data)
print("Image saved to generated_image.png")
```

### With curl

```bash
curl http://localhost:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer dummy-key" \
  -d '{
    "model": "flux-dev",
    "prompt": "A cyberpunk city at night with neon lights",
    "n": 1,
    "size": "512x512"
  }'
```

### Using requests

```python
import requests
import base64

response = requests.post(
    "http://localhost:8000/v1/images/generations",
    headers={
        "Authorization": "Bearer dummy-key",
        "Content-Type": "application/json"
    },
    json={
        "model": "flux-dev",
        "prompt": "A watercolor painting of mountains at sunset",
        "n": 1,
        "size": "1024x1024"
    },
    timeout=120  # Image generation takes 20-60 seconds
)

if response.ok:
    data = response.json()
    image_b64 = data["data"][0]["b64_json"]
    with open("output.png", "wb") as f:
        f.write(base64.b64decode(image_b64))
    print("Image saved to output.png")
```

### Supported Parameters

| Parameter | Values | Description |
|-----------|--------|-------------|
| `model` | `flux-dev` | FLUX.1-dev image model |
| `prompt` | string | Text description of image to generate |
| `n` | 1 | Number of images (currently 1 supported) |
| `size` | `512x512`, `1024x1024` | Image dimensions |
| `response_format` | `b64_json` | Response format (base64 JSON) |

**Notes**:
- Image generation takes 20-60 seconds depending on size
- FLUX.1-dev produces high-quality photorealistic images
- First request may be slower (model warmup)

## Important Notes

- **Do not start/stop Sparkstation services** - they are managed by the system
- Models are already running and ready to use
- Use the gateway endpoint (`http://localhost:8000/v1`) for all requests
- All models support standard OpenAI APIs:
  - Chat: `/v1/chat/completions` (qwen3-vl-4b, gpt-oss-20b)
  - Embeddings: `/v1/embeddings` (bge-large, clip-vit)
  - Image Generation: `/v1/images/generations` (flux-dev)

### Model-Specific Details

- **Vision Chat** (`qwen3-vl-4b`):
  - Supports image analysis via URL or base64
  - Uses standard OpenAI vision format: `{"type": "image_url", "image_url": {"url": "..."}}`

- **Reasoning** (`gpt-oss-20b`):
  - Includes reasoning traces in `reasoning_content` field

- **Text Embeddings** (`bge-large`):
  - Generates 1024-dim embeddings for text semantic tasks
  - Standard format: `input="text"` or `input=["text1", "text2"]`

- **Image Embeddings** (`clip-vit`):
  - Generates 768-dim embeddings for images and cross-modal search
  - **Special format required**: Images must use `input=[{"image": "..."}]` (not flat strings)
  - Text queries use simple format: `input="text query"`
  - Supports URL, base64 with data URL prefix, or raw base64

- **Image Generation** (`flux-dev`):
  - Generates high-quality images from text prompts using FLUX.1-dev
  - Supports sizes: 512x512, 1024x1024
  - Takes 20-60 seconds per image
  - Returns base64-encoded PNG
<!-- SPARKSTATION-END -->
---
