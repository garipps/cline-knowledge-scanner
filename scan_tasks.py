#!/usr/bin/env python3
"""
Knowledge Scanner v2 - scans Cline conversations and creates wiki articles.
- Classifies articles into concepts/entities/tasks
- Generates slug-based filenames: YYYY-MM-DD_slug.md
- Adds YAML frontmatter with tags, status, related wikilinks
- Saves raw cleaned conversation for reprocessing
- Auto-generates system/_index.md and system/_tags.md
"""

import json
import os
import re
import sys
import time
import glob
import signal
import logging
import requests
from datetime import datetime
from pathlib import Path

# --- Configuration ---
TASKS_DIR = os.path.expandvars(
    r'%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\tasks'
)
WIKI_ROOT = os.path.join(os.path.expanduser('~'), 'Documents', 'knowledge-wiki')
PROCESSED_MARKER = '.done'
MODEL = 'qwen/qwen-2.5-7b-instruct'
API_URL = 'https://openrouter.ai/api/v1/chat/completions'
INTERVAL = 180  # seconds between scans
MAX_INPUT_TOKENS = 4000  # limit for LLM input (~16K chars)
MAX_MESSAGES = 30  # max messages from conversation
API_KEY = os.environ.get('OPENROUTER_API_KEY', '')

# --- Subdirectory names ---
DIR_CONCEPTS = 'concepts'
DIR_ENTITIES = 'entities'
DIR_TASKS = 'tasks'
DIR_SYSTEM = 'system'
DIR_RAW = 'raw'

TYPE_DIRS = {
    'concept': DIR_CONCEPTS,
    'entity': DIR_ENTITIES,
    'task': DIR_TASKS,
}

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(
            open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)
        ),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scanner.log'),
            encoding='utf-8'
        )
    ]
)
log = logging.getLogger('scanner')

if not API_KEY:
    log.error("OPENROUTER_API_KEY not set. Run: set OPENROUTER_API_KEY=sk-or-v1-...")
    sys.exit(1)

# --- Noise patterns (regex) ---
NOISE_PATTERNS = [
    # environment_details block (with any attributes)
    (re.compile(r'<environment_details[^>]*>.*?</environment_details>', re.DOTALL), ''),
    # hook_context block (with any attributes)
    (re.compile(r'<hook_context[^>]*>.*?</hook_context>', re.DOTALL), ''),
    # <thinking> blocks
    (re.compile(r'<thinking>.*?</thinking>', re.DOTALL), ''),
    # task_progress blocks (all variants)
    (re.compile(r'# task_progress(?:\s+List|\s+RECOMMENDED)?\s*(?:\(Optional.*?)?$', re.DOTALL), ''),
    (re.compile(r'# task_progress List.*?(?=\n\n[A-Z*#<]|\Z)', re.DOTALL), ''),
    (re.compile(r'# task_progress RECOMMENDED.*', re.DOTALL), ''),
    # "When starting a new task..." reminder
    (re.compile(r'When starting a new task.*?(?=\n\n[A-Z*#<]|\Z)', re.DOTALL), ''),
    # "Reminder on how to use the task_progress parameter"
    (re.compile(r'Reminder on how to use the task_progress parameter:.*?(?=\n\n[^\n\t]|\Z)', re.DOTALL), ''),
    # "Keeping the task_progress list updated..."
    (re.compile(r'Keeping the task_progress list updated.*', re.DOTALL), ''),
    # **Remember:** and **Note:** reminders
    (re.compile(r'\*\*Remember:\*\*.*?missed\.', re.DOTALL), ''),
    (re.compile(r'\*\*Note:\*\*.*?(?=\n\n[A-Z*#<\[]|\Z)', re.DOTALL), ''),
    (re.compile(r'\*\*Benefits of creating.*?(?=\n\n[A-Z*#<\[]|\Z)', re.DOTALL), ''),
    (re.compile(r'\*\*Example structure:\*\*.*?```\s*$', re.DOTALL), ''),
    # # TODO LIST UPDATE REQUIRED
    (re.compile(r'# TODO LIST UPDATE REQUIRED.*?(?=\n\n[A-Z*#]|\Z)', re.DOTALL), ''),
    # [ERROR] system messages
    (re.compile(r'\[ERROR\].*?(?=\n\n[^\n#]|\Z)', re.DOTALL), ''),
    # # Reminder: Instructions for Tool Use
    (re.compile(r'# Reminder: Instructions for Tool Use.*?(?=\n#[^#]|\Z)', re.DOTALL), ''),
    # # Next Steps
    (re.compile(r'# Next Steps.*?(?=\n#[^#]|\Z)', re.DOTALL), ''),
    # [TASK RESUMPTION] block
    (re.compile(r'\[TASK RESUMPTION\].*?(?=\n\n[A-Z*#\[]|\Z)', re.DOTALL), ''),
    # PLAN MODE reminders
    (re.compile(r'While in PLAN MODE.*?(?=\n\n[A-Z*#\[]|\Z)', re.DOTALL), ''),
    (re.compile(r'New message to respond to with plan_mode_respond tool.*?(?=\n\n[A-Z*#\[]|\Z)', re.DOTALL), ''),
    (re.compile(r'Note: If you previously attempted a tool use.*?(?=\n\n[A-Z*#\[]|\Z)', re.DOTALL), ''),
    # Tool XML calls (all known tags) - remove entirely
    (re.compile(r'<(list_files|read_file|write_to_file|replace_in_file|search_files|execute_command|attempt_completion|ask_followup_question|use_mcp_tool|access_mcp_resource|plan_mode_respond|new_task|load_mcp_documentation)\b[^>]*>.*?</\1>', re.DOTALL), ''),
    # Tool invocation results: [read_file for 'path'] Result: ...
    (re.compile(r"\[.*?for '.*?'\] Result:.*?(?=\n\n[^\n]|\Z)", re.DOTALL), ''),
    # [attempt_completion] Result: ...
    (re.compile(r'\[attempt_completion\]\s*Result:.*', re.DOTALL), ''),
    # The user has provided feedback
    (re.compile(r'The user has provided feedback on the results\..*?(?=\n\n[A-Z*#\[]|\Z)', re.DOTALL), ''),
    # modelInfo and metrics
    (re.compile(r'"modelInfo"\s*:\s*\{[^}]*\}'), ''),
    (re.compile(r'"metrics"\s*:\s*\{[^}]*\}'), ''),
    # Self-reading detection
    (re.compile(r'\[read_file for .*api_conversation_history\.json.*?\]'), ''),
    # System Information block
    (re.compile(r'## SYSTEM INFORMATION.*?(?=\n##|\Z)', re.DOTALL), ''),
    # === MSG dump blocks
    (re.compile(r'=== MSG \d+.*?===\n', re.DOTALL), ''),
    # (File has N lines total.)
    (re.compile(r'\(File has \d+ lines? total\.\)\s*', re.DOTALL), ''),
    # <feedback> tags (keep content, remove tags)
    (re.compile(r'</?feedback>'), ''),
    # Empty lines (3+ -> 2)
    (re.compile(r'\n{3,}'), '\n\n'),
]

