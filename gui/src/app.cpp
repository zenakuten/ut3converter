#include "app.h"

#include <SDL3/SDL_dialog.h>
#include <SDL3/SDL_filesystem.h>

#include <imgui.h>

#include <cstdio>
#include <cstring>
#include <string>

#include "spec_generated.h"

namespace {

// Paths get room to be long; a package name does not need it.
constexpr size_t kPathBuffer = 1024;
constexpr size_t kShortBuffer = 256;

// The token gen_spec.py writes instead of baking one machine's install root
// into a committed header.
const char kInstallRootToken[] = "<install-root>";

#ifdef _WIN32
constexpr char kSep = '\\';
const char kDefaultPython[] = "py";
#else
constexpr char kSep = '/';
const char kDefaultPython[] = "python3";
#endif

std::string Join(const std::string& dir, const std::string& leaf) {
    if (dir.empty()) return leaf;
    if (dir.back() == '/' || dir.back() == '\\') return dir + leaf;
    return dir + kSep + leaf;
}

std::string Parent(const std::string& path) {
    size_t cut = path.find_last_of("/\\");
    if (cut == std::string::npos) return std::string();
    if (cut == 0) return path.substr(0, 1);
    return path.substr(0, cut);
}

bool Exists(const std::string& path) {
    SDL_PathInfo info;
    return SDL_GetPathInfo(path.c_str(), &info);
}

// The build tree puts the executable at gui/build/, an install may put it
// anywhere, so walk up looking for the script rather than assuming a depth.
std::string FindConverter() {
    const char* base = SDL_GetBasePath();
    std::string dir = base != nullptr ? base : ".";
    for (int up = 0; up < 5; ++up) {
        if (Exists(Join(dir, "ut3conv.py"))) return dir;
        std::string parent = Parent(dir);
        if (parent.empty() || parent == dir) break;
        dir = parent;
    }
    return std::string();
}

void SetBuffer(std::vector<char>& buf, const std::string& text, size_t size) {
    buf.assign(size, '\0');
    size_t n = text.size() < size - 1 ? text.size() : size - 1;
    std::memcpy(buf.data(), text.data(), n);
}

// Shell-ish quoting, for the previewed line only: the child is spawned from
// the argument vector, so this never has to be exact -- just pasteable.
std::string Quote(const std::string& part) {
    bool plain = !part.empty();
    for (char ch : part) {
        if (std::strchr(" \t\"'\\&|<>$()", ch) != nullptr) { plain = false; break; }
    }
    if (plain) return part;
#ifdef _WIN32
    return "\"" + part + "\"";
#else
    std::string out = "'";
    for (char ch : part) {
        if (ch == '\'') out += "'\\''";
        else out += ch;
    }
    return out + "'";
#endif
}

void Tooltip(const char* text) {
    if (text == nullptr || text[0] == '\0') return;
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal |
                             ImGuiHoveredFlags_AllowWhenDisabled)) {
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 32.0f);
        ImGui::TextUnformatted(text);
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
}

bool IsNumber(const std::string& text, bool whole) {
    if (text.empty()) return false;
    char* end = nullptr;
    if (whole) {
        long value = std::strtol(text.c_str(), &end, 10);
        (void)value;
    } else {
        double value = std::strtod(text.c_str(), &end);
        (void)value;
    }
    return end != nullptr && *end == '\0';
}

// A browse dialog is asynchronous, so the field it fills has to outlive the
// call. Every Value lives in a Panel that outlives the frame, so the pointer
// is safe; this just carries it through SDL's userdata.
void SDLCALL BrowsePicked(void* userdata, const char* const* files, int) {
    Value* value = static_cast<Value*>(userdata);
    if (files == nullptr || files[0] == nullptr) return;   // cancelled
    value->SetText(files[0]);
}

