# Деплой ноды на флот дронов (Ansible)

Раскатывает openclaw bridge-ноду (`real_bridge.py` + зависимости + разметку
мата) на N реальных бортов одной командой. Логика запуска — общая с
однодронным `scripts/deploy_drone.sh` (`scripts/_drone_bridge_launch.sh`),
Ansible только транспорт.

Лётный рецепт зашит дефолтами: **взлёт по `body`** (маркеры с земли не видны),
**полёт в `aruco_map`** (общий кадр поля), сервисы offboard в корне
(`/navigate`).

## Запуск

```bash
# зависимости оператора (один раз)
sudo apt install ansible sshpass

# весь флот
make deploy-fleet
# или напрямую / один борт:
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/deploy.yml
ansible-playbook -i deploy/ansible/inventory.yml deploy/ansible/deploy.yml -l drone-1
```

## Добавить борт

Один блок в `inventory.yml`: адрес, способ подключения (в контейнер напрямую
`:22 sverk/sverk` или через хост Orange Pi `:2222 orangepi/orangepi` +
`sverk_container`), `instance`, `bridge_port`, `start_cell`. Общие параметры
(кадры, высота, размер клетки, список файлов) — `group_vars/drones.yml`.

## Что делает плейбук

1. Копирует файлы ноды + `markers.txt` + лаунчер в `node_dir` борта
   (и `docker cp` в контейнер для способа Б).
2. Перезапускает бридж лаунчером: тот сам убивает старый процесс, стартует с
   env лётного рецепта и проверяет `healthz` (fail → плейбук красный с хвостом
   лога борта).
3. `POST /set_cell` — говорит борту, над какой клеткой он стоит.

После деплоя агенты на хосте оператора подключаются как обычно:
`BRIDGE_URL=http://<ip-борта>:<port>` у flyer в `docker-compose.test.yml`
(см. docs/test-3x3.md).
