// Real-time speech recognition fed by raw PCM on stdin — not a live
// microphone. Adapted from examples/stream/stream.cpp's sliding-window
// algorithm (step/length/keep re-decoding of a bounded trailing audio
// window), with SDL2 direct-mic-capture replaced by a stdin reader
// thread. This lets the host app (Veya) keep its already
// permission-gated Swift/AVAudioEngine microphone capture as the single
// owner of the microphone, while this process does genuine incremental
// transcription of the PCM it's handed — a persistent, stateful process
// that keeps re-decoding a bounded, sliding trailing window as new audio
// arrives, not a batch CLI invoked once per fixed window.
//
// Protocol: stdin is raw pcm_s16le mono at the model's sample rate
// (16000 Hz). Stdout is JSON Lines, one hypothesis per line:
//   {"type":"partial","text":"..."}
//   {"type":"final","text":"..."}
// A "final" line is emitted at each step/length reset boundary (the
// same "start a new line" checkpoint upstream's stream.cpp uses) and
// once more, unconditionally, when stdin reaches EOF (session ended) —
// this exits the process cleanly with status 0.

#include "common.h"
#include "whisper.h"

#include <algorithm>
#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr int kSampleRate = WHISPER_SAMPLE_RATE; // 16000, whisper.cpp's fixed internal rate
constexpr size_t kMaxBufferedSamples = (size_t) kSampleRate * 60; // bounded memory: 60s cap

// Thread-safe int16 sample queue fed by a background stdin-reading
// thread. Bounded so a consumer that falls behind can never grow memory
// without limit — the oldest not-yet-consumed audio is dropped instead.
class StdinPCMSource {
public:
    void start() {
        reader_ = std::thread([this] { readLoop(); });
    }

    ~StdinPCMSource() {
        if (reader_.joinable()) reader_.join();
    }

    // Blocks until at least `n` samples are buffered or stdin has hit
    // EOF. `out` receives up to `n` samples (fewer only at EOF). Sets
    // `isFinal` to true iff there will never be more data after this
    // call (EOF reached and the buffer is now fully drained) — the
    // caller's authoritative signal to flush and exit.
    void waitFor(size_t n, std::vector<int16_t> & out, bool & isFinal) {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [&] { return buffer_.size() >= n || eof_; });
        const size_t take = std::min(n, buffer_.size());
        out.assign(buffer_.begin(), buffer_.begin() + take);
        buffer_.erase(buffer_.begin(), buffer_.begin() + take);
        isFinal = eof_ && buffer_.empty();
    }

private:
    void readLoop() {
        std::vector<int16_t> chunk(4096);
        while (true) {
            const size_t n = fread(chunk.data(), sizeof(int16_t), chunk.size(), stdin);
            if (n == 0) {
                std::lock_guard<std::mutex> lock(mutex_);
                eof_ = true;
                cv_.notify_all();
                return;
            }
            std::lock_guard<std::mutex> lock(mutex_);
            buffer_.insert(buffer_.end(), chunk.begin(), chunk.begin() + n);
            if (buffer_.size() > kMaxBufferedSamples) {
                buffer_.erase(buffer_.begin(), buffer_.begin() + (buffer_.size() - kMaxBufferedSamples));
            }
            cv_.notify_all();
        }
    }

    std::thread reader_;
    std::mutex mutex_;
    std::condition_variable cv_;
    std::deque<int16_t> buffer_;
    bool eof_ = false;
};

std::string jsonEscape(const std::string & text) {
    std::string out;
    out.reserve(text.size());
    for (unsigned char c : text) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    out += (char) c;
                }
        }
    }
    return out;
}

void emitHypothesis(const char * type, const std::string & text) {
    printf("{\"type\":\"%s\",\"text\":\"%s\"}\n", type, jsonEscape(text).c_str());
    fflush(stdout);
}

} // namespace