const SDL_DialogFileFilter kPackageFilters[] = {
    { "UT3 packages", "ut3;upk" },
    { "All files", "*" },
};
const SDL_DialogFileFilter kT3dFilters[] = {
    { "UT2004 t3d", "t3d" },
    { "All files", "*" },
};

}  // namespace

void Value::SetText(const std::string& text) {
    size_t size = buf.size();
    if (size == 0) size = kPathBuffer;
    SetBuffer(buf, text, size);
}

bool Value::Changed() const {
    if (opt->kind == spec::Kind::Flag) return flag != (std::strcmp(opt->def, "true") == 0);
    return Text() != def;
}

// ---------------------------------------------------------------- lifecycle

std::string App::InstallRoot() const {
    return Parent(std::string(converter_.data()));
}

void App::SetupPanel(Panel& panel, const char* title, const char* command,
                     const char* script, const spec::Opt* options, int count,
                     const spec::Section* sections, int section_count) {
    panel.title = title;
    panel.command = command;
    panel.script = script;
    panel.options = options;
    panel.count = count;
    panel.sections = sections;
    panel.section_count = section_count;

    panel.values.resize(count);
    for (int i = 0; i < count; ++i) {
        Value& value = panel.values[i];
        value.opt = &options[i];

        std::string def = value.opt->def;
        if (def == kInstallRootToken) def = InstallRoot();
        value.def = def;

        if (value.opt->kind == spec::Kind::Flag) {
            value.flag = def == "true";
            value.buf.assign(1, '\0');
        } else {
            size_t size = value.opt->browse == spec::Browse::None ? kShortBuffer
                                                                  : kPathBuffer;
            SetBuffer(value.buf, def, size);
        }
    }
}

bool App::Init(SDL_Window* window) {
    window_ = window;

    SetBuffer(python_, kDefaultPython, kShortBuffer);
    SetBuffer(converter_, FindConverter(), kPathBuffer);
    SetBuffer(editor_, "", kPathBuffer);
    SetBuffer(ut3_root_, "", kPathBuffer);

    SetupPanel(convert_, "Convert", "t3d", "ut3conv.py",
               spec::kT3dOptions, spec::kT3dOptionCount,
               spec::kT3dSections, spec::kT3dSectionCount);
    SetupPanel(repoint_, "Repoint", "", "tools/repoint_package.py",
               spec::kRepointOptions, spec::kRepointOptionCount);
    SetupPanel(batch_, "Batch", "", "batch.py",
               spec::kBatchOptions, spec::kBatchOptionCount);

    for (int i = 0; i < spec::kInspectCommandCount; ++i) {
        const spec::Command& command = spec::kInspectCommands[i];
        inspect_.push_back(std::make_unique<Panel>());
        SetupPanel(*inspect_.back(), command.name, command.name, "ut3conv.py",
                   command.options, command.count);
    }

    Load();
    // If the scripts are not where the executable could find them, say so at
    // once rather than failing on the first Run.
    setup_open_ = converter_.data()[0] == '\0';
    return true;
}

// ------------------------------------------------------------- command line

std::vector<std::string> App::Argv(const Panel& panel) const {
    std::vector<std::string> argv;
    argv.push_back(std::string(python_.data()));
    argv.push_back("-u");            // unbuffered, or the log arrives in lumps
    argv.push_back(Join(std::string(converter_.data()), panel.script));
    if (!panel.command.empty()) argv.push_back(panel.command);

    // Positionals in the order argparse declares them, then the flags.
    for (const Value& value : panel.values) {
        if (value.opt->flag[0] != '\0') continue;
        std::string text = value.Text();
        if (text.empty()) continue;
        if (!value.opt->variadic) {
            argv.push_back(text);
            continue;
        }
        // nargs="*": one field holding several paths. repoint_package.py wants
        // a map and its package together, and a single argv entry with a space
        // in it would be read as one filename.
        size_t at = 0;
        while (at < text.size()) {
            size_t start = text.find_first_not_of(" \t", at);
            if (start == std::string::npos) break;
            size_t stop = text.find_first_of(" \t", start);
            if (stop == std::string::npos) stop = text.size();
            argv.push_back(text.substr(start, stop - start));
            at = stop;
        }
    }
    for (const Value& value : panel.values) {
        if (value.opt->flag[0] == '\0') continue;
        if (value.opt->kind == spec::Kind::Flag) {
            if (value.Changed()) argv.push_back(value.opt->flag);
            continue;
        }
        // Only what was actually changed: restating a default would make the
        // previewed line useless for pasting.
        std::string text = value.Text();
        if (text.empty() || !value.Changed()) continue;
        argv.push_back(value.opt->flag);
        argv.push_back(text);
    }
    return argv;
}