JSON_BLOCK_PATTERN = re.compile(r'```json\s*\n([\s\S]*?)```', re.DOTALL)
LARGE_JSON_THRESHOLD = 500


def is_garbage_message(text: str) -> bool:
    """Detects if a message is purely tool output / garbage (not meaningful content)."""
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) < 15:
        return True

    # List of numbers (task IDs) - one per line
    lines = [l.strip() for l in stripped.split('\n') if l.strip()]
    if len(lines) > 3:
        numeric_lines = sum(1 for l in lines if re.match(r'^\d{10,}$', l))
        if numeric_lines / len(lines) > 0.7:
            return True

    # Single filename
    if re.match(r'^[\w.\-]+\.(json|py|md|txt|yaml|yml|toml|js|ts)\s*$', stripped):
        return True

    # Only "Done" or similar
    if stripped.lower() in ('done', 'ok', 'yes', 'no', 'success', 'error', 'result'):
        return True

    return False


def has_broken_encoding(text: str) -> bool:
    """Detects broken encoding (cp1251 mojibake)."""
    weird = re.findall(r'[\u0400-\u04FF]{3,}', text)
    if len(weird) > 3:
        if '\ufffd' in text or '?' in text:
            return True
    return False


def clean_noise(text: str) -> str:
    """Removes all noise from message text."""
    for pattern, replacement in NOISE_PATTERNS:
        text = pattern.sub(replacement, text)

    # Truncate large JSON blocks in markdown
    def truncate_json(match):
        content = match.group(1)
        if len(content) > LARGE_JSON_THRESHOLD:
            return '```json\n' + content[:200] + '...[TRUNCATED]\n```'
        return match.group(0)

    text = JSON_BLOCK_PATTERN.sub(truncate_json, text)

    # Clean up leftover artifacts
    text = re.sub(r'\[TOOL_CALL\]\s*', '', text)
    text = re.sub(r'\[TOOL_RESULT\]\s*', '', text)
    text = re.sub(r'\[SELF_READ_SKIPPED\]\s*', '', text)

    # Remove lines with broken encoding
    if has_broken_encoding(text):
        clean_lines = []
        for line in text.split('\n'):
            weird_count = len(re.findall(r'[\u0400-\u04FF]{2,}', line))
            if weird_count > 2 and ('?' in line or '\ufffd' in line):
                continue
            clean_lines.append(line)
        text = '\n'.join(clean_lines)

    return text.strip()


