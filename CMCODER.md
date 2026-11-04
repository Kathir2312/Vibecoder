# cmcoder: rules for working on this repository

- **Use LangChain and LangGraph wherever one of their building blocks applies**: a
  built-in middleware, `BaseTool`/`StructuredTool` with `ToolRuntime`, `BaseStore`,
  `PromptTemplate`, `create_agent`, `StateGraph`, `ContextOverflowError`,
  `count_tokens_approximately`, message utilities. Write our own code only where they
  have nothing that fits (a shell-command parser, secret detection, the stdio protocol,
  the permission rules), and say so in the code's header comment.
- Before hand-writing something general (retry, trimming, summarising, state, storage,
  prompts, structured output), look in `langchain`, `langchain-core` and `langgraph` first.
- Do not open a pull request unless asked.
- Checks before a push: `ruff check src tests`, `ruff format --check src tests`, `pyright`,
  `pytest`.