std::string App::CommandLine(const Panel& panel) const {
    std::vector<std::string> argv = Argv(panel);
    std::string line;
    if (&panel == &batch_) {
        // These reach batch.py through the environment, so show them the way
        // a shell would have to pass them.
        std::string editor(editor_.data());
        std::string root(ut3_root_.data());
#ifdef _WIN32
        if (!editor.empty()) line += "set UT3CONV_EDITOR=" + editor + " && ";
        if (!root.empty()) line += "set UT3CONV_UT3_ROOT=" + root + " && ";
#else
        if (!editor.empty()) line += "UT3CONV_EDITOR=" + Quote(editor) + " ";
        if (!root.empty()) line += "UT3CONV_UT3_ROOT=" + Quote(root) + " ";
#endif
    }
    for (size_t i = 0; i < argv.size(); ++i) {
        if (i > 0) line += ' ';
        line += Quote(argv[i]);
    }
    return line;
}

bool App::Validate(const Panel& panel, std::string* problem) const {
    if (converter_.data()[0] == '\0') {
        *problem = "Point Setup at the folder holding ut3conv.py first.";
        return false;
    }
    if (!Exists(Join(std::string(converter_.data()), panel.script))) {
        *problem = "No " + panel.script + " in " + std::string(converter_.data());
        return false;
    }
    for (const Value& value : panel.values) {
        std::string text = value.Text();
        if (value.opt->required && text.empty()) {
            *problem = std::string(value.opt->label) + " is required.";
            return false;
        }
        if (text.empty()) continue;
        if (value.opt->kind == spec::Kind::Int && !IsNumber(text, true)) {
            *problem = std::string(value.opt->label) + " wants a whole number.";
            return false;
        }
        if (value.opt->kind == spec::Kind::Float && !IsNumber(text, false)) {
            *problem = std::string(value.opt->label) + " wants a number.";
            return false;
        }
        if (value.opt->browse == spec::Browse::OpenPackage && !Exists(text)) {
            *problem = "No such package: " + text;
            return false;
        }
    }
    return true;
}

void App::Run(Panel& panel) {
    std::string problem;
    if (!Validate(panel, &problem)) {
        panel.status = problem;
        panel.runner.AddLine("** " + problem);
        return;
    }
    std::vector<std::pair<std::string, std::string>> env;
    if (&panel == &batch_) {
        if (editor_.data()[0] != '\0')
            env.emplace_back("UT3CONV_EDITOR", std::string(editor_.data()));
        if (ut3_root_.data()[0] != '\0')
            env.emplace_back("UT3CONV_UT3_ROOT", std::string(ut3_root_.data()));
    }
    panel.runner.AddLine("$ " + CommandLine(panel));
    if (panel.runner.Start(Argv(panel), std::string(converter_.data()), env)) {
        panel.status = "running...";
    } else {
        panel.status = "could not start";
    }
}

void App::OpenBrowse(Value& value) {
    std::string current = value.Text();
    std::string start = Exists(current) ? current : Parent(current);
    const char* location = start.empty() ? nullptr : start.c_str();
    switch (value.opt->browse) {
        case spec::Browse::OpenPackage:
            SDL_ShowOpenFileDialog(BrowsePicked, &value, window_, kPackageFilters,
                                   2, location, false);
            break;
        case spec::Browse::SavePath:
            SDL_ShowSaveFileDialog(BrowsePicked, &value, window_, kT3dFilters,
                                   2, location);
            break;
        case spec::Browse::Folder:
            SDL_ShowOpenFolderDialog(BrowsePicked, &value, window_, location, false);
            break;
        default:
            break;
    }
}