def extract_conversation(filepath: str) -> str:
    """Parses api_conversation_history.json and returns cleaned dialogue text."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        log.error(f"Read error {filepath}: {e}")
        return ""

    if not isinstance(data, list):
        log.warning(f"Unexpected format: {filepath}")
        return ""

    clean_parts = []
    msg_count = 0

    for msg in data:
        role = msg.get('role', 'unknown')

        # Skip system messages
        if role == 'system':
            continue

        content = msg.get('content', [])
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get('type') != 'text':
                continue

            raw_text = block.get('text', '')
            if not raw_text:
                continue

            # Clean noise
            cleaned = clean_noise(raw_text)

            # Skip garbage messages (tool output, short stuff)
            if is_garbage_message(cleaned):
                continue

            # Skip completely broken encoding messages
            if has_broken_encoding(cleaned) and len(cleaned) < 100:
                continue

            # Format role
            role_label = 'USER' if role == 'user' else 'AI'
            clean_parts.append(f"**{role_label}**: {cleaned}")
            msg_count += 1

            if msg_count >= MAX_MESSAGES:
                clean_parts.append("\n[...truncated, max messages reached...]")
                break

    return '\n\n'.join(clean_parts)


def truncate_for_llm(text: str, max_chars: int = MAX_INPUT_TOKENS * 4) -> str:
    """Truncates text to char limit for LLM."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n\n[...MIDDLE TRUNCATED...]\n\n" + text[-half:]


def call_summarizer(text: str) -> dict:
    """Sends text to Qwen 2.5 7B for summarization. Returns parsed JSON dict."""
    prompt = """You are a wiki article assistant. Analyze the conversation between USER and AI assistant and create a structured wiki article.

IMPORTANT: Return ONLY valid JSON, no markdown, no code fences. The JSON must have this exact structure:

{
  "title": "Short Title in Title Case (3-6 words)",
  "slug": "short-title-in-lowercase-with-hyphens",
  "type": "task",
  "status": "completed",
  "tags": ["tag1", "tag2", "tag3"],
  "related": ["related-entity-1", "related-entity-2"],
  "summary": "1-2 sentence summary of what happened",
  "key_facts": ["Fact 1", "Fact 2", "Fact 3"],
  "results": ["Result 1", "Result 2"],
  "notes": ["Note 1", "Note 2"],
  "timeline": [{"time": "YYYY-MM-DD HH:MM or unknown", "event": "What happened"}]
}

Classification rules for "type":
- "concept": Methodologies, frameworks, architecture decisions, comparisons, ideas, patterns
- "entity": Specific tools, products, platforms, APIs, libraries, services
- "task": Concrete tasks, bug fixes, scripts created, deployments, setups

Rules:
- "slug": lowercase, 3-6 words separated by hyphens, derived from title
- "tags": lowercase, 3-8 relevant tags (technology names, concepts, tools)
- "related": lowercase hyphenated names of entities/concepts mentioned (for [[wikilinks]])
- "status": "completed", "in-progress", or "unclear"
- "timeline": 2-5 key events with timestamps if available, or "unknown"
- Write all text content in Russian
- Keep title in English (Title Case) for file naming, but summary/facts/notes in Russian

Conversation:
""" + text

    headers = {
        'Authorization': f'Bearer {API_KEY}',
        'Content-Type': 'application/json',
        'HTTP-Referer': 'https://github.com/knowledge-scanner',
        'X-Title': 'Knowledge Scanner v2'
    }

    payload = {
        'model': MODEL,
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 1200,
        'temperature': 0.3,
        'response_format': {'type': 'json_object'},
    }

    try:
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=90)
        resp.raise_for_status()
        result = resp.json()

        if 'choices' in result and len(result['choices']) > 0:
            content = result['choices'][0]['message']['content']
            # Try to parse JSON from response
            # Remove code fences if LLM added them despite instructions
            content = re.sub(r'^```json\s*\n?', '', content)
            content = re.sub(r'\n?```\s*$', '', content)
            content = content.strip()

            try:
                # Fix Windows path escapes that break JSON
                fixed_content = content.replace('\\', '\\\\')
                parsed = json.loads(fixed_content)
                # Validate required fields
                required = ['title', 'slug', 'type', 'status', 'tags', 'summary']
                for field in required:
                    if field not in parsed:
                        parsed[field] = 'unknown' if field != 'tags' else []
                if 'type' not in parsed or parsed['type'] not in TYPE_DIRS:
                    parsed['type'] = 'task'
                if 'related' not in parsed:
                    parsed['related'] = []
                if 'key_facts' not in parsed:
                    parsed['key_facts'] = []
                if 'results' not in parsed:
                    parsed['results'] = []
                if 'notes' not in parsed:
                    parsed['notes'] = []
                if 'timeline' not in parsed:
                    parsed['timeline'] = []
                return parsed
            except json.JSONDecodeError as e:
                log.error(f"JSON parse error: {e}, content: {content[:300]}")
                return _fallback_article(content)
        elif 'error' in result:
            log.error(f"API error: {result['error']}")
            return _error_article(str(result['error']))
        else:
            log.error(f"Unexpected response: {json.dumps(result, ensure_ascii=False)[:500]}")
            return _error_article("Unexpected API response")

    except requests.exceptions.Timeout:
        log.error("OpenRouter request timeout")
        return _error_article("request timeout")
    except requests.exceptions.RequestException as e:
        log.error(f"Network error: {e}")
        return _error_article(str(e))
    except Exception as e:
        log.error(f"Unexpected error: {e}")
        return _error_article(str(e))


