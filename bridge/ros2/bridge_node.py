# bridge_node.py

import rospy

def translate_command_to_ros_topic(command):
    rospy.loginfo(f"Трансляция HTTP-команды ИИ в топики ROS 2: {command}")

if __name__ == '__main__':
    rospy.init_node('bridge_node')
    rospy.spin()