// ------------------------------------------------------------------ drawing

void App::DrawOptionRows(Panel& panel, int begin, int end) {
    // Checkboxes pack three to a row; everything else gets a labelled row of
    // its own, so a section of fifteen omit-flags stays one screenful.
    bool any_value = false;
    for (int i = begin; i < end; ++i) {
        if (panel.values[i].opt->kind != spec::Kind::Flag) { any_value = true; break; }
    }

    if (any_value && ImGui::BeginTable("values", 3, ImGuiTableFlags_SizingFixedFit)) {
        ImGui::TableSetupColumn("label", ImGuiTableColumnFlags_WidthFixed, 200.0f);
        ImGui::TableSetupColumn("value", ImGuiTableColumnFlags_WidthStretch);
        ImGui::TableSetupColumn("browse", ImGuiTableColumnFlags_WidthFixed, 90.0f);
        for (int i = begin; i < end; ++i) {
            Value& value = panel.values[i];
            if (value.opt->kind == spec::Kind::Flag) continue;
            ImGui::TableNextRow();
            ImGui::PushID(i);

            ImGui::TableSetColumnIndex(0);
            if (value.opt->required) {
                ImGui::TextUnformatted(value.opt->label);
                ImGui::SameLine();
                ImGui::TextDisabled("*");
            } else if (value.Changed()) {
                // A changed value is the one thing worth spotting at a glance:
                // it is what will end up on the command line.
                ImGui::TextColored(ImVec4(0.45f, 0.80f, 1.00f, 1.00f), "%s",
                                   value.opt->label);
            } else {
                ImGui::TextUnformatted(value.opt->label);
            }
            Tooltip(value.opt->help);

            ImGui::TableSetColumnIndex(1);
            ImGui::SetNextItemWidth(-FLT_MIN);
            if (value.opt->kind == spec::Kind::Choice) {
                std::string current = value.Text();
                if (ImGui::BeginCombo("##v", current.c_str())) {
                    for (int c = 0; c < 8 && value.opt->choices[c] != nullptr; ++c) {
                        bool selected = current == value.opt->choices[c];
                        if (ImGui::Selectable(value.opt->choices[c], selected)) {
                            value.SetText(value.opt->choices[c]);
                            dirty_ = true;
                        }
                    }
                    ImGui::EndCombo();
                }
            } else {
                ImGuiInputTextFlags flags = 0;
                if (value.opt->kind == spec::Kind::Int ||
                    value.opt->kind == spec::Kind::Float) {
                    flags = ImGuiInputTextFlags_CharsDecimal;
                }
                if (ImGui::InputText("##v", value.buf.data(), value.buf.size(), flags)) {
                    dirty_ = true;
                }
            }
            Tooltip(value.opt->help);

            ImGui::TableSetColumnIndex(2);
            if (value.opt->browse != spec::Browse::None) {
                if (ImGui::Button("Browse...")) OpenBrowse(value);
            }
            ImGui::PopID();
        }
        ImGui::EndTable();
    }

    bool any_flag = false;
    for (int i = begin; i < end; ++i) {
        if (panel.values[i].opt->kind == spec::Kind::Flag) { any_flag = true; break; }
    }
    if (!any_flag) return;

    if (ImGui::BeginTable("flags", 3, ImGuiTableFlags_SizingStretchSame)) {
        for (int i = begin; i < end; ++i) {
            Value& value = panel.values[i];
            if (value.opt->kind != spec::Kind::Flag) continue;
            ImGui::TableNextColumn();
            ImGui::PushID(i);
            if (ImGui::Checkbox(value.opt->label, &value.flag)) dirty_ = true;
            Tooltip(value.opt->help);
            ImGui::PopID();
        }
        ImGui::EndTable();
    }
}