def _fallback_article(raw_text: str) -> dict:
    """Create a minimal article from raw LLM text when JSON parsing fails."""
    # Try to extract a title from first line
    first_line = raw_text.split('\n')[0].strip('# ').strip()
    title = first_line[:60] if first_line else "Untitled Article"
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40]
    return {
        'title': title,
        'slug': slug or 'untitled',
        'type': 'task',
        'status': 'unclear',
        'tags': ['uncategorized'],
        'related': [],
        'summary': raw_text[:200],
        'key_facts': [],
        'results': [],
        'notes': ['JSON parsing failed, raw text saved'],
        'timeline': [],
    }


def _error_article(error_msg: str) -> dict:
    """Create a minimal article for error cases."""
    return {
        'title': 'Processing Error',
        'slug': 'processing-error',
        'type': 'task',
        'status': 'unclear',
        'tags': ['error'],
        'related': [],
        'summary': f'Error during LLM summarization: {error_msg[:200]}',
        'key_facts': [],
        'results': [],
        'notes': [f'Error: {error_msg[:200]}'],
        'timeline': [],
    }


def get_task_timestamp(task_dir: str) -> str:
    """Gets task date from folder timestamp."""
    task_id = os.path.basename(task_dir)
    try:
        ts = int(task_id) / 1000  # Cline uses milliseconds
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')
    except (ValueError, OSError):
        return "unknown"


def get_task_date_iso(task_dir: str) -> str:
    """Gets task date in ISO format (YYYY-MM-DD) for filename."""
    task_id = os.path.basename(task_dir)
    try:
        ts = int(task_id) / 1000
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d')
    except (ValueError, OSError):
        return datetime.now().strftime('%Y-%m-%d')


def build_article_markdown(article: dict, task_id: str, task_date: str,
                           created_date: str, conv_len: int) -> str:
    """Build the final wiki article markdown with frontmatter and wikilinks."""

    # Build wikilinks from related
    related_links = ', '.join(f'[[{r}]]' for r in article.get('related', []))
    tags_yaml = json.dumps(article.get('tags', []), ensure_ascii=False)
    tags_yaml = tags_yaml.replace('[', '[').replace(']', ']')

    lines = []

    # Frontmatter
    lines.append('---')
    lines.append(f'title: "{article["title"]}"')
    lines.append(f'type: {article["type"]}')
    lines.append(f'status: {article["status"]}')
    lines.append(f'created: {created_date}')
    lines.append(f'updated: {datetime.now().strftime("%Y-%m-%d")}')
    lines.append(f'tags: {tags_yaml}')
    lines.append(f'task_id: "{task_id}"')
    lines.append(f'source: cline-task')
    if related_links:
        lines.append(f'related: {related_links}')
    lines.append('---')
    lines.append('')

    # Title
    lines.append(f'# {article["title"]}')
    lines.append('')

    # Summary
    lines.append('## Summary')
    lines.append(article.get('summary', ''))
    lines.append('')

    # Key Facts
    key_facts = article.get('key_facts', [])
    if key_facts:
        lines.append('## Key Facts')
        for fact in key_facts:
            # Convert entity mentions to wikilinks
            fact = _add_wikilinks(fact, article.get('related', []))
            lines.append(f'- {fact}')
        lines.append('')

    # Results
    results = article.get('results', [])
    if results:
        lines.append('## Results')
        for r in results:
            status_char = '[x]' if article.get('status') == 'completed' else '[ ]'
            r = _add_wikilinks(r, article.get('related', []))
            lines.append(f'- {status_char} {r}')
        lines.append('')

    # Notes
    notes = article.get('notes', [])
    if notes:
        lines.append('## Notes')
        for note in notes:
            note = _add_wikilinks(note, article.get('related', []))
            lines.append(f'- {note}')
        lines.append('')

    # Timeline
    timeline = article.get('timeline', [])
    if timeline:
        lines.append('## Timeline')
        for entry in timeline:
            t = entry.get('time', 'unknown')
            event = entry.get('event', '')
            lines.append(f'- **{t}** | {event}')
        lines.append('')

    # See Also
    related = article.get('related', [])
    if related:
        lines.append('## See Also')
        for r in related:
            lines.append(f'- [[{r}]]')
        lines.append('')

    return '\n'.join(lines)


