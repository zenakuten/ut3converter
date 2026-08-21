# Cross-compiling the Windows .exe from Linux, so a Windows build does not
# mean booting Windows and opening Visual Studio.
#
#   pacman -S mingw-w64-toolchain
#   yay -S mingw-w64-sdl3
#   cmake -B build-win -DCMAKE_BUILD_TYPE=Release \
#         -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-mingw.cmake
#   cmake --build build-win
#
# Dear ImGui still comes from FetchContent and is built by the same
# cross-compiler, so only SDL3 has to exist as a Windows package.

set(CMAKE_SYSTEM_NAME Windows)
set(CMAKE_SYSTEM_PROCESSOR x86_64)

set(TARGET_TRIPLE x86_64-w64-mingw32)

set(CMAKE_C_COMPILER   ${TARGET_TRIPLE}-gcc)
set(CMAKE_CXX_COMPILER ${TARGET_TRIPLE}-g++)
set(CMAKE_RC_COMPILER  ${TARGET_TRIPLE}-windres)

# Where the Windows SDL3 lives. Look for libraries and headers only under the
# cross root, or CMake will happily hand the build a host Linux SDL3.
set(CMAKE_FIND_ROOT_PATH /usr/${TARGET_TRIPLE})
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# Fold the GCC runtime into the executable. Without this the .exe needs
# libgcc_s_seh-1.dll, libstdc++-6.dll and libwinpthread-1.dll beside it, which
# is a poor thing to hand someone alongside a single tool.
set(CMAKE_EXE_LINKER_FLAGS_INIT
    "-static-libgcc -static-libstdc++ -Wl,-Bstatic,--whole-archive -lwinpthread -Wl,--no-whole-archive")
