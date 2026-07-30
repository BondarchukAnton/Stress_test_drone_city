@echo off
start "drone_110" python fly_photo_land_pw.py 192.168.1.110 sverk drone110_2_5.jpg
start "drone_124" python fly_photo_land_pw.py 192.168.1.124 sverk drone124_5_5.jpg
start "drone_116" python fly_photo_land_pw.py 192.168.1.116 sverk drone116_2_2.jpg
start "drone_111" python fly_photo_land_pw.py 192.168.1.111 sverk drone111_5_2.jpg
echo All 4 drones launched.
pause