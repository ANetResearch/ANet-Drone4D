# libDrone4DGz.so (M24 §6.1): Drone4DClock and Drone4DVelocityControl in one library so the D4dVelocityCmd component is
# registered once. Built inside the d3-gz image (tools/deploy/dmax/images/gz): gz-sim 8 (Harmonic, ROS 2 Jazzy vendor
# packages), libzmq + cppzmq, protobuf, Eigen.
cmake_minimum_required(VERSION 3.22)
project(drone4d_gz LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(D4D_PLUGIN_BUILD "dev" CACHE STRING "plugin build id reported in HelloAck")

find_package(gz-sim8 REQUIRED)
find_package(gz-plugin2 REQUIRED COMPONENTS register)
find_package(Eigen3 REQUIRED)
find_package(Protobuf REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(ZMQ REQUIRED IMPORTED_TARGET libzmq)

protobuf_generate_cpp(D4D_PROTO_SRCS D4D_PROTO_HDRS proto/drone4d_gz.proto)

add_library(Drone4DGz SHARED
  src/Drone4DClock.cc src/Drone4DVelocityControl.cc src/Common.cc src/LeeVelocityController.cc ${D4D_PROTO_SRCS})
target_include_directories(Drone4DGz PRIVATE src ${CMAKE_CURRENT_BINARY_DIR})
target_compile_definitions(Drone4DGz PRIVATE D4D_PLUGIN_BUILD="${D4D_PLUGIN_BUILD}")
target_link_libraries(Drone4DGz PRIVATE gz-sim8::gz-sim8 gz-plugin2::register Eigen3::Eigen protobuf::libprotobuf PkgConfig::ZMQ)
install(TARGETS Drone4DGz LIBRARY DESTINATION lib)
