// The interface: three tabs over ut3conv.py and batch.py.
#pragma once

#include <SDL3/SDL.h>

#include <memory>
#include <string>
#include <vector>

#include "process.h"
#include "spec.h"

// One option's current value. Editing happens straight in `buf` so no ImGui
// std::string binding is needed; Text() reads it back out.
struct Value {
    const spec::Opt* opt = nullptr;
    std::vector<char> buf;
    bool flag = false;
    std::string def;              // the default, with <install-root> resolved

    std::string Text() const { return std::string(buf.data()); }
    void SetText(const std::string& text);
    bool Changed() const;
};

// A tab (or, for Inspect, one of the subcommands behind the picker): a
// subcommand's options, the command line they build, and the log of a run.
struct Panel {
    std::string title;
    std::string command;          // "t3d", "info", ...; empty for batch.py
    std::string script;           // "ut3conv.py" or "batch.py"
    const spec::Opt* options = nullptr;
    int count = 0;
    const spec::Section* sections = nullptr;
    int section_count = 0;

    std::vector<Value> values;
    Runner runner;
    std::string status;
    float log_height = 220.0f;
    bool drawn = false;            // first draw decides which sections open
};

class App {
public:
    bool Init(SDL_Window* window);
    void Frame();
    void Save();

private:
    void SetupPanel(Panel& panel, const char* title, const char* command,
                    const char* script, const spec::Opt* options, int count,
                    const spec::Section* sections = nullptr, int section_count = 0);

    void DrawSetup();
    void DrawPanel(Panel& panel);
    void DrawOptionRows(Panel& panel, int begin, int end);
    void DrawFooter(Panel& panel);
    void DrawLog(Panel& panel);

    std::vector<std::string> Argv(const Panel& panel) const;
    std::string CommandLine(const Panel& panel) const;
    bool Validate(const Panel& panel, std::string* problem) const;
    void Run(Panel& panel);
    void OpenBrowse(Value& value);

    std::string InstallRoot() const;
    void Load();

    SDL_Window* window_ = nullptr;

    Panel convert_;
    Panel batch_;
    // Held by pointer: the output reader thread captures a Runner*, so a
    // Panel must keep its address for as long as it lives.
    std::vector<std::unique_ptr<Panel>> inspect_;
    int inspect_index_ = 0;

    // Where the scripts and the interpreter are. Editable, because a Windows
    // install has neither at a path this can guess.
    std::vector<char> python_;
    std::vector<char> converter_;
    // batch.py takes these two from the environment; empty means leave its
    // own defaults alone.
    std::vector<char> editor_;
    std::vector<char> ut3_root_;

    bool setup_open_ = false;
    bool dirty_ = false;
    Uint64 last_save_ = 0;
};
