cmake_minimum_required(VERSION 3.22)
project(mcp_image LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# libjpeg-turbo explicitly requires its own top-level CMake build.
include(ExternalProject)
set(MCP_JPEG_SOURCE "${CMAKE_CURRENT_SOURCE_DIR}/third_party/libjpeg-turbo")
set(MCP_JPEG_BINARY "${CMAKE_CURRENT_BINARY_DIR}/turbojpeg")
set(MCP_REQUIRE_SIMD OFF)
if(ANDROID_ABI STREQUAL "arm64-v8a")
    set(MCP_REQUIRE_SIMD ON)
endif()
ExternalProject_Add(mcp_turbojpeg_build
    SOURCE_DIR "${MCP_JPEG_SOURCE}"
    BINARY_DIR "${MCP_JPEG_BINARY}"
    UPDATE_COMMAND ""
    CMAKE_ARGS
        "-DCMAKE_TOOLCHAIN_FILE=${CMAKE_TOOLCHAIN_FILE}"
        "-DCMAKE_MAKE_PROGRAM=${CMAKE_MAKE_PROGRAM}"
        "-DANDROID_ABI=${ANDROID_ABI}"
        "-DANDROID_PLATFORM=${ANDROID_PLATFORM}"
        "-DANDROID_NDK=${ANDROID_NDK}"
        -DCMAKE_BUILD_TYPE=Release
        -DCMAKE_POSITION_INDEPENDENT_CODE=ON
        -DENABLE_SHARED=OFF
        -DENABLE_STATIC=ON
        -DWITH_TURBOJPEG=ON
        -DWITH_SIMD=ON
        "-DREQUIRE_SIMD=${MCP_REQUIRE_SIMD}"
        -DWITH_TOOLS=OFF
        -DWITH_TESTS=OFF
        -DWITH_JNA=
        -DWITH_SYSTEM_ZLIB=ON
    BUILD_COMMAND "${CMAKE_COMMAND}" --build <BINARY_DIR> --target turbojpeg-static --parallel 4
    INSTALL_COMMAND ""
    BUILD_BYPRODUCTS "${MCP_JPEG_BINARY}/libturbojpeg.a"
)
add_library(mcp_turbojpeg STATIC IMPORTED)
set_target_properties(mcp_turbojpeg PROPERTIES
    IMPORTED_LOCATION "${MCP_JPEG_BINARY}/libturbojpeg.a"
    INTERFACE_INCLUDE_DIRECTORIES "${MCP_JPEG_SOURCE}/src"
)
add_dependencies(mcp_turbojpeg mcp_turbojpeg_build)

set(PNG_SHARED OFF CACHE BOOL "" FORCE)
set(PNG_STATIC ON CACHE BOOL "" FORCE)
set(PNG_TESTS OFF CACHE BOOL "" FORCE)
set(PNG_TOOLS OFF CACHE BOOL "" FORCE)
set(PNG_HARDWARE_OPTIMIZATIONS ON CACHE BOOL "" FORCE)
if(ANDROID_ABI STREQUAL "arm64-v8a")
    set(PNG_ARM_NEON on CACHE STRING "" FORCE)
endif()
add_subdirectory(third_party/libpng libpng EXCLUDE_FROM_ALL)
target_compile_options(png_static PRIVATE -O3)

find_library(MCP_ZLIB z REQUIRED)
find_library(MCP_ANDROID android REQUIRED)
find_library(MCP_JNIGRAPHICS jnigraphics REQUIRED)
find_library(MCP_LOG log REQUIRED)

add_library(mcp_image SHARED native_image.cpp)
target_compile_options(mcp_image PRIVATE -O3 -Wall -Wextra -fvisibility=hidden)
target_link_options(mcp_image PRIVATE
    "-Wl,-z,max-page-size=16384"
    "-Wl,-z,common-page-size=16384"
    "-Wl,--exclude-libs,ALL"
)
target_link_libraries(mcp_image PRIVATE mcp_turbojpeg png_static
    "${MCP_ZLIB}" "${MCP_ANDROID}" "${MCP_JNIGRAPHICS}" "${MCP_LOG}")

# Standalone safe file operations; intentionally independent of all media libraries.
add_library(mcp_file_ops SHARED native_file_ops.cpp)
target_compile_options(mcp_file_ops PRIVATE -O2 -Wall -Wextra -fvisibility=hidden)
target_link_options(mcp_file_ops PRIVATE "-Wl,-z,max-page-size=16384" "-Wl,-z,common-page-size=16384")