void App::DrawFooter(Panel& panel) {
    ImGui::Separator();

    std::string line = CommandLine(panel);
    ImGui::TextUnformatted("Command");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(-90.0f);
    // Read-only rather than disabled, so the text can still be selected.
    ImGui::InputText("##cmd", line.data(), line.size() + 1,
                     ImGuiInputTextFlags_ReadOnly);
    ImGui::SameLine();
    if (ImGui::Button("Copy##cmd")) {
        SDL_SetClipboardText(line.c_str());
        panel.status = "command copied";
    }

    bool running = panel.runner.Running();
    ImGui::BeginDisabled(running);
    if (ImGui::Button("Run")) Run(panel);
    ImGui::EndDisabled();

    ImGui::SameLine();
    ImGui::BeginDisabled(!running);
    if (ImGui::Button("Cancel")) {
        panel.runner.Cancel();
        panel.status = "cancelling...";
    }
    ImGui::EndDisabled();

    ImGui::SameLine();
    if (ImGui::Button("Reset to defaults")) {
        for (Value& value : panel.values) {
            if (value.opt->kind == spec::Kind::Flag) {
                value.flag = std::strcmp(value.opt->def, "true") == 0;
            } else {
                value.SetText(value.def);
            }
        }
        dirty_ = true;
    }
    ImGui::SameLine();
    if (ImGui::Button("Clear log")) panel.runner.ClearLines();

    if (!panel.status.empty()) {
        ImGui::SameLine();
        ImGui::TextDisabled("%s", panel.status.c_str());
    }
}

void App::DrawLog(Panel& panel) {
    ImGui::Separator();
    ImGui::BeginChild("log", ImVec2(0, 0), ImGuiChildFlags_Borders,
                      ImGuiWindowFlags_HorizontalScrollbar);
    const std::vector<std::string>& lines = panel.runner.Lines();
    // Only the visible lines are laid out; batch.py over 55 maps is thousands.
    ImGuiListClipper clipper;
    clipper.Begin((int)lines.size());
    while (clipper.Step()) {
        for (int i = clipper.DisplayStart; i < clipper.DisplayEnd; ++i) {
            const std::string& text = lines[i];
            if (!text.empty() && text[0] == '$') {
                ImGui::TextColored(ImVec4(0.55f, 0.75f, 0.55f, 1.0f), "%s", text.c_str());
            } else if (text.rfind("** ", 0) == 0) {
                ImGui::TextColored(ImVec4(1.0f, 0.55f, 0.45f, 1.0f), "%s", text.c_str());
            } else {
                ImGui::TextUnformatted(text.c_str());
            }
        }
    }
    // Stay pinned to the bottom only while already there, so scrolling back
    // through a finished run is not yanked away by the next line.
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 1.0f) {
        ImGui::SetScrollHereY(1.0f);
    }
    ImGui::EndChild();
}

void App::DrawPanel(Panel& panel) {
    float footer = ImGui::GetFrameHeightWithSpacing() * 2.4f;
    float options_height = ImGui::GetContentRegionAvail().y - panel.log_height - footer;
    if (options_height < 80.0f) options_height = 80.0f;

    ImGui::BeginChild("options", ImVec2(0, options_height), ImGuiChildFlags_Borders);
    if (panel.sections != nullptr) {
        for (int s = 0; s < panel.section_count; ++s) {
            const spec::Section& section = panel.sections[s];

            // A collapsed section must still say that something inside it is
            // set, or an option restored from last time silently changes the
            // conversion from behind a shut header.
            int changed = 0;
            for (int i = section.begin; i < section.end; ++i) {
                if (panel.values[i].Changed()) ++changed;
            }

            // Required is always in view, and so is anything already carrying
            // a non-default value; the rest start shut.
            if (!panel.drawn) {
                ImGui::SetNextItemOpen(s == 0 || changed > 0);
            }
            char title[128];
            if (changed > 0) {
                SDL_snprintf(title, sizeof(title), "%s  (%d set)###s%d",
                             section.title, changed, s);
            } else {
                SDL_snprintf(title, sizeof(title), "%s###s%d", section.title, s);
            }
            if (ImGui::CollapsingHeader(title)) {
                ImGui::PushID(s);
                DrawOptionRows(panel, section.begin, section.end);
                ImGui::PopID();
            }
        }
    } else {
        DrawOptionRows(panel, 0, panel.count);
    }
    panel.drawn = true;
    ImGui::EndChild();

    DrawFooter(panel);
    DrawLog(panel);
}

