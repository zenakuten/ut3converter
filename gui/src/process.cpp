#include "process.h"

#include <SDL3/SDL_process.h>

namespace {

// A run that produces a lot of output (batch.py over 55 maps) should not be
// able to grow the log without bound.
constexpr size_t kMaxLines = 20000;

}  // namespace

Runner::~Runner() {
    Cancel();
    if (thread_ != nullptr) {
        SDL_WaitThread(thread_, nullptr);
        thread_ = nullptr;
    }
    if (proc_ != nullptr) {
        SDL_DestroyProcess(proc_);
        proc_ = nullptr;
    }
    if (mutex_ != nullptr) {
        SDL_DestroyMutex(mutex_);
        mutex_ = nullptr;
    }
}

bool Runner::TakeFinished() {
    if (!finished_) return false;
    finished_ = false;
    return true;
}

void Runner::ClearLines() {
    lines_.clear();
    partial_.clear();
}

void Runner::AddLine(std::string text) {
    lines_.push_back(std::move(text));
    if (lines_.size() > kMaxLines) {
        lines_.erase(lines_.begin(), lines_.begin() + (lines_.size() - kMaxLines));
    }
}

bool Runner::Start(const std::vector<std::string>& argv,
                   const std::string& cwd,
                   const std::vector<std::pair<std::string, std::string>>& env) {
    if (Running()) return false;

    std::vector<const char*> args;
    args.reserve(argv.size() + 1);
    for (const std::string& arg : argv) args.push_back(arg.c_str());
    args.push_back(nullptr);

    SDL_Environment* environment = nullptr;
    if (!env.empty()) {
        environment = SDL_CreateEnvironment(true);   // inherit, then override
        if (environment != nullptr) {
            for (const auto& pair : env) {
                SDL_SetEnvironmentVariable(environment, pair.first.c_str(),
                                           pair.second.c_str(), true);
            }
        }
    }

    SDL_PropertiesID props = SDL_CreateProperties();
    SDL_SetPointerProperty(props, SDL_PROP_PROCESS_CREATE_ARGS_POINTER,
                           (void*)args.data());
    if (!cwd.empty()) {
        SDL_SetStringProperty(props, SDL_PROP_PROCESS_CREATE_WORKING_DIRECTORY_STRING,
                              cwd.c_str());
    }
    if (environment != nullptr) {
        SDL_SetPointerProperty(props, SDL_PROP_PROCESS_CREATE_ENVIRONMENT_POINTER,
                               environment);
    }
    // The child never reads stdin; leaving it inherited would let it block on
    // a terminal this app may not have.
    SDL_SetNumberProperty(props, SDL_PROP_PROCESS_CREATE_STDIN_NUMBER,
                          SDL_PROCESS_STDIO_NULL);
    SDL_SetNumberProperty(props, SDL_PROP_PROCESS_CREATE_STDOUT_NUMBER,
                          SDL_PROCESS_STDIO_APP);
    SDL_SetBooleanProperty(props, SDL_PROP_PROCESS_CREATE_STDERR_TO_STDOUT_BOOLEAN,
                           true);

    proc_ = SDL_CreateProcessWithProperties(props);
    SDL_DestroyProperties(props);
    if (environment != nullptr) SDL_DestroyEnvironment(environment);

    if (proc_ == nullptr) {
        AddLine(std::string("could not start it: ") + SDL_GetError());
        return false;
    }

    if (mutex_ == nullptr) mutex_ = SDL_CreateMutex();
    incoming_.clear();
    eof_ = false;
    finished_ = false;
    exit_code_ = 0;

    thread_ = SDL_CreateThread(ReadThread, "ut3conv-output", this);
    if (thread_ == nullptr) {
        AddLine(std::string("could not read its output: ") + SDL_GetError());
        SDL_KillProcess(proc_, true);
        SDL_DestroyProcess(proc_);
        proc_ = nullptr;
        return false;
    }
    return true;
}

// Blocks on the child's pipe so the interface does not have to. Everything it
// reads goes into incoming_ for Poll() to pick up on the main thread.
int SDLCALL Runner::ReadThread(void* self) {
    Runner* runner = static_cast<Runner*>(self);
    SDL_IOStream* out = SDL_GetProcessOutput(runner->proc_);
    if (out == nullptr) {
        SDL_LockMutex(runner->mutex_);
        runner->eof_ = true;
        SDL_UnlockMutex(runner->mutex_);
        return 0;
    }

    char buffer[4096];
    for (;;) {
        size_t got = SDL_ReadIO(out, buffer, sizeof(buffer));
        if (got > 0) {
            SDL_LockMutex(runner->mutex_);
            runner->incoming_.append(buffer, got);
            SDL_UnlockMutex(runner->mutex_);
            continue;
        }
        SDL_IOStatus status = SDL_GetIOStatus(out);
        if (status == SDL_IO_STATUS_NOT_READY) {
            SDL_Delay(5);
            continue;
        }
        break;                                   // EOF, or the pipe broke
    }

    SDL_LockMutex(runner->mutex_);
    runner->eof_ = true;
    SDL_UnlockMutex(runner->mutex_);
    return 0;
}

void Runner::Poll() {
    if (proc_ == nullptr) return;

    std::string chunk;
    bool eof = false;
    SDL_LockMutex(mutex_);
    chunk.swap(incoming_);
    eof = eof_;
    SDL_UnlockMutex(mutex_);

    for (char ch : chunk) {
        if (ch == '\n') {
            AddLine(partial_);
            partial_.clear();
        } else if (ch != '\r') {                 // the child may emit CRLF
            partial_.push_back(ch);
        }
    }

    if (eof) Reap();
}

void Runner::Reap() {
    if (!partial_.empty()) {                     // a last line with no newline
        AddLine(partial_);
        partial_.clear();
    }
    if (thread_ != nullptr) {
        SDL_WaitThread(thread_, nullptr);
        thread_ = nullptr;
    }
    SDL_WaitProcess(proc_, true, &exit_code_);
    SDL_DestroyProcess(proc_);
    proc_ = nullptr;
    finished_ = true;
}

void Runner::Cancel() {
    if (proc_ == nullptr) return;
    // SIGTERM rather than SIGKILL: a half-written package is worse than a
    // conversion that gets to unwind.
    SDL_KillProcess(proc_, false);
}