def _add_wikilinks(text: str, related: list) -> str:
    """Add [[wikilinks]] around mentions of related entities in text."""
    if not related:
        return text
    # Sort by length (longest first) to avoid partial replacements
    for entity in sorted(related, key=len, reverse=True):
        # Case-insensitive replacement, but only for standalone words
        # Don't replace if already inside [[]]
        pattern = re.compile(
            r'(?<!\[\[)(' + re.escape(entity) + r')(?!\]\])',
            re.IGNORECASE
        )
        # Only replace the first occurrence to avoid over-linking
        text = pattern.sub(f'[[{entity}]]', text, count=1)
    return text


def save_raw_conversation(task_id: str, created_date: str, conv_text: str) -> str:
    """Save raw cleaned conversation to raw/ directory."""
    raw_dir = os.path.join(WIKI_ROOT, DIR_RAW)
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f'{created_date}_{task_id}_raw.md')

    with open(raw_path, 'w', encoding='utf-8') as f:
        f.write(f'# Raw conversation: task {task_id}\n\n')
        f.write(f'**Task ID**: {task_id}\n')
        f.write(f'**Date**: {created_date}\n')
        f.write(f'**Saved**: {datetime.now().strftime("%Y-%m-%d %H:%M")}\n')
        f.write(f'**Length**: {len(conv_text)} chars\n\n---\n\n')
        f.write(conv_text)

    return raw_path