void App::DrawSetup() {
    if (setup_open_) {
        // Latched through storage rather than ImGuiTreeNodeFlags_DefaultOpen:
        // that flag is re-evaluated every frame, so dropping it on the next
        // one snapped the header shut again after a single frame.
        ImGui::SetNextItemOpen(true, ImGuiCond_Once);
        setup_open_ = false;
    }
    bool open = ImGui::CollapsingHeader("Setup");

    // Said outside the header, because it is exactly when the converter is
    // missing that this must not be hidden behind a collapsed section: the
    // command line still reads plausibly, and only Run says otherwise.
    if (converter_.data()[0] == '\0') {
        ImGui::TextColored(ImVec4(1.0f, 0.55f, 0.45f, 1.0f),
                           "ut3conv.py was not found near this executable -- "
                           "set the converter folder under Setup.");
    }
    if (!open) return;

    if (ImGui::BeginTable("setup", 2, ImGuiTableFlags_SizingFixedFit)) {
        ImGui::TableSetupColumn("label", ImGuiTableColumnFlags_WidthFixed, 200.0f);
        ImGui::TableSetupColumn("value", ImGuiTableColumnFlags_WidthStretch);

        ImGui::TableNextRow();
        ImGui::TableSetColumnIndex(0);
        ImGui::TextUnformatted("python");
        Tooltip("The interpreter to run the scripts with. python3 on Linux, "
                "py or python on Windows.");
        ImGui::TableSetColumnIndex(1);
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputText("##python", python_.data(), python_.size())) dirty_ = true;

        ImGui::TableNextRow();
        ImGui::TableSetColumnIndex(0);
        if (converter_.data()[0] == '\0') {
            ImGui::TextColored(ImVec4(1.0f, 0.55f, 0.45f, 1.0f), "converter folder");
        } else {
            ImGui::TextUnformatted("converter folder");
        }
        Tooltip("The folder holding ut3conv.py and batch.py. Found by walking "
                "up from this executable; set it by hand if it moved.");
        ImGui::TableSetColumnIndex(1);
        ImGui::SetNextItemWidth(-FLT_MIN);
        if (ImGui::InputText("##converter", converter_.data(), converter_.size())) {
            dirty_ = true;
        }
        ImGui::EndTable();
    }

    ImGui::Spacing();
}

