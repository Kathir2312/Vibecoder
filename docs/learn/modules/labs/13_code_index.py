"""Chapter 13 lab: build a code index, search it, keep it current, and use it
as a LangChain retriever in an LCEL chain.

    python 13_code_index.py http://127.0.0.1:8765/v1 <project dir>

Mock server script: 13_one_answer.json (one reply, for the chain's model call).
The mock server also answers /v1/embeddings for "text-embedding-3-small" with
fake vectors: texts sharing words come out similar.

It writes a small demo project into <project dir>/rag-demo.
"""

import asyncio
import sys
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda, RunnablePassthrough

from cmcoder.config.settings import Settings
from cmcoder.models.chat import build_chat_model
from cmcoder.rag.chunker import chunk_file
from cmcoder.rag.index import open_index

AUTH = '''"""Authentication helpers."""


def refresh_auth_token(session):
    """Refresh the expired auth token with the identity provider."""
    session.token = session.provider.renew(session.refresh_token)
    return session.token


def parse_invoice_total(lines):
    """Sum the amounts of an invoice."""
    return sum(float(x.split(";")[1]) for x in lines)
'''


def format_docs(docs: list[Document]) -> str:
    return "\n\n".join(
        f"{d.metadata['path']}:{d.metadata['start']}-{d.metadata['end']}\n{d.page_content}"
        for d in docs
    )


async def main(base_url: str, project: str) -> None:
    root = Path(project) / "rag-demo"
    root.mkdir(parents=True, exist_ok=True)
    (root / "auth.py").write_text(AUTH)
    (root / "README.md").write_text("# Billing\n\nInvoices are emailed monthly.\n")
    (root / ".env").write_text("TOKEN=topsecret\n")  # a secret file: never indexed

    # 1. Chunking: along definitions, each piece with path, lines and a name.
    print("== chunks of auth.py")
    for c in chunk_file("auth.py", AUTH):
        print(f"  {c.location:14} symbol={c.symbol!r}")

    settings = Settings.model_validate(
        {
            "providers": {"mock": {"baseUrl": base_url}},
            "model": "mock:qwen3-27b",
            "rag": {"embeddingModel": "mock:text-embedding-3-small", "store": {"type": "local"}},
        }
    )
    index = open_index(settings, root)
    assert index is not None
    try:
        # 2. A full update: every file is read, chunked, embedded and stored.
        result = await index.update()
        print(f"\n== update: {result.indexed} files, {result.chunks} chunks")
        print("   store:", type(index.store).__name__, "| manifest:", index.manifest_path.name)

        # 3. Search by meaning (CodeIndex.search -> the VectorStore).
        print("\n== search: 'refresh the expired auth token'")
        for hit in await index.search("refresh the expired auth token", k=2):
            print(f"  {hit.score:.2f}  {hit.chunk.location}  ({hit.chunk.symbol})")

        # 4. Incremental: nothing changed, then one file changed.
        result = await index.update()
        print(f"\n== update again: {result.indexed} indexed, {result.unchanged} unchanged")
        (root / "README.md").write_text("# Billing\n\nInvoices are emailed weekly.\n")
        result = await index.update()
        print(f"== after editing README.md: {result.indexed} indexed, {result.unchanged} unchanged")

        # 5. The store is a LangChain VectorStore, so it is also a retriever,
        #    and a retriever is a Runnable you can put in an LCEL chain.
        retriever = index.store.as_retriever(search_kwargs={"k": 1})
        prompt = ChatPromptTemplate.from_messages(
            [
                ("system", "Answer from this code only:\n\n{context}"),
                ("human", "{question}"),
            ]
        )
        model = build_chat_model(settings).model
        chain = (
            {"context": retriever | RunnableLambda(format_docs), "question": RunnablePassthrough()}
            | prompt
            | model
            | StrOutputParser()
        )
        print("\n== LCEL chain: retriever | format_docs | prompt | model | parser")
        docs = await retriever.ainvoke("where is the auth token refreshed?")
        print("   retriever found:", docs[0].metadata["path"], docs[0].metadata["symbol"])
        print("   answer:", await chain.ainvoke("where is the auth token refreshed?"))
    finally:
        await index.close()


asyncio.run(main(sys.argv[1], sys.argv[2]))
