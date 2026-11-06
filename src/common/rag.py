# -*- coding: utf-8 -*-
"""
Lightweight, zero-external-dependency Workspace RAG indexer and retriever for llmCode.
Scans project source files, splits them into semantic line-bounded chunks, builds a
BM25/TF-IDF lexical index stored in `config/rag_index.json`, and generates a compact
`config/rag_context.md` summary for OpenCode, Claude Code, Codex, and API retrieval queries.
"""
import json
import math
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.common.paths import CONFIG_DIR

RAG_INDEX_FILE = CONFIG_DIR / 'rag_index.json'
RAG_CONTEXT_FILE = CONFIG_DIR / 'rag_context.md'

INDEXABLE_EXTENSIONS = {
    '.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.c', '.cpp', '.cc',
    '.h', '.hpp', '.java', '.cs', '.rb', '.php', '.swift', '.kt', '.scala',
    '.md', '.txt', '.json', '.yaml', '.yml', '.toml', '.ini', '.sh', '.bat',
    '.ps1', '.sql', '.html', '.css',
}

IGNORED_DIRS = {
    '.git', '.hg', '.svn', '__pycache__', '.venv', 'venv', 'env',
    'node_modules', 'dist', 'build', 'build-cuda', 'build-metal',
    'models', 'logs', 'ollama_dist', '.idea', '.vscode', '.next', 'target',
}

_TOKEN_RE = re.compile(r'[a-zA-Z_][a-zA-Z0-9_]{1,40}')


def _tokenize(text: str) -> List[str]:
    """Extracts normalized identifier and keyword tokens from code or prose."""
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def index_workspace(project_dir: Path, chunk_lines: int = 70, overlap_lines: int = 10, max_files: int = 400) -> Dict[str, Any]:
    """
    Scans `project_dir`, splits source & doc files into overlapping chunks, computes
    document frequencies, Saves index to `config/rag_index.json`, and writes an initial
    high-signal workspace context file to `config/rag_context.md`.
    """
    project_dir = project_dir.expanduser().resolve()
    if not project_dir.is_dir():
        raise ValueError(f"Workspace directory not found: {project_dir}")

    chunks: List[Dict[str, Any]] = []
    file_list: List[str] = []
    df_counter: Counter = Counter()

    for root, dirs, files in os.walk(project_dir):
        # Prune ignored directories in-place
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS and not d.startswith('.')]
        for fname in sorted(files):
            if len(file_list) >= max_files:
                break
            fpath = Path(root) / fname
            if fpath.suffix.lower() not in INDEXABLE_EXTENSIONS:
                continue
            try:
                st = fpath.stat()
                # Skip files larger than 512 KB or empty
                if st.st_size == 0 or st.st_size > 512 * 1024:
                    continue
                content = fpath.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue

            rel_path = fpath.relative_to(project_dir).as_posix()
            file_list.append(rel_path)
            lines = content.splitlines()
            step = max(10, chunk_lines - overlap_lines)

            for start_idx in range(0, len(lines), step):
                slice_lines = lines[start_idx:start_idx + chunk_lines]
                if not slice_lines:
                    break
                chunk_text = '\n'.join(slice_lines)
                tokens = _tokenize(f"{rel_path} {chunk_text}")
                if not tokens:
                    continue
                tf = Counter(tokens)
                for tok in tf.keys():
                    df_counter[tok] += 1
                chunks.append({
                    'file': rel_path,
                    'start_line': start_idx + 1,
                    'end_line': start_idx + len(slice_lines),
                    'text': chunk_text,
                    'tf': dict(tf.most_common(60)),
                    'len': len(tokens),
                })
        if len(file_list) >= max_files:
            break

    index_data = {
        'project_dir': str(project_dir),
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'file_count': len(file_list),
        'chunk_count': len(chunks),
        'files': file_list,
        'df': dict(df_counter),
        'chunks': chunks,
    }

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RAG_INDEX_FILE.write_text(json.dumps(index_data, indent=2) + '\n', encoding='utf-8')

    # Generate default workspace RAG context markdown
    generate_rag_context_file(project_dir=project_dir, query='', top_k=6, index_data=index_data)

    return {
        'ok': True,
        'project_dir': str(project_dir),
        'file_count': len(file_list),
        'chunk_count': len(chunks),
        'index_file': str(RAG_INDEX_FILE),
        'context_file': str(RAG_CONTEXT_FILE),
    }


