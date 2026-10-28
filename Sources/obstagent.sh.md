*Это shell-обёртка для [[Sources/obstagent.py|watcher]]: она находит корень vault относительно своего расположения, переходит в него и передаёт Python все аргументы запуска. Если watcher завершился с кодом `75` после проверки обновлённого исходника, обёртка запускает его снова. При любом другом коде она завершает работу с тем же кодом. Скрипт нужен, чтобы новая версия watcher начинала работать после обновления без ручного запуска.*

```sh
#!/bin/sh

vault_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd -- "$vault_dir" || exit 1

while true; do
    python3 Sources/obstagent.py --vault "$vault_dir" "$@"
    status_code=$?
    [ "$status_code" -eq 75 ] || exit "$status_code"
done
```

См. [[Запуск и перезапуск obstagent]]

*Файл на диске — `Sources/obstagent.sh`; запуск из корня: `./Sources/obstagent.sh`. Python использует договор [[Использование Git]]; выход `75` возможен после сохранённого Б и проверки новой версии, чистота HEAD не требуется.*
