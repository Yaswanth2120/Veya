set(TARGET whisper-stream-stdin)
add_executable(${TARGET} stream_stdin.cpp)

include(DefaultTargetOptions)

target_link_libraries(${TARGET} PRIVATE common whisper ${CMAKE_THREAD_LIBS_INIT})

install(TARGETS ${TARGET} RUNTIME)