int main(int argc, char ** argv) {
    ggml_backend_load_all();

    std::string model;
    int32_t step_ms   = 1000;
    int32_t length_ms = 6000;
    int32_t keep_ms   = 200;
    int32_t n_threads = std::min(4, (int32_t) std::thread::hardware_concurrency());
    bool keep_context = false;
    std::string language = "en";

    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];
        if (arg == "-m" || arg == "--model") model = argv[++i];
        else if (arg == "--step") step_ms = std::stoi(argv[++i]);
        else if (arg == "--length") length_ms = std::stoi(argv[++i]);
        else if (arg == "--keep") keep_ms = std::stoi(argv[++i]);
        else if (arg == "-t" || arg == "--threads") n_threads = std::stoi(argv[++i]);
        else if (arg == "-l" || arg == "--language") language = argv[++i];
        else if (arg == "-kc" || arg == "--keep-context") keep_context = true;
        else {
            fprintf(stderr, "error: unknown argument: %s\n", arg.c_str());
            return 1;
        }
    }

    if (model.empty()) {
        fprintf(stderr, "error: -m/--model is required\n");
        return 1;
    }

    keep_ms   = std::min(keep_ms, step_ms);
    length_ms = std::max(length_ms, step_ms);

    const int n_samples_step = (int) ((1e-3 * step_ms)   * kSampleRate);
    const int n_samples_len  = (int) ((1e-3 * length_ms) * kSampleRate);
    const int n_samples_keep = (int) ((1e-3 * keep_ms)   * kSampleRate);
    const int n_new_line     = std::max(1, length_ms / step_ms - 1);

    struct whisper_context_params cparams = whisper_context_default_params();
    struct whisper_context * ctx = whisper_init_from_file_with_params(model.c_str(), cparams);
    if (ctx == nullptr) {
        fprintf(stderr, "error: failed to initialize whisper context from '%s'\n", model.c_str());
        return 2;
    }

    fprintf(
        stderr,
        "whisper-stream-stdin: step=%dms length=%dms keep=%dms threads=%d lang=%s keep_context=%d\n",
        step_ms, length_ms, keep_ms, n_threads, language.c_str(), keep_context
    );

    StdinPCMSource source;
    source.start();

    std::vector<float> pcmf32;
    std::vector<float> pcmf32_old;
    std::vector<whisper_token> prompt_tokens;

    int n_iter = 0;

    while (true) {
        std::vector<int16_t> step_i16;
        bool is_final = false;
        source.waitFor((size_t) n_samples_step, step_i16, is_final);

        if (step_i16.empty() && is_final && pcmf32_old.empty()) {
            break; // nothing was ever buffered, and nothing more is coming
        }

        std::vector<float> pcmf32_new(step_i16.size());
        for (size_t i = 0; i < step_i16.size(); i++) {
            pcmf32_new[i] = step_i16[i] / 32768.0f;
        }

        const int n_samples_new = (int) pcmf32_new.size();
        const int n_samples_take = std::min((int) pcmf32_old.size(), std::max(0, n_samples_keep + n_samples_len - n_samples_new));

        pcmf32.resize(n_samples_new + n_samples_take);
        for (int i = 0; i < n_samples_take; i++) {
            pcmf32[i] = pcmf32_old[pcmf32_old.size() - n_samples_take + i];
        }
        if (n_samples_new > 0) {
            memcpy(pcmf32.data() + n_samples_take, pcmf32_new.data(), n_samples_new * sizeof(float));
        }
        pcmf32_old = pcmf32;

        if (!pcmf32.empty()) {
            whisper_full_params wparams = whisper_full_default_params(WHISPER_SAMPLING_GREEDY);
            wparams.print_progress   = false;
            wparams.print_special    = false;
            wparams.print_realtime   = false;
            wparams.print_timestamps = false;
            wparams.single_segment   = true;
            wparams.max_tokens       = 0;
            wparams.language         = language.c_str();
            wparams.n_threads        = n_threads;
            wparams.no_context       = !keep_context;
            wparams.prompt_tokens    = keep_context && !prompt_tokens.empty() ? prompt_tokens.data() : nullptr;
            wparams.prompt_n_tokens  = keep_context ? (int) prompt_tokens.size() : 0;

            if (whisper_full(ctx, wparams, pcmf32.data(), pcmf32.size()) == 0) {
                std::string text;
                const int n_segments = whisper_full_n_segments(ctx);
                for (int i = 0; i < n_segments; ++i) {
                    text += whisper_full_get_segment_text(ctx, i);
                }

                const bool boundary = is_final || ((n_iter + 1) % n_new_line == 0);
                emitHypothesis(boundary ? "final" : "partial", text);

                if (boundary) {
                    const size_t keep_from = pcmf32.size() > (size_t) n_samples_keep ? pcmf32.size() - (size_t) n_samples_keep : 0;
                    pcmf32_old = std::vector<float>(pcmf32.begin() + keep_from, pcmf32.end());

                    if (keep_context) {
                        prompt_tokens.clear();
                        for (int i = 0; i < n_segments; ++i) {
                            const int token_count = whisper_full_n_tokens(ctx, i);
                            for (int j = 0; j < token_count; ++j) {
                                prompt_tokens.push_back(whisper_full_get_token_id(ctx, i, j));
                            }
                        }
                    }
                }
            } else {
                fprintf(stderr, "whisper-stream-stdin: whisper_full failed on step %d\n", n_iter);
            }
        }

        ++n_iter;
        if (is_final) break;
    }

    whisper_free(ctx);
    return 0;
}