def update_index():
    """Regenerate system/_index.md from all existing articles."""
    index_path = os.path.join(WIKI_ROOT, DIR_SYSTEM, '_index.md')
    os.makedirs(os.path.join(WIKI_ROOT, DIR_SYSTEM), exist_ok=True)

    articles = _scan_all_articles()

    # Group by type
    by_type = {'concept': [], 'entity': [], 'task': []}
    for a in articles:
        article_type = a.get('type', 'task')
        if article_type not in by_type:
            article_type = 'task'
        by_type[article_type].append(a)

    # Sort each group by date
    for t in by_type:
        by_type[t].sort(key=lambda x: x.get('created', ''), reverse=True)

    lines = []
    lines.append('# Knowledge Wiki Index')
    lines.append('')
    lines.append(f'> Auto-generated by Knowledge Scanner v2 on {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append('')

    # Stats
    total = sum(len(v) for v in by_type.values())
    lines.append('## Stats')
    lines.append(f'- Total articles: {total}')
    lines.append(f'- Concepts: {len(by_type["concept"])} | Entities: {len(by_type["entity"])} | Tasks: {len(by_type["task"])}')
    lines.append(f'- Last updated: {datetime.now().strftime("%Y-%m-%d")}')
    lines.append('')

    # Concepts
    if by_type['concept']:
        lines.append('## Concepts')
        for a in by_type['concept']:
            lines.append(f'- [[{a["filename"]}]] -- {a["title"]}')
        lines.append('')

    # Entities
    if by_type['entity']:
        lines.append('## Entities')
        for a in by_type['entity']:
            lines.append(f'- [[{a["filename"]}]] -- {a["title"]}')
        lines.append('')

    # Tasks
    if by_type['task']:
        lines.append('## Tasks')
        for a in by_type['task']:
            lines.append(f'- [[{a["filename"]}]] -- {a["title"]}')
        lines.append('')

    # Recent changes
    all_sorted = sorted(articles, key=lambda x: x.get('created', ''), reverse=True)
    if all_sorted:
        lines.append('## Recent Changes')
        for a in all_sorted[:10]:
            lines.append(f'- {a.get("created", "?")} | [[{a["filename"]}]] -- {a["title"]}')
        lines.append('')

    with open(index_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    log.info(f"Index updated: {index_path} ({total} articles)")


def update_tags():
    """Regenerate system/_tags.md from all existing articles."""
    tags_path = os.path.join(WIKI_ROOT, DIR_SYSTEM, '_tags.md')
    os.makedirs(os.path.join(WIKI_ROOT, DIR_SYSTEM), exist_ok=True)

    articles = _scan_all_articles()

    # Collect tag -> articles mapping
    tag_map = {}
    for a in articles:
        for tag in a.get('tags', []):
            tag_lower = tag.lower()
            if tag_lower not in tag_map:
                tag_map[tag_lower] = []
            tag_map[tag_lower].append(a)

    # Sort tags
    sorted_tags = sorted(tag_map.keys())

    lines = []
    lines.append('# Tags Index')
    lines.append('')
    lines.append(f'> Auto-generated by Knowledge Scanner v2 on {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append(f'> Total tags: {len(sorted_tags)}')
    lines.append('')

    for tag in sorted_tags:
        articles_for_tag = tag_map[tag]
        lines.append(f'## {tag}')
        lines.append(f'({len(articles_for_tag)} articles)')
        lines.append('')
        for a in articles_for_tag:
            lines.append(f'- [[{a["filename"]}]] -- {a["title"]}')
        lines.append('')

    with open(tags_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    log.info(f"Tags updated: {tags_path} ({len(sorted_tags)} tags)")


def _scan_all_articles() -> list:
    """Scan all article files in wiki and extract metadata from frontmatter."""
    articles = []

    for type_name, dir_name in TYPE_DIRS.items():
        type_dir = os.path.join(WIKI_ROOT, dir_name)
        if not os.path.exists(type_dir):
            continue

        for md_file in glob.glob(os.path.join(type_dir, '*.md')):
            try:
                with open(md_file, 'r', encoding='utf-8') as f:
                    content = f.read()

                # Parse frontmatter
                meta = _parse_frontmatter(content)
                meta['filename'] = os.path.splitext(os.path.basename(md_file))[0]
                meta['type'] = type_name
                meta['filepath'] = md_file
                articles.append(meta)
            except Exception as e:
                log.warning(f"Error reading {md_file}: {e}")

    return articles


def _parse_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from markdown content."""
    meta = {'title': 'Untitled', 'tags': [], 'created': '', 'related': []}

    if not content.startswith('---'):
        return meta

    # Find end of frontmatter
    end_idx = content.find('---', 3)
    if end_idx == -1:
        return meta

    fm = content[3:end_idx].strip()

    # Parse simple YAML key-value pairs
    for line in fm.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue

        # Handle "key: value" format
        if ':' in line:
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip()

            if key == 'title':
                meta['title'] = value.strip('"').strip("'")
            elif key == 'created':
                meta['created'] = value
            elif key == 'tags':
                # Parse [tag1, tag2, ...]
                tags = re.findall(r'["\']?([^,\[\]"\']+)["\']?', value)
                meta['tags'] = [t.strip() for t in tags if t.strip()]
            elif key == 'related':
                # Parse [[link1]], [[link2]]
                links = re.findall(r'\[\[([^\]]+)\]\]', value)
                meta['related'] = links

    return meta


def ensure_dirs():
    """Create all wiki subdirectories."""
    for dir_name in [DIR_CONCEPTS, DIR_ENTITIES, DIR_TASKS, DIR_SYSTEM, DIR_RAW]:
        os.makedirs(os.path.join(WIKI_ROOT, dir_name), exist_ok=True)


def process_task(task_dir: str, dry_run: bool = False) -> bool:
    """Processes one task. Returns True on success."""
    json_file = os.path.join(task_dir, 'api_conversation_history.json')
    marker = os.path.join(task_dir, PROCESSED_MARKER)
    task_id = os.path.basename(task_dir)
    task_date = get_task_timestamp(task_dir)
    created_date = get_task_date_iso(task_dir)

    if not os.path.exists(json_file):
        log.debug(f"No JSON: {task_id}")
        return False

    if os.path.exists(marker):
        log.debug(f"Already processed: {task_id}")
        return False

    # Extract and clean conversation
    conv = extract_conversation(json_file)
    if not conv or len(conv) < 50:
        log.info(f"Too short/empty: {task_id} ({len(conv)} chars)")
        with open(marker, 'w', encoding='utf-8') as f:
            f.write(f'skipped:{datetime.now().isoformat()}')
        return False

    log.info(f"Processing: {task_id} ({len(conv)} chars, date: {task_date})")

    if dry_run:
        raw_dir = os.path.join(WIKI_ROOT, DIR_RAW)
        os.makedirs(raw_dir, exist_ok=True)
        out_path = os.path.join(raw_dir, f'{created_date}_{task_id}_clean.md')
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write(f"# Cleaned conversation {task_id}\n\n")
            f.write(f"**Date**: {task_date}\n\n---\n\n")
            f.write(conv)
        log.info(f"DRY RUN: saved cleaned text -> {out_path}")
        return True

    # Save raw cleaned conversation for reprocessing
    raw_path = save_raw_conversation(task_id, created_date, conv)
    log.info(f"Raw saved: {raw_path}")

    # Truncate for LLM
    truncated = truncate_for_llm(conv)

    # Send to LLM (returns structured dict)
    article = call_summarizer(truncated)

    # Determine output subdirectory
    article_type = article.get('type', 'task')
    if article_type not in TYPE_DIRS:
        article_type = 'task'
    type_dir = os.path.join(WIKI_ROOT, TYPE_DIRS[article_type])
    os.makedirs(type_dir, exist_ok=True)

    # Build filename: YYYY-MM-DD_slug.md
    slug = article.get('slug', 'untitled')
    # Clean slug
    slug = re.sub(r'[^a-z0-9-]', '-', slug.lower()).strip('-')
    slug = re.sub(r'-{2,}', '-', slug)
    if not slug:
        slug = 'untitled'
    filename = f'{created_date}_{slug}.md'
    out_path = os.path.join(type_dir, filename)

    # Handle duplicate filenames
    if os.path.exists(out_path):
        filename = f'{created_date}_{slug}_{task_id[:8]}.md'
        out_path = os.path.join(type_dir, filename)

    # Build article markdown
    article_md = build_article_markdown(article, task_id, task_date, created_date, len(conv))

    # Save
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(article_md)

    # Mark as processed
    with open(marker, 'w', encoding='utf-8') as f:
        f.write(f'processed:{datetime.now().isoformat()}:article={out_path}')

    log.info(f"OK saved: {out_path} (type={article_type}, slug={slug})")

    # Update indexes
    try:
        update_index()
        update_tags()
    except Exception as e:
        log.warning(f"Index update failed: {e}")

    return True


def scan_all(dry_run: bool = False, limit: int = 0):
    """Scans all task folders and processes new ones."""
    task_dirs = sorted(glob.glob(os.path.join(TASKS_DIR, '*')))
    processed = 0

    for task_dir in task_dirs:
        if not os.path.isdir(task_dir):
            continue

        try:
            if process_task(task_dir, dry_run=dry_run):
                processed += 1
                if limit > 0 and processed >= limit:
                    log.info(f"Limit reached: {limit} tasks")
                    break
        except Exception as e:
            log.error(f"Error processing {task_dir}: {e}")
            continue

    log.info(f"Scan complete. Processed: {processed}")

    # Always update indexes after scan
    if not dry_run:
        try:
            update_index()
            update_tags()
        except Exception as e:
            log.warning(f"Final index update failed: {e}")


def migrate_old_articles():
    """Migrate old flat-structure articles to new directory structure."""
    old_dir = WIKI_ROOT

    # Look for .md files directly in wiki root (old format: {task_id}.md)
    old_files = glob.glob(os.path.join(old_dir, '*.md'))

    if not old_files:
        log.info("No old articles to migrate")
        return

    ensure_dirs()
    migrated = 0

    for old_file in old_files:
        basename = os.path.basename(old_file)
        # Skip system files
        if basename.startswith('_'):
            continue

        # Skip if already in a subdirectory
        if os.path.dirname(old_file) != old_dir:
            continue

        # Parse old article
        try:
            with open(old_file, 'r', encoding='utf-8') as f:
                content = f.read()

            # Extract task_id from old format
            task_id_match = re.search(r'\*\*Task ID\*\*:\s*`(\d+)`', content)
            if not task_id_match:
                # Try from filename
                task_id_match = re.match(r'^(\d{10,})\.md$', basename)

            if not task_id_match:
                log.warning(f"Cannot determine task_id for {old_file}, skipping")
                continue

            task_id = task_id_match.group(1) if task_id_match.lastindex else task_id_match.group(0)

            # Extract date
            date_match = re.search(r'\*\*Date\*\*:\s*(\d{4}-\d{2}-\d{2})', content)
            date_iso = date_match.group(1) if date_match else '2025-01-01'

            # Extract title from first ## heading
            title_match = re.search(r'^##\s+(.+)$', content, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else 'Migrated Article'

            # Generate slug
            slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:40]
            if not slug:
                slug = f'migrated-{task_id[:8]}'

            # All old articles are tasks
            new_filename = f'{date_iso}_{slug}.md'
            new_path = os.path.join(WIKI_ROOT, DIR_TASKS, new_filename)

            # Handle duplicates
            if os.path.exists(new_path):
                new_filename = f'{date_iso}_{slug}_{task_id[:8]}.md'
                new_path = os.path.join(WIKI_ROOT, DIR_TASKS, new_filename)

            # Build frontmatter
            # Extract existing content after the header
            content_lines = content.split('\n')
            # Find where the actual article starts (after ---)
            article_start = 0
            for i, line in enumerate(content_lines):
                if line.strip() == '---' and i > 0:
                    article_start = i + 1
                    break

            article_body = '\n'.join(content_lines[article_start:]) if article_start > 0 else content

            # Build new article with frontmatter
            tags_from_content = _extract_tags_from_text(article_body)

            new_content = f"""---
title: "{title}"
type: task
status: completed
created: {date_iso}
updated: {datetime.now().strftime("%Y-%m-%d")}
tags: {json.dumps(tags_from_content, ensure_ascii=False)}
task_id: "{task_id}"
source: cline-task-migrated
---

# {title}

{article_body}
"""

            with open(new_path, 'w', encoding='utf-8') as f:
                f.write(new_content)

            # Copy original to raw/
            raw_path = os.path.join(WIKI_ROOT, DIR_RAW, f'{date_iso}_{task_id}_raw.md')
            with open(raw_path, 'w', encoding='utf-8') as f:
                f.write(content)

            # Remove old file
            os.remove(old_file)

            log.info(f"Migrated: {old_file} -> {new_path}")
            migrated += 1

        except Exception as e:
            log.error(f"Migration error for {old_file}: {e}")
            continue

    log.info(f"Migration complete. Migrated: {migrated}")

    if migrated > 0:
        update_index()
        update_tags()


def _extract_tags_from_text(text: str) -> list:
    """Extract potential tags from article text based on keyword patterns."""
    tags = ['migrated']
    tech_keywords = {
        'python': r'\bpython\b',
        'javascript': r'\bjavascript\b',
        'html': r'\bhtml\b',
        'css': r'\bcss\b',
        'json': r'\bjson\b',
        'xml': r'\bxml\b',
        'cline': r'\bcline\b',
        'esp32': r'\besp32\b',
        'arduino': r'\barduino\b',
        'docker': r'\bdocker\b',
        'api': r'\bapi\b',
        'mcp': r'\bmcp\b',
        'llm': r'\bllm\b',
        'wiki': r'\bwiki\b',
    }
    for tag, pattern in tech_keywords.items():
        if re.search(pattern, text, re.IGNORECASE):
            tags.append(tag)
    return tags


def run_daemon():
    """Background process - scans every INTERVAL seconds."""
    log.info(f"Daemon started (interval: {INTERVAL}s, model: {MODEL})")
    log.info(f"Tasks dir: {TASKS_DIR}")
    log.info(f"Wiki root: {WIKI_ROOT}")

    while True:
        try:
            scan_all()
        except Exception as e:
            log.error(f"Daemon loop error: {e}")

        log.info(f"Waiting {INTERVAL}s until next scan...")
        time.sleep(INTERVAL)


# --- CLI ---
def main():
    import argparse

    parser = argparse.ArgumentParser(description='Knowledge Scanner v2 - Cline to Wiki')
    parser.add_argument('--daemon', '-d', action='store_true', help='Run as daemon')
    parser.add_argument('--once', '-1', action='store_true', help='Process all new tasks once')
    parser.add_argument('--test', '-t', action='store_true', help='Dry run (no LLM)')
    parser.add_argument('--limit', '-n', type=int, default=0, help='Max tasks per run (0=all)')
    parser.add_argument('--task', type=str, help='Process specific task by ID')
    parser.add_argument('--output', '-o', type=str, help='Output dir override')
    parser.add_argument('--migrate', '-m', action='store_true', help='Migrate old flat articles to new structure')
    parser.add_argument('--reindex', action='store_true', help='Rebuild _index.md and _tags.md only')
    parser.add_argument('--reprocess', type=str, help='Reprocess raw file by path')

    args = parser.parse_args()

    global WIKI_ROOT
    if args.output:
        WIKI_ROOT = args.output

    ensure_dirs()

    signal.signal(signal.SIGINT, lambda *_: (log.info("Stopped"), sys.exit(0)))

    if args.migrate:
        log.info("Running migration...")
        migrate_old_articles()
    elif args.reindex:
        log.info("Rebuilding indexes...")
        update_index()
        update_tags()
    elif args.reprocess:
        # Reprocess a raw conversation file
        raw_path = args.reprocess
        if os.path.exists(raw_path):
            with open(raw_path, 'r', encoding='utf-8') as f:
                raw_content = f.read()
            # Extract conversation after ---
            parts = raw_content.split('---\n', 2)
            conv_text = parts[-1] if len(parts) > 2 else raw_content
            truncated = truncate_for_llm(conv_text)
            article = call_summarizer(truncated)
            # Extract task_id from raw filename
            tid_match = re.search(r'(\d{10,})_raw\.md$', raw_path)
            task_id = tid_match.group(1) if tid_match else 'unknown'
            date_match = re.search(r'^(\d{4}-\d{2}-\d{2})', os.path.basename(raw_path))
            created_date = date_match.group(1) if date_match else datetime.now().strftime('%Y-%m-%d')
            article_md = build_article_markdown(article, task_id, created_date, created_date, len(conv_text))
            article_type = article.get('type', 'task')
            type_dir = os.path.join(WIKI_ROOT, TYPE_DIRS.get(article_type, DIR_TASKS))
            slug = article.get('slug', 'reprocessed')
            out_path = os.path.join(type_dir, f'{created_date}_{slug}.md')
            with open(out_path, 'w', encoding='utf-8') as f:
                f.write(article_md)
            log.info(f"Reprocessed: {out_path}")
            update_index()
            update_tags()
        else:
            log.error(f"Raw file not found: {raw_path}")
    elif args.task:
        task_dir = os.path.join(TASKS_DIR, args.task)
        if os.path.exists(task_dir):
            process_task(task_dir, dry_run=args.test)
        else:
            log.error(f"Task not found: {task_dir}")
    elif args.test:
        log.info("Test mode (dry run, no LLM)")
        scan_all(dry_run=True, limit=args.limit or 1)
    elif args.once:
        log.info("One-time run")
        scan_all(limit=args.limit)
    elif args.daemon:
        run_daemon()
    else:
        log.info("Default: test on 1 task (use --daemon, --once, --test, --migrate, or --reindex)")
        scan_all(dry_run=True, limit=1)


if __name__ == '__main__':
    main()