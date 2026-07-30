import paramiko

ROBOT_IP = "192.168.1.201"
USERNAME = "pi"
PASSWORD = "raspberry"


def run_command(fire_coord_x, fire_coord_y, kolvo_fire):
    URL = "http://192.168.1.201:8767"
    CLIENT = "tools/rover_control_client.py"

    init_coord_x = 1
    init_coord_y = 1

    water_coord_x = 5
    water_coord_y = 4

    commands = [
        "cd sverk_rover",
        "source install/setup.zsh",
        f'python3 "{CLIENT}" --url "{URL}" field-status',
        f'python3 "{CLIENT}" --url "{URL}" cell {init_coord_x} {init_coord_y}',
        f'python3 "{CLIENT}" --url "{URL}" initial-cell {init_coord_x} {init_coord_y} --yaw 0',
        f'python3 "{CLIENT}" --url "{URL}" clear'
        #f'python3 "{CLIENT}" --url "{URL}" goal-cell 6 4 --replace',    # здесь вписываем координаты ближайшей клетки
        #f'python3 "{CLIENT}" --url "{URL}" goal-cell {init_coord_x} {init_coord_y} --replace',    # две команды чтобы ровер проехал тудасюда для баллов, но таким образом их врят ли засчитают
    ]

    # делаем циклы тушения пожара       здесь еще должна быть команада dwell но ее пока нет
    for i in range(1, kolvo_fire + 1):
        commands.append(f'python3 "{CLIENT}" --url "{URL}" goal-cell {water_coord_x} {water_coord_y} --replace')
        commands.append('sleep 3.5')
        commands.append(f'python3 "{CLIENT}" --url "{URL}" goal-cell {fire_coord_x} {fire_coord_y} --replace')

    # возвращаемся на старт
    commands.append(f'python3 "{CLIENT}" --url "{URL}" goal-cell {init_coord_x} {init_coord_y} --replace')

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(
            hostname=ROBOT_IP,
            username=USERNAME,
            password=PASSWORD,
            timeout=30,
        )
        print(f"Подключено к {USERNAME}@{ROBOT_IP}")

        full_command = " && ".join(commands)

        print(f"Выполняем цепочку:\n{full_command}\n")

        stdin, stdout, stderr = ssh.exec_command(full_command)

        out = stdout.read().decode("utf-8")
        err = stderr.read().decode("utf-8")

        print("--- Результат ---")
        print(out if out else "Команда выполнена (нет вывода).")

        if err:
            print("--- Ошибки / Предупреждения ---")
            print(err)

    finally:
        ssh.close()


if __name__ == "__main__":
    run_command(3, 1, 2)
