cmake_minimum_required(VERSION 3.5)
project(unitree_go)

# Standard rosidl message-only package pattern for ROS 2 Foxy.
# See: https://docs.ros.org/en/foxy/Tutorials/Beginner-Client-Libraries/
#      Custom-ROS2-Interfaces.html

if(NOT CMAKE_CXX_STANDARD)
  set(CMAKE_CXX_STANDARD 14)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)

# List all .msg files here. Add more entries as new topics are needed
# (e.g. "msg/Go2FrontDepthData.msg" for depth, "msg/IMUState.msg" for IMU).
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Go2FrontVideoData.msg"
)

ament_export_dependencies(rosidl_default_runtime)
ament_package()
