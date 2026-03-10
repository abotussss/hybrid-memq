# Long Context Chunking

## Overview

The long context chunking system automatically handles documents that exceed embedding model context limits by splitting them into manageable chunks and computing averaged embeddings.

## Problem Solved

When embedding very long documents or messages, you might encounter errors like:

```
Input length exceeds context length: 12453 tokens. Maximum length: 8192 tokens.
```

This plugin now handles such cases gracefully by:
1. Detecting context length errors before they cause failures
2. Automatically splitting the document into overlapping chunks
3. Embedding each chunk separately
4. Computing an averaged embedding that preserves semantic meaning

## How It Works

### Chunking Strategy

The chunker uses a **semantic-aware** approach:

- **Splits at sentence boundaries** when possible (better for preserving meaning)
- **Configurable overlap** (default: 200 characters) to maintain context across chunks
- **Adapts to model context limits** based on the embedding model
- **Forced splits** at hard limits if sentence boundaries are not found

### Chunking Flow

```
Long Document
    │
    ├── 8192+ characters ──┐
                            │
                            ▼
                ┌─────────────────┐
                │  Detect Overflow │
                └────────┬────────┘
                         │
                         ▼
                ┌─────────────────┐
                │  Split into     │
                │  Overlapping     │
                │  Chunks          │
                └────────┬────────┘
                         │
    ┌────────────────────┼────────────────────┐
    │                    │                    │
    ▼                    ▼                    ▼
┌────────┐         ┌────────┐         ┌────────┐
│ Chunk 1│         │ Chunk 2│         │ Chunk 3│
│  [1-2k]│         │[1.8k-3.8k]│    │[3.6k-5.6k]│
└───┬────┘         └───┬────┘         └───┬────┘
    │                  │                  │
    ▼                  ▼                  ▼
Embedding          Embedding          Embedding
    │                  │                  │
    └──────────────────┼──────────────────┘
                       │
                       ▼
              Compute Average
                       │
                       ▼
              Final Embedding
```

## Configuration

### Default Settings

The chunker automatically adapts to your embedding model:

- **maxChunkSize**: 70% of model context limit (e.g., 5734 for 8192-token model)
- **overlapSize**: 5% of model context limit
- **minChunkSize**: 10% of model context limit
- **semanticSplit**: true (prefer sentence boundaries)
- **maxLinesPerChunk**: 50 lines

### Disabling Auto-Chunking

If you prefer to handle chunking manually or want the model to fail on long documents:

```json
{
  "plugins": {
    "entries": {
      "memory-lancedb-pro": {
        "enabled": true,
        "config": {
          "embedding": {
            "apiKey": "${JINA_API_KEY}",
            "model": "jina-embeddings-v5-text-small",
            "chunking": false  // Disable auto-chunking
          }
        }
      }
    }
  }
}
```

### Custom Chunking Parameters

For advanced users who want to tune chunking behavior:

```json
{
  "plugins": {
    "entries": {
      "memory-lancedb-pro": {
        "enabled": true,
        "config": {
          "embedding": {
            "autoChunk": {
              "maxChunkSize": 2000,      // Characters per chunk
              "overlapSize": 500,          // Overlap between chunks
              "minChunkSize": 500,         // Minimum acceptable chunk size
              "semanticSplit": true,       // Prefer sentence boundaries
              "maxLinesPerChunk": 100      // Max lines before forced split
            }
          }
        }
      }
    }
  }
}
```

## Supported Models

The chunker automatically adapts to these embedding models:

| Model | Context Limit | Chunk Size | Overlap |
|-------|---------------|------------|----------|
| Jina jina-embeddings-v5-text-small | 8192 | 5734 | 409 |
| OpenAI text-embedding-3-small | 8192 | 5734 | 409 |
| OpenAI text-embedding-3-large | 8192 | 5734 | 409 |
| Gemini gemini-embedding-001 | 2048 | 1433 | 102 |

## Performance Considerations

### Token Savings

- **Without chunking**: 1 failed embedding (retries required)
- **With chunking**: 3-4 chunk embeddings (1 avg result)
- **Net cost increase**: ~3x for long documents (>8k tokens)
- **Trade-off**: Gracefully handling vs. processing smaller documents

### Caching

Chunked embeddings are cached by their original document hash, so:
- Subsequent requests for the same document get the cached averaged embedding
- Cache hit rate improves as long documents are processed repeatedly

### Processing Time

- **Small documents (<4k chars)**: No chunking, same as before
- **Medium documents (4k-8k chars)**: No chunking, same as before
- **Long documents (>8k chars)**: ~100-200ms additional chunking overhead

## Logging & Debugging

### Enable Debug Logging

To see chunking in action, you can check the logs:

```
Document exceeded context limit (...), attempting chunking...
Split document into 3 chunks for embedding
Successfully embedded long document as 3 averaged chunks
```

### Common Scenarios

**Scenario 1: Long memory text**
- When a user's message or system prompt is very long
- Automatically chunked before embedding
- No error thrown, memory is still stored and retrievable

**Scenario 2: Batch embedding long documents**
- If some documents in a batch exceed limits
- Only the long ones are chunked
- Successful documents processed normally

## Troubleshooting

### Chunking Still Fails

If you still see context length errors:

1. **Verify model**: Check which embedding model you're using
2. **Increase minChunkSize**: May need smaller chunks for some models
3. **Disable autoChunk**: Handle chunking manually with explicit split

### Too Many Small Chunks

If chunking creates many tiny fragments:

1. **Increase minChunkSize**: Larger minimum chunk size
2. **Reduce overlap**: Less overlap between chunks means more efficient chunks

### Embedding Quality Degradation

If chunked embeddings seem less accurate:

1. **Increase overlap**: More context between chunks preserves relationships
2. **Use smaller maxChunkSize**: Split into more, smaller overlapping pieces
3. **Consider hierarchical approach**: Use a two-pass retrieval (chunk → document → full text)

## Future Enhancements

Planned improvements:

- [ ] **Hierarchical chunking**: Chunk → document-level embedding
- [ ] **Sliding window**: Different overlap strategies per document complexity
- [ ] **Smart summarization**: Summarize chunks before averaging for better quality
- [ ] **Context-aware overlap**: Dynamic overlap based on document complexity
- [ ] **Async chunking**: Process chunks in parallel for batch operations

## Technical Details

### Algorithm

1. **Detect overflow**: Check if document exceeds maxChunkSize
2. **Split semantically**: Find sentence boundaries within target range
3. **Create overlap**: Include overlap with previous chunk's end
4. **Embed in parallel**: Process all chunks simultaneously
5. **Average the result**: Compute mean embedding across all chunks

### Complexity

- **Time**: O(n × k) where n = number of chunks, k = average chunk processing time
- **Space**: O(n × d) where d = embedding dimension

### Edge Cases

| Case | Handling |
|------|----------|
| Empty document | Returns empty embedding immediately |
| Very small documents | No chunking, normal processing |
| Perfect boundaries | Split at sentence ends, no truncation |
| No boundaries found | Hard split at max position |
| Single oversized chunk | Process as-is, let provider error |
| All chunks too small | Last chunk takes remaining text |

## References

- [LanceDB Documentation](https://lancedb.com)
- [OpenAI Embedding Context Limits](https://platform.openai.com/docs/guides/embeddings)
- [Semantic Chunking Research](https://arxiv.org/abs/2310.05970)

---

*This feature was added to handle long-context documents gracefully without losing memory quality.*
