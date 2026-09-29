"""Adapt the supplied Agent Memory rc2 image's embedding calls to NVIDIA.

Run during a derived image build; --root supports an isolated offline check.
Fail if the expected source differs. Existing installed images are not changed.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path


HELPER = '''"""Embedding request options for StreamAI's selected NVIDIA model."""

def embedding_request_options(model: str, *, input_type: str) -> dict:
    if model != "nvidia/llama-3.2-nv-embedqa-1b-v2":
        return {}
    if input_type not in {"query", "passage"}:
        raise ValueError("input_type must be query or passage")
    return {"encoding_format": "float", "extra_body": {"input_type": input_type}}


def llm_health_request_options(model: str) -> dict:
    # Capella's deployed Mistral accepts max_tokens. Its health response ignored
    # max_completion_tokens and generated 105 tokens for each recurring ping.
    if model == "mistralai/mistral-7b-instruct-v0.3":
        return {"max_tokens": 1, "temperature": 0}
    return {"max_completion_tokens": 16}
'''


def replace_once(text: str, old: str, new: str, path: Path) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"Unexpected source in {path}: expected one match for {old!r}")
    return text.replace(old, new, 1)


def prepare(lib: Path) -> dict[Path, str]:
    helper = lib / "core/utils/embedding_request.py"
    if helper.exists():
        raise RuntimeError(f"Compatibility helper already present: {helper}. Build from the original rc2 image.")

    clients_path = lib / "core/utils/model_clients.py"
    db_path = lib / "core/db.py"
    memory_path = lib / "core/memory.py"
    clients = clients_path.read_text()
    db = db_path.read_text()
    memory = memory_path.read_text()
    helper_import = "from libs.core.utils.embedding_request import embedding_request_options, llm_health_request_options\n"

    clients = replace_once(clients, "import openai\n", "import openai\n" + helper_import, clients_path)
    clients = replace_once(
        clients,
        "    texts: list[str], conn: Optional[AgentMemoryConfig] = None\n",
        '    texts: list[str], conn: Optional[AgentMemoryConfig] = None, *, input_type: str = "passage"\n',
        clients_path,
    )
    clients = replace_once(
        clients,
        "_embed_batch_chunk(client, model_name, chunk)",
        "_embed_batch_chunk(client, model_name, chunk, input_type=input_type)",
        clients_path,
    )
    clients = replace_once(
        clients,
        "def _embed_batch_chunk(client, model_name: str, texts: list[str]) -> list[list[float]]:",
        'def _embed_batch_chunk(client, model_name: str, texts: list[str], *, input_type: str = "passage") -> list[list[float]]:',
        clients_path,
    )
    clients = replace_once(
        clients,
        "    response = client.embeddings.create(model=model_name, input=texts)",
        "    response = client.embeddings.create(\n"
        "        model=model_name, input=texts,\n"
        "        **embedding_request_options(model_name, input_type=input_type),\n"
        "    )",
        clients_path,
    )
    memory = replace_once(
        memory,
        "query_embedding = embed_texts_batch([query], conn)[0]",
        'query_embedding = embed_texts_batch([query], conn, input_type="query")[0]',
        memory_path,
    )
    db = replace_once(
        db,
        "                model=self.embedding_model, input=test_input\n",
        "                model=self.embedding_model, input=test_input,\n"
        '                **embedding_request_options(self.embedding_model, input_type="passage"),\n',
        db_path,
    )
    db = replace_once(
        db,
        '            model=model,\n            input="ping",\n',
        '            model=model,\n            input="ping",\n'
        '            **embedding_request_options(model, input_type="query"),\n',
        db_path,
    )
    db = replace_once(
        db,
        "            max_completion_tokens=16,  # reasoning models reject the legacy max_tokens\n",
        "            **llm_health_request_options(model),\n",
        db_path,
    )
    # Insert after future imports, preserving module docstrings and Python syntax.
    tree = ast.parse(db)
    insert_after = 0
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            if insert_after == 0:
                insert_after = node.end_lineno
                continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            insert_after = node.end_lineno
            continue
        break
    lines = db.splitlines(keepends=True)
    lines.insert(insert_after, helper_import)
    db = "".join(lines)

    # Correlate synchronous comparison work and asynchronous retries by block
    # annotations. ContextVars keep concurrent health/background work separate.
    trial_import = "from libs.core.utils.trial_context import metered_memory, trial_headers\n"
    clients = clients.replace(helper_import, helper_import + trial_import)
    for name in ("summarize_memory_blocks", "summarize_memory_blocks_sync"):
        prefix = "async def " if name == "summarize_memory_blocks" else "def "
        clients = replace_once(clients, prefix + name + "(", "@metered_memory\n" + prefix + name + "(", clients_path)
    clients = replace_once(clients, "        model=model_name, input=texts,", "        model=model_name, input=texts, extra_headers=trial_headers(),", clients_path)
    if clients.count("        temperature=0.2,") != 2:
        raise RuntimeError("Unexpected summary call sites")
    clients = clients.replace("        temperature=0.2,", "        temperature=0.2,\n        extra_headers=trial_headers(),")
    memory = replace_once(memory, "    def __process_memory_blocks(", "    @metered_memory\n    def __process_memory_blocks(", memory_path)
    # Import after the future statement without changing its required position.
    memory_lines = memory.splitlines(keepends=True)
    future = [n.end_lineno for n in ast.parse(memory).body if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    memory_lines.insert(max(future, default=0), trial_import)
    memory = "".join(memory_lines)
    trial = Path(__file__).with_name("trial_context.py").read_text()
    return {clients_path: clients, db_path: db, memory_path: memory, helper: HELPER,
            lib / "core/utils/trial_context.py": trial}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("/"))
    args = parser.parse_args()
    roots = [args.root / "app/libs"]
    roots.extend(sorted((args.root / "opt/venv/lib").glob("python*/site-packages/libs")))
    pending: dict[Path, str] = {}
    for lib in roots:
        if not (lib / "core/utils/model_clients.py").is_file():
            if lib == roots[0]:
                raise RuntimeError(f"Agent Memory sources are missing from {lib}")
            continue
        pending.update(prepare(lib))
    # Validate every copy before changing any file.
    for path, text in pending.items():
        compile(text, str(path), "exec")
    for path, text in pending.items():
        path.write_text(text)
    print(f"NVIDIA compatibility and usage correlation applied to {len(pending) // 5} Agent Memory source installation(s).")


if __name__ == "__main__":
    main()
