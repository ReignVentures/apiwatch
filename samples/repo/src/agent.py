import anthropic

MODEL = "claude-opus-4-0"  # hardcoded, will break on retirement

def search(client, q):
    return client.call_tool("search_documents", {"q": q})

def run(prompt):
    return anthropic.Anthropic().messages.create(model=MODEL, max_tokens=500, messages=[{"role": "user", "content": prompt}])
