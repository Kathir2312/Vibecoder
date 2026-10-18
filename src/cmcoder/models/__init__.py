"""Chat models: how cmcoder talks to the model gateway through LangChain.

LANGCHAIN CONCEPT: every LLM in LangChain implements `BaseChatModel`, so the
rest of the engine (agent loop, middleware, summarisation) never knows which
server or vendor is behind it. We use `langchain_openai.ChatOpenAI`, which
speaks the OpenAI Chat Completions format that LiteLLM, Open WebUI, vLLM and
Ollama all serve, and subclass it for Qwen3's extra fields (see chat.py).

Nothing is imported here: `cmcoder.models.gateway` (and the commands that use
it) must load without LangChain, so `cmcoder --help` stays fast.
"""
