#!/usr/bin/env python3
"""fly_photo_land_pw.py — взлёт, фото каждые 3с, посадка.
Использование: python3 fly_photo_land_pw.py <ip> <password> <filename>
  filename: drone{id}_{col}_{row}.jpg  (координаты + секунда добавятся авто)
"""

import paramiko
import sys
import os
import time
import tempfile

DRONE_USER = "pi"
ALTITUDE = 1.7
INTERVAL = 3.0
FLIGHT_SECONDS = 18

ONBOARD_SCRIPT = f'''
import time
import sverk_interfaces

drone = sverk_interfaces.init(Nodename="photo_mission")
print("ARMING...")
drone.control.navigate(x=0, y=0, z={ALTITUDE}, yaw=0, speed=0.5, frame_id="body", auto_arm=True)
time.sleep(2)
print("TAKEOFF_OK")

t0 = time.time()
while time.time() - t0 < {FLIGHT_SECONDS}:
    elapsed = int(time.time() - t0)
    frame = drone.image.take_picture(timeout=3.0)
    raw = bytes(frame.data if hasattr(frame, "data") else frame)
    fname = f"/tmp/{{elapsed}}s.jpg"
    with open(fname, "wb") as f:
        f.write(raw)
    print(f"PHOTO:{fname}")
    time.sleep({INTERVAL})

drone.control.land(timeout=15)
print("LAND_OK")
'''

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    drone_ip = sys.argv[1]
    password = sys.argv[2]
    base_name = sys.argv[3]
    name_no_ext = os.path.splitext(base_name)[0]

    print(f"[{drone_ip}] Подключение к дрону...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(hostname=drone_ip, username=DRONE_USER,
                    password=password, timeout=30)
        print(f"[{drone_ip}] Подключено. Запуск полётной программы...")

        sftp = ssh.open_sftp()
        remote_script = "/tmp/_fly_mission.py"
        with sftp.open(remote_script, "w") as f:
            f.write(ONBOARD_SCRIPT)
        sftp.close()

        stdin, stdout, stderr = ssh.exec_command(f"python3 {remote_script}")
        out = stdout.read().decode()
        err = stderr.read().decode()
        print(out.strip())
        if err:
            print(f"STDERR: {err.strip()}", file=sys.stderr)

        sftp = ssh.open_sftp()
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("PHOTO:"):
                remote_path = line.split(":", 1)[1].strip()
                remote_basename = os.path.basename(remote_path)
                local_name = f"{name_no_ext}_{remote_basename}"
                local_path = os.path.join(SCRIPT_DIR, local_name)
                sftp.get(remote_path, local_path)
                sftp.remove(remote_path)
                print(f"  Скачан: {local_name}")
        sftp.remove(remote_script)
        sftp.close()

    finally:
        ssh.close()

    print(f"[{drone_ip}] Завершено.")


if __name__ == "__main__":
    main()