# Knowledge Scanner v2 — Cline Conversation to Wiki

> Автоматическое создание wiki-статей из диалогов Cline (VS Code) через LLM.

[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![OpenRouter](https://img.shields.io/badge/LLM-OpenRouter%20Qwen-orange.svg)](https://openrouter.ai/)

**Knowledge Scanner** — это Python-скрипт, который сканирует историю диалогов Cline (VS Code расширение), очищает их от технического шума (environment_details, tool XML, system blocks и т.д.) и через дешёвую LLM (Qwen 2.5 7B) создаёт структурированные wiki-статьи.

## ✨ Возможности

- 🔍 **Сканирует** `api_conversation_history.json` из задач Cline
- 🧹 **Чистит** от служебного шума (~64% сокращение объёма)
- 🤖 **Суммаризирует** через Qwen 2.5 7B (OpenRouter) — ~$0.002 за задачу
- 📝 **Создаёт** markdown-статьи с YAML frontmatter, тегами и wikilinks
- 🏷️ **Классифицирует** статьи на concepts / entities / tasks
- 🔗 **Генерирует** `_index.md` и `_tags.md` для навигации
- 🔄 **Режим daemon** — фоновый запуск с интервалом
- 🔁 **Поддержка reprocess** — переработка сырых файлов при улучшении промпта

## 🎯 Для кого это

- **Пользователи Cline (VS Code)**, которые хотят сохранять знания из диалогов
- **Разработчики AI-агентов**, строящие персистентную базу знаний
- **Все, кто использует** `api_conversation_history.json` для анализа работы с LLM

## 🚀 Быстрый старт

### 1. Установка

```bash
# Клонировать
git clone https://github.com/YOUR_USERNAME/cline-knowledge-scanner.git
cd cline-knowledge-scanner

# Зависимости
pip install requests
```

### 2. Настройка API ключа

```bash
# Windows (CMD)
set OPENROUTER_API_KEY=sk-or-v1-your-key-here

# Windows (PowerShell)
$env:OPENROUTER_API_KEY="sk-or-v1-your-key-here"
```

Получить ключ: [OpenRouter.ai](https://openrouter.ai/)

### 3. Запуск

```bash
# Обработать все новые задачи
python scan_tasks.py --once

# Тестовый запуск (без LLM, сохраняет очищенный текст)
python scan_tasks.py --test --limit 1

# Обработать конкретную задачу
python scan_tasks.py --task 1769879602668

# Фоновый режим (проверяет каждые 3 минуты)
python scan_tasks.py --daemon

# Пересоздать индексы
python scan_tasks.py --reindex
```

## 📁 Структура Wiki

```
C:\Users\User\Documents\knowledge-wiki\
├── concepts/      -- Фреймворки, методологии, идеи
├── entities/      -- Инструменты, продукты, платформы
├── tasks/         -- Конкретные задачи (по датам)
├── system/        
│   ├── _index.md  -- Оглавление (авто-генерируется)
│   └── _tags.md   -- Индекс тегов (авто-генерируется)
└── raw/           -- Сырые диалоги (для reprocess)
```

Формат статей: `YYYY-MM-DD_slug.md`

## 📋 Формат статьи

```markdown
---
title: "Название статьи"
type: task|concept|entity
status: completed|in-progress|unclear
created: 2026-01-25
updated: 2026-04-21
tags: ["тег1", "тег2"]
task_id: "1769369499864"
source: cline-task
---

# Название статьи

## Summary
Краткое описание.

## Key Facts
- Факт 1 с [[wikilink]]
- Факт 2

## Results
- [x] Результат 1

## Timeline
- **2026-01-25 22:31** | Событие

## See Also
- [[связанная-статья]]
```

## 🔧 Технические детали

| Параметр | Значение |
|----------|----------|
| LLM | Qwen 2.5 7B Instruct (OpenRouter) |
| Стоимость | ~$0.002 за задачу |
| Шумоочистка | ~64% сокращение объёма |
| Маркер обработки | `.done` файл в папке задачи |
| Макс. сообщений | 30 на задачу |
| Лог | `scanner.log` рядом со скриптом |

## 🧩 Планировщик Windows

1. Откройте `taskschd.msc`
2. Создайте задачу:
   - **Имя:** Knowledge Scanner
   - **Триггер:** Каждые 30 минут
   - **Действие:** Запуск программы
   - **Программа:** `pythonw.exe`
   - **Аргументы:** `C:\path\to\scan_tasks.py --once`

## 🙏 Благодарности

- **[Andrej Karpathy](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)** — автор концепции **LLM Wiki Pattern**, вдохновившей этот проект
- **[dazeb/cline-mcp-memory-bank](https://github.com/dazeb/cline-mcp-memory-bank)** — репозиторий, с которого началась работа над данной системой памяти

## 🔍 Поисковые теги

`cline` `vscode` `knowledge-base` `wiki` `llm-wiki` `memory` `openrouter` `qwen` `python` `obsidian` `markdown` `conversation-history` `api-conversation-history` `cline-extension` `ai-agent` `personal-knowledge-management` `pkm` `second-brain`

## 📄 Лицензия

MIT