void App::Frame() {
    convert_.runner.Poll();
    batch_.runner.Poll();
    for (auto& panel : inspect_) panel->runner.Poll();

    auto settle = [](Panel& panel) {
        if (panel.runner.TakeFinished()) {
            int code = panel.runner.ExitCode();
            panel.runner.AddLine(code == 0 ? "-- done --"
                                           : "** exit " + std::to_string(code));
            panel.status = code == 0 ? "done" : "failed (exit " +
                                                std::to_string(code) + ")";
        }
    };
    settle(convert_);
    settle(batch_);
    for (auto& panel : inspect_) settle(*panel);

    const ImGuiViewport* viewport = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(viewport->WorkPos);
    ImGui::SetNextWindowSize(viewport->WorkSize);
    ImGui::Begin("ut3conv", nullptr,
                 ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoBringToFrontOnFocus);

    DrawSetup();

    if (ImGui::BeginTabBar("tabs")) {
        if (ImGui::BeginTabItem("Convert")) {
            ImGui::TextDisabled(
                "One UT3 map to a UT2004 .t3d plus a buildable asset package. "
                "Hover any option for what it does.");
            DrawPanel(convert_);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Batch")) {
            ImGui::TextDisabled(
                "Every UT3 map, one package per ucc make. Start with list to "
                "see what would run.");
            if (ImGui::BeginTable("roots", 2, ImGuiTableFlags_SizingFixedFit)) {
                ImGui::TableSetupColumn("l", ImGuiTableColumnFlags_WidthFixed, 200.0f);
                ImGui::TableSetupColumn("v", ImGuiTableColumnFlags_WidthStretch);
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImGui::TextUnformatted("editor");
                Tooltip("The UT2004 install results are copied into: Textures/ "
                        "for the packages, Converted/ for the t3d files. Empty "
                        "leaves batch.py's own default alone.");
                ImGui::TableSetColumnIndex(1);
                ImGui::SetNextItemWidth(-FLT_MIN);
                if (ImGui::InputText("##editor", editor_.data(), editor_.size()))
                    dirty_ = true;
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImGui::TextUnformatted("ut3 root");
                Tooltip("UT3's CookedPC folder, the one holding Maps/. Empty "
                        "leaves batch.py's own default alone.");
                ImGui::TableSetColumnIndex(1);
                ImGui::SetNextItemWidth(-FLT_MIN);
                if (ImGui::InputText("##ut3root", ut3_root_.data(), ut3_root_.size()))
                    dirty_ = true;
                ImGui::EndTable();
            }
            DrawPanel(batch_);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Inspect")) {
            ImGui::TextDisabled(
                "Read a UT3 package without converting it -- how most "
                "conversion bugs get found.");
            ImGui::SetNextItemWidth(160.0f);
            if (ImGui::BeginCombo("command", inspect_[inspect_index_]->title.c_str())) {
                for (int i = 0; i < (int)inspect_.size(); ++i) {
                    if (ImGui::Selectable(inspect_[i]->title.c_str(),
                                          i == inspect_index_)) {
                        inspect_index_ = i;
                        dirty_ = true;
                    }
                }
                ImGui::EndCombo();
            }
            DrawPanel(*inspect_[inspect_index_]);
            ImGui::EndTabItem();
        }
        if (ImGui::BeginTabItem("Repoint")) {
            ImGui::TextDisabled(
                "Rename the texture package a built map refers to, for shipping "
                "an edited copy under a name of your own.");
            ImGui::TextDisabled(
                "Both files: the map imports a class of that name as well as "
                "the package, so renaming only the .ut2 leaves it unloadable. "
                "The new name must be exactly as long as the old one.");
            ImGui::TextDisabled(
                "Put the .ut2 and the .utx in files, separated by a space. "
                "Use list on its own first to see what a map imports.");
            DrawPanel(repoint_);
            ImGui::EndTabItem();
        }
        ImGui::EndTabBar();
    }
    ImGui::End();

    // Settings are a convenience, so write them on a lull rather than on
    // every keystroke.
    Uint64 now = SDL_GetTicks();
    if (dirty_ && now - last_save_ > 1500) {
        Save();
        dirty_ = false;
        last_save_ = now;
    }
}

// ----------------------------------------------------------------- settings

namespace {

// Flat key=value beside the user's other preferences. Values are single-line
// paths and numbers, so nothing needs escaping.
std::string SettingsPath() {
    char* pref = SDL_GetPrefPath("zenakuten", "ut3convgui");
    if (pref == nullptr) return std::string();
    std::string path = std::string(pref) + "settings.ini";
    SDL_free(pref);
    return path;
}

void WriteLine(std::string& out, const std::string& key, const std::string& value) {
    if (value.empty()) return;
    out += key;
    out += '=';
    out += value;
    out += '\n';
}

}  // namespace

