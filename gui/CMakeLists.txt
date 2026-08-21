cmake_minimum_required(VERSION 3.20)
project(ut3convgui LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(SDL3 CONFIG REQUIRED)
find_package(imgui CONFIG QUIET)

# vcpkg has imgui with the SDL3 bindings; without it, fetch and build the
# five core files plus the two backends, the way utcolor does.
if(NOT imgui_FOUND)
    include(FetchContent)
    FetchContent_Declare(
        imgui
        GIT_REPOSITORY https://github.com/ocornut/imgui.git
        GIT_TAG v1.91.9
    )
    FetchContent_MakeAvailable(imgui)

    add_library(imgui_lib STATIC
        ${imgui_SOURCE_DIR}/imgui.cpp
        ${imgui_SOURCE_DIR}/imgui_demo.cpp
        ${imgui_SOURCE_DIR}/imgui_draw.cpp
        ${imgui_SOURCE_DIR}/imgui_tables.cpp
        ${imgui_SOURCE_DIR}/imgui_widgets.cpp
        ${imgui_SOURCE_DIR}/backends/imgui_impl_sdl3.cpp
        ${imgui_SOURCE_DIR}/backends/imgui_impl_sdlrenderer3.cpp
    )
    target_include_directories(imgui_lib PUBLIC
        ${imgui_SOURCE_DIR}
        ${imgui_SOURCE_DIR}/backends
    )
    target_link_libraries(imgui_lib PUBLIC SDL3::SDL3)
    add_library(imgui::imgui ALIAS imgui_lib)
endif()

add_executable(ut3convgui WIN32
    src/main.cpp
    src/app.cpp
    src/process.cpp
)
target_link_libraries(ut3convgui PRIVATE SDL3::SDL3 imgui::imgui)
