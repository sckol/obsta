---
Template: Synced note
Path: /.gitignore
---
## *~~Назначение~~*
*~~[[Синхронизованная заметка|Синхронизованная заметка]] для .gitignore в корне хранилища. Git использует этот файл, чтобы не предлагать добавление неотслеживаемых служебных и локальных файлов. Уже отслеживаемые файлы правила не исключают.~~*
*~~Меняйте правила при появлении новых временных файлов или локальных настроек. Сейчас исключены кеш Python, файлы macOS, настройки Obsidian и Codex, а также Configs/obsta.local.md. Строка с obsta-history — комментарий, её следует сохранить.~~*
## *~~Действия~~*
- [ ] *~~Проверить, нет ли среди неотслеживаемых файлов новых служебных файлов, и предложить правила для .gitignore.~~*
## *~~Конфигурация~~*
*~~Полное содержимое рабочего файла.~~*
## *~~File content~~*
```gitignore
# Python
__pycache__/
*.py[cod]

# macOS
.DS_Store

# Obsidian workspace state
.obsidian/workspace.json
.obsidian/workspace-mobile.json

# Local editor, agent and user settings
/.codex/
/.obsidian/
/Configs/obsta.local.md
# obsta-history: /Configs/obsta.local.md
```
