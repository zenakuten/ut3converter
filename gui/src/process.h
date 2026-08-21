// Running ut3conv.py and streaming its output back into the log.
//
// SDL3's process API covers the whole platform difference -- pipes, working
// directory, environment, and killing the thing -- so there is no #ifdef in
// here. The child's stderr is folded into stdout so a traceback lands in the
// log in the order it happened rather than arriving in a lump at the end.
#pragma once

#include <SDL3/SDL.h>

#include <string>
#include <utility>
#include <vector>

class Runner {
public:
    Runner() = default;
    ~Runner();

    Runner(const Runner&) = delete;
    Runner& operator=(const Runner&) = delete;

    // Spawns argv in `cwd`. `env` entries are added to a copy of this
    // process's environment, which is how batch.py gets its two roots.
    // Returns false and logs the reason if the child will not start.
    bool Start(const std::vector<std::string>& argv,
               const std::string& cwd,
               const std::vector<std::pair<std::string, std::string>>& env = {});

    // Call once a frame: moves whatever the reader thread has collected into
    // the line buffer, and reaps the child once it has gone.
    void Poll();

    void Cancel();

    bool Running() const { return proc_ != nullptr; }
    // One-shot: true once, for the frame the child was reaped on. The caller
    // logs the exit line, and a caller polled every frame must not log it
    // again for the rest of the session.
    bool TakeFinished();
    int ExitCode() const { return exit_code_; }

    const std::vector<std::string>& Lines() const { return lines_; }
    void ClearLines();
    void AddLine(std::string text);

private:
    static int SDLCALL ReadThread(void* self);
    void Reap();

    SDL_Process* proc_ = nullptr;
    SDL_Thread* thread_ = nullptr;
    SDL_Mutex* mutex_ = nullptr;

    // Written by the reader thread, drained by Poll(), both under mutex_.
    std::string incoming_;
    bool eof_ = false;

    std::string partial_;               // a line the child has not finished
    std::vector<std::string> lines_;
    bool finished_ = false;
    int exit_code_ = 0;
};