void App::Save() {
    std::string path = SettingsPath();
    if (path.empty()) return;

    std::string out;
    out += "# ut3conv gui, last used values\n";
    WriteLine(out, "python", std::string(python_.data()));
    WriteLine(out, "converter", std::string(converter_.data()));
    WriteLine(out, "editor", std::string(editor_.data()));
    WriteLine(out, "ut3_root", std::string(ut3_root_.data()));
    WriteLine(out, "inspect", inspect_[inspect_index_]->title);

    auto save_panel = [&out](const Panel& panel, const std::string& prefix) {
        for (const Value& value : panel.values) {
            if (!value.Changed() && value.Text().empty()) continue;
            if (value.opt->kind == spec::Kind::Flag) {
                if (value.Changed()) WriteLine(out, prefix + value.opt->dest,
                                               value.flag ? "true" : "false");
            } else {
                WriteLine(out, prefix + value.opt->dest, value.Text());
            }
        }
    };
    save_panel(convert_, "convert.");
    save_panel(batch_, "batch.");
    for (const auto& panel : inspect_) save_panel(*panel, panel->title + ".");

    SDL_IOStream* io = SDL_IOFromFile(path.c_str(), "w");
    if (io == nullptr) return;
    SDL_WriteIO(io, out.data(), out.size());
    SDL_CloseIO(io);
}

void App::Load() {
    std::string path = SettingsPath();
    if (path.empty()) return;
    size_t size = 0;
    void* data = SDL_LoadFile(path.c_str(), &size);
    if (data == nullptr) return;
    std::string text(static_cast<char*>(data), size);
    SDL_free(data);

    auto apply = [this](const std::string& key, const std::string& saved) {
        if (key == "python") { SetBuffer(python_, saved, kShortBuffer); return; }
        if (key == "converter") { SetBuffer(converter_, saved, kPathBuffer); return; }
        if (key == "editor") { SetBuffer(editor_, saved, kPathBuffer); return; }
        if (key == "ut3_root") { SetBuffer(ut3_root_, saved, kPathBuffer); return; }
        if (key == "inspect") {
            for (int i = 0; i < (int)inspect_.size(); ++i) {
                if (inspect_[i]->title == saved) inspect_index_ = i;
            }
            return;
        }
        size_t dot = key.find('.');
        if (dot == std::string::npos) return;
        std::string which = key.substr(0, dot);
        std::string dest = key.substr(dot + 1);

        Panel* panel = nullptr;
        if (which == "convert") panel = &convert_;
        else if (which == "batch") panel = &batch_;
        else {
            for (auto& candidate : inspect_) {
                if (candidate->title == which) { panel = candidate.get(); break; }
            }
        }
        if (panel == nullptr) return;
        for (Value& value : panel->values) {
            if (dest != value.opt->dest) continue;
            if (value.opt->kind == spec::Kind::Flag) value.flag = saved == "true";
            else value.SetText(saved);
            return;
        }
    };

    size_t start = 0;
    while (start < text.size()) {
        size_t end = text.find('\n', start);
        if (end == std::string::npos) end = text.size();
        std::string line = text.substr(start, end - start);
        start = end + 1;
        if (line.empty() || line[0] == '#') continue;
        if (!line.empty() && line.back() == '\r') line.pop_back();
        size_t eq = line.find('=');
        if (eq == std::string::npos) continue;
        apply(line.substr(0, eq), line.substr(eq + 1));
    }

    // A converter folder saved from another machine is worse than none.
    if (converter_.data()[0] != '\0' &&
        !Exists(Join(std::string(converter_.data()), "ut3conv.py"))) {
        SetBuffer(converter_, FindConverter(), kPathBuffer);
    }
    // Defaults that resolve against the install root have to be recomputed
    // once the converter folder is known.
    for (Value& value : convert_.values) {
        if (std::string(value.opt->def) == kInstallRootToken) value.def = InstallRoot();
    }
}
