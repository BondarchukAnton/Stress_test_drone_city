# drone_main.py

import os

def main():
    handler_id = os.getenv('HANDLER_ID')
    fleet = os.getenv('FLEET')
    led = os.getenv('LED')
    
    print(f"Бортовой исполнитель задач. HANDLER_ID: {handler_id}, FLEET: {fleet}, LED: {led}")

if __name__ == '__main__':
    main()