def retrieve_chunks(query: str, top_k: int = 6, project_dir: Optional[Path] = None, index_data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Scores indexed workspace chunks against `query` using BM25 weighting and returns
    the top_k highest-scoring chunks.
    """
    if index_data is None:
        if not RAG_INDEX_FILE.is_file():
            if project_dir and project_dir.is_dir():
                index_workspace(project_dir)
            else:
                return []
        try:
            index_data = json.loads(RAG_INDEX_FILE.read_text(encoding='utf-8'))
        except Exception:
            return []

    chunks = index_data.get('chunks', [])
    if not chunks:
        return []

    if not query.strip():
        # Return entry-point / README / core chunks when no specific query is provided
        priority = []
        for c in chunks:
            fn = c['file'].lower()
            score = 0
            if 'readme' in fn:
                score += 10
            elif fn.endswith(('app.py', 'main.py', 'llmcode.py', 'index.ts', 'index.js', 'paths.py')):
                score += 8
            elif '/' not in fn:
                score += 4
            if c['start_line'] == 1:
                score += 3
            priority.append((score, c))
        priority.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in priority[:top_k]]

    q_tokens = _tokenize(query)
    if not q_tokens:
        return chunks[:top_k]

    n_docs = max(1, len(chunks))
    avg_dl = sum(c.get('len', 1) for c in chunks) / n_docs
    df = index_data.get('df', {})
    k1 = 1.5
    b = 0.75

    scored = []
    for c in chunks:
        tf_map = c.get('tf', {})
        dl = c.get('len', 1)
        score = 0.0
        for qt in q_tokens:
            f = tf_map.get(qt, 0)
            if qt in c['file'].lower():
                f += 3
            if f == 0:
                continue
            n_q = df.get(qt, 0)
            idf = math.log(1.0 + (n_docs - n_q + 0.5) / (n_q + 0.5))
            denom = f + k1 * (1.0 - b + b * (dl / max(1.0, avg_dl)))
            score += idf * ((f * (k1 + 1.0)) / denom)
        if score > 0:
            scored.append((score, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            'file': c['file'],
            'start_line': c['start_line'],
            'end_line': c['end_line'],
            'score': round(sc, 3),
            'text': c['text'],
        }
        for sc, c in scored[:top_k]
    ]


def generate_rag_context_file(project_dir: Path, query: str = '', top_k: int = 6, index_data: Optional[Dict[str, Any]] = None) -> Path:
    """
    Writes `config/rag_context.md` containing the repository file tree summary and
    top retrieved code chunks so coding agents (OpenCode / Claude / Codex) get high-signal context
    without overflowing the context window.
    """
    if index_data is None:
        if RAG_INDEX_FILE.is_file():
            try:
                loaded = json.loads(RAG_INDEX_FILE.read_text(encoding='utf-8'))
                if loaded.get('project_dir') == str(project_dir.resolve()):
                    index_data = loaded
            except Exception:
                pass
        if index_data is None:
            index_workspace(project_dir)
            index_data = json.loads(RAG_INDEX_FILE.read_text(encoding='utf-8'))

    top_chunks = retrieve_chunks(query=query, top_k=top_k, project_dir=project_dir, index_data=index_data)
    files = index_data.get('files', [])

    lines = [
        "# llmCode Workspace RAG Context",
        f"- **Workspace**: `{index_data.get('project_dir', project_dir)}`",
        f"- **Indexed Files**: {len(files)} (`{index_data.get('chunk_count', 0)}` chunks)",
    ]
    if query:
        lines.append(f"- **Active Retrieval Query**: `{query}`")

    lines.append("\n## Workspace File Map")
    for f in files[:80]:
        lines.append(f"- `{f}`")
    if len(files) > 80:
        lines.append(f"- *(...and {len(files) - 80} more files)*")

    lines.append("\n## Retrieved Relevant Code Chunks")
    for ch in top_chunks:
        lines.append(f"\n### `{ch['file']}` (Lines {ch['start_line']}-{ch['end_line']})")
        lines.append("```")
        lines.append(ch['text'])
        lines.append("```")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RAG_CONTEXT_FILE.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return RAG_CONTEXT_FILE
