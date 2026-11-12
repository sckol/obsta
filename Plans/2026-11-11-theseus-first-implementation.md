---
Template: Implementation plan
Status: Draft
Plan type: First implementation
Module: "[[Sources/theseus]]"
---
## *~~Context~~*
### *~~Goal~~*
*~~Собрать адаптированные скиллы Тесея и реализовать проверяемое ядро подготовки контекста, заданий, продолжения и возврата результата без коммитов. Требования и технические контракты — [[Sources/theseus#Specification]], критерии A1–A5 — [[Sources/theseus#Acceptance]].~~*

### *~~Specification and scope~~*
*~~Разрешённая запись: Sources/theseus/ со всеми файлами; Specification и Working context в [[Sources/theseus]] только в пределах текущего этапа; этот план. User requirements, обсуждения, тикеты и служебные строки сохраняются. Исходники других блоков, References, Conventions, Configs и настройки среды не изменяются. Для встроенных тестов используются временные репозитории. При реализации этого блока его собственные инструменты проверяются на тестовых модулях, не очищают свой работающий код.~~*

### *~~Plan status~~*
*~~Draft: реализация не начата. Задачи выполняются сверху вниз; планировщик меняет задачи, оркестратор фиксирует прогресс. Состояния задач: pending, in progress, in review, blocked, interrupted, done. Done требует проверок и независимого ревью. Продолжение — с первой незавершённой задачи после сверки файлов и сессий.~~*

## *~~Tasks~~*
### *~~Execution order and progress~~*

| *~~#~~* | *~~Task / files~~* | *~~Verification~~* | *~~Progress / result~~* |
| --- | --- | --- | --- |
| *~~1~~* | *~~Sources/theseus/upstream.lock, build.py, patches/series, tests/test_build.py: закрепить архив, безопасную распаковку, порядок патчей и атомарную повторяемую сборку с LICENSE.~~* | *~~`python3 -m unittest discover -s Sources/theseus/tests -p 'test_build.py'`; корректная сборка, неверный хеш, опасный архив и конфликт патча; A1.~~* | *~~pending; not run~~* |
| *~~2~~* | *~~`Sources/theseus/patches/*.patch`, build.py, tests/test_skills.py: адаптировать 11 скиллов и их вспомогательные файлы; единая точка входа, пути obsta, предложения, режим без коммитов, этапы, TDD и ревью.~~* | *~~python3 Sources/theseus/build.py --check и `python3 -m unittest discover -s Sources/theseus/tests -p 'test_skills.py'`; A1, все адаптированные внутренние ссылки разрешаются.~~* | *~~pending; not run~~* |
| *~~3~~* | *~~Sources/theseus/context.py, cli.py, tests/test_context.py: JSON-схема v1, маршрутизация режимов и этапов, вывод scope из заметки и Module, detached worktree с текущими входами, манифест и очистка контекста.~~* | *~~`python3 -m unittest discover -s Sources/theseus/tests -p 'test_context.py'`; A2, включая dirty/untracked/deleted и неизменность основного vault при рефакторинге.~~* | *~~pending; not run~~* |
| *~~4~~* | *~~Sources/theseus/sessions.py, cli.py, `patches/*.patch`, tests/test_sessions.py: brief/record/resume, адаптер start/status/result/resume, настройки ролей, атомарный журнал, блокировка модуля, сверка сессий и продвижение статусов.~~* | *~~`python3 -m unittest discover -s Sources/theseus/tests -p 'test_sessions.py'`; A3, счётчик запусков исключает дубликаты, неполные evidence/review блокируют Done.~~* | *~~pending; not run~~* |
| *~~5~~* | *~~Sources/theseus/transfer.py, cli.py, tests/test_transfer.py: collect/apply/cleanup; перенос только результата, сохранение прозы и конкурентных правок, конфликты, идемпотентность и восстановление частичного переноса.~~* | *~~`python3 -m unittest discover -s Sources/theseus/tests -p 'test_transfer.py'`; A4, сравнение байтов сохранённых областей, индекса и obsta-history.~~* | *~~pending; not run~~* |
| *~~6~~* | *~~Sources/theseus/tests/test_workflow.py, `patches/*.patch`: связать четыре режима с ядром; полный цикл TDD, замечание ревью, повторная проверка и перенос; тестовые сбои и продолжение.~~* | *~~python3 Sources/theseus/build.py --check; затем `python3 -m unittest discover -s Sources/theseus/tests -p 'test_*.py'`; A1–A4, exit code 0 у обеих команд; итоговое независимое ревью diff.~~* | *~~pending; not run~~* |
| *~~7~~* | *~~Sources/theseus/tests/fixtures/ при необходимости, Working context заметки блока и Outcome этого плана: провести A5 с доступным реальным адаптером в отдельном временном vault, записать результаты и ограничения.~~* | *~~A5: наблюдаемые session_id, независимый ревьюер, возврат изменений и продолжение после замечаний; при недоступной интеграции записать Blocked и точную следующую задачу.~~* | *~~pending; not run~~* |

### *~~Task details~~*
*~~У всех команд проверки cwd — корень vault; зависимости — Python 3, Git и локальный архив из [[References/Superpowers]]. Сначала падающий поведенческий тест, затем реализация; задачи последовательно проходят независимое ревью. Для №7 загрузка точки входа и адаптер должны быть доступны в тестовой среде. Изменения основного obstagent и его инструкции требуют отдельной разрешённой интеграционной задачи из [[Sources/theseus#Proposed changes for current vault]]; нехватку этой интеграции нельзя обходить расширением scope.~~*

## *~~Actions~~*
- [ ] *~~Выполнить план: прочитать Module, Specification и Acceptance; проверить область записи; выполнить задачи последовательно с проверками и ревью; записать прогресс, Status и блокеры.~~*

- [ ] *~~Продолжить план: сверить весь план, текущие файлы, журнал и живых участников; продолжить первую незавершённую задачу без дублирования; обновить прогресс и Status.~~*

## *~~Outcome~~*
### *~~Final verification~~*
*~~Не выполнена: план подготовлен, код не реализован. После исполнения записать команды A1–A4, фактические результаты A5, итог независимого ревью и непроведённые проверки. Status=Done допустим только после выполнения всех семи задач, обязательных проверок и итогового ревью.~~*

### *~~Interruption and next step~~*
*~~Прерывания нет. Следующий шаг при активации реализации — задача 1. Worktree пока не создан. При блокере сохранить worktree, точную причину и безопасный шаг продолжения; приёмку пользователя учитывать отдельно, cleanup до неё запрещён.~~*
