---
Template: Building block
Status: Project, Implemented, Obsolete
---
## User requirements
### Purpose
Скилл реализует то, что описано в [[004-arc67-structure]]
### Interface

### Depends from

### Additional requirements

### Actions
- [ ] Brainstorm: ask me some questions about the text above and challenge the idea *
- [ ] (Re) make a design spec below with implementation plan
- [ ] (Re) make a design spec below with implementation plan + implement without stop
- [ ] Refactor the block (drop everything except User requirements (this section) and implement from scratch)
- [ ] Additional actions can be freely added by user or agent
#KOL-109🍏 __Реализуй что написано, ранее часть того что есть в [[Скилл инициализации arc67]] было интегрировано в arc67, надо вынести это отдельно теперь__

*◇ Вынес инициализацию в отдельный скилл `arc67init`. Команда `obsta arc67init <путь к vault>` создаёт недостающие папки, `Architecture.md` и `Glossary.base`, обновляет штатные шаблоны по именам и выбирает `Conventions/Templates` в Obsidian. Существующие Architecture и Glossary, дополнительные шаблоны и пользовательские правки при автоматической установке через `obsta add` и `obsta start` сохраняются.*

*Прошли 3 целевых теста и 23 самопроверки obstagent. Проверил сборку wheel из sdist, запуск в двух временных vault и обнаружение скилла Codex из репозитория и общего каталога. Настройки Obsidian проверил по JSON.*

*Изменения — [[Sources/arc67init]] — описание реализации и приёмки; [[Sources/arc67init/SKILL]] — скилл; [[.agents/skills/arc67init]] — ссылка для Codex; [[Sources/arc67init/test_arc67init.py]] — тесты; [[Sources/obstagent/obsta/arc67init.py]] — создание структуры; [[Sources/obstagent/obsta/assets.py]] — обновление шаблонов и установка скилла; [[Sources/obstagent/obsta/cli.py]] — команда; [[Sources/obstagent/setup.py]] и [[Sources/obstagent/pyproject.toml]] — упаковка скилла.*
#KOL-109🛑
## Specification
### Architecture and techniques

*~~[[Scenarios/004-arc67-structure]] задаёт состав хранилища. Отдельный скилл вызывает `obsta arc67init <vault>`. Явный запуск обновляет штатные шаблоны по именам. Регистрация и запуск obstagent продолжают устанавливать шаблоны по [[Scenarios/003-multiproject]] с сохранением пользовательских правок.~~*

Describe what this block must do and how it can be implemented. Link to the relevant scenarios and requirement sections, stating what each reference defines; add only requirements missing from those documents.
### Acceptance

*~~`PYTHONPATH=Sources/obstagent python3 -m unittest discover -s Sources/arc67init -v` проверяет чистое и существующее хранилища, повторный запуск и подключение скилла.~~*

State how to verify the required behavior, including only setup details needed to reproduce the checks. Record the list of concrete commands to run the acceptance checks.
### Actions
- [ ] Implement the design spec
- [ ] Implement the design spec (force "from scratch mode")
## Working context
### Files

*~~[[Sources/arc67init/SKILL|arc67init]] — вход в скилл. [[Sources/obstagent/obsta/arc67init.py|arc67init.py]] — структура хранилища. [[Sources/arc67init/test_arc67init.py|test_arc67init.py]] — проверки.~~*

Hierarchical list of links to files and folders with a very brief description for each.
### Implementation details
Keep only non-obvious details useful for maintenance not stated anywhere else, not requirements for a rewrite.
### Proposed changes for current vault
Link to proposed documentation changes and briefly explain why they are needed. You can use checklists to make a ready prompt for agent.
### Problems
List contradictions, non-implemented parts, unresolved conflicts, unmet requirements, and failed or unrun checks.