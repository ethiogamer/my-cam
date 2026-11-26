// CamBridge fast video decoder for Windows.
//
// Decodes H.264 / HEVC access units with Windows' built-in Media Foundation decoder,
// then converts NV12 -> BGR24 (scaled, letterboxed) on all CPU cores.
// Plain C exports so Python can call it with ctypes.

#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mftransform.h>
#include <mferror.h>
#include <codecapi.h>

#include <algorithm>
#include <mutex>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "mfreadwrite.lib")
#pragma comment(lib, "ole32.lib")

namespace {

template <class T> void SafeRelease(T*& p) {
    if (p) { p->Release(); p = nullptr; }
}

struct Decoder {
    IMFTransform* mft = nullptr;
    bool hevc = false;
    bool started = false;
    // Current output format
    UINT32 frameW = 0, frameH = 0;   // buffer size (may be padded, e.g. 1088)
    UINT32 showW = 0, showH = 0;     // visible picture
    LONG stride = 0;
    bool fullRange = false;
    bool bt601 = false;
    // Newest decoded picture (compact NV12, showW x showH)
    std::vector<uint8_t> nv12;
    bool hasFrame = false;
    LONGLONG pts = 0;
    std::string error;
    unsigned threads = 4;
};

bool g_mfStarted = false;

void SetError(Decoder* d, const char* where, HRESULT hr) {
    char buf[160];
    snprintf(buf, sizeof(buf), "%s failed (0x%08lx)", where, static_cast<unsigned long>(hr));
    d->error = buf;
}

bool ConfigureOutput(Decoder* d) {
    for (DWORD i = 0;; ++i) {
        IMFMediaType* type = nullptr;
        HRESULT hr = d->mft->GetOutputAvailableType(0, i, &type);
        if (FAILED(hr)) {
            SetError(d, "GetOutputAvailableType", hr);
            return false;
        }
        GUID subtype = {};
        type->GetGUID(MF_MT_SUBTYPE, &subtype);
        if (subtype == MFVideoFormat_NV12) {
            hr = d->mft->SetOutputType(0, type, 0);
            if (FAILED(hr)) {
                SafeRelease(type);
                SetError(d, "SetOutputType", hr);
                return false;
            }
            MFGetAttributeSize(type, MF_MT_FRAME_SIZE, &d->frameW, &d->frameH);
            d->showW = d->frameW;
            d->showH = d->frameH;
            MFVideoArea area = {};
            if (SUCCEEDED(type->GetBlob(MF_MT_MINIMUM_DISPLAY_APERTURE, reinterpret_cast<UINT8*>(&area),
                                        sizeof(area), nullptr))) {
                if (area.Area.cx > 0 && area.Area.cy > 0) {
                    d->showW = static_cast<UINT32>(area.Area.cx);
                    d->showH = static_cast<UINT32>(area.Area.cy);
                }
            }
            UINT32 strideValue = 0;
            d->stride = SUCCEEDED(type->GetUINT32(MF_MT_DEFAULT_STRIDE, &strideValue))
                            ? static_cast<LONG>(strideValue) : static_cast<LONG>(d->frameW);
            UINT32 range = 0;
            d->fullRange = SUCCEEDED(type->GetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, &range)) &&
                           range == MFNominalRange_0_255;
            UINT32 matrix = 0;
            d->bt601 = SUCCEEDED(type->GetUINT32(MF_MT_YUV_MATRIX, &matrix)) &&
                       matrix == MFVideoTransferMatrix_BT601;
            SafeRelease(type);
            return true;
        }
        SafeRelease(type);
    }
}

bool CopyFrame(Decoder* d, IMFSample* sample) {
    IMFMediaBuffer* buffer = nullptr;
    HRESULT hr = sample->ConvertToContiguousBuffer(&buffer);
    if (FAILED(hr)) return false;

    BYTE* data = nullptr;
    LONG pitch = d->stride;
    IMF2DBuffer* buffer2d = nullptr;
    bool locked2d = false;
    DWORD maxLen = 0, curLen = 0;
    if (SUCCEEDED(buffer->QueryInterface(IID_PPV_ARGS(&buffer2d))) &&
        SUCCEEDED(buffer2d->Lock2D(&data, &pitch))) {
        locked2d = true;
    } else if (FAILED(buffer->Lock(&data, &maxLen, &curLen))) {
        SafeRelease(buffer2d);
        SafeRelease(buffer);
        return false;
    }
    if (pitch <= 0) pitch = static_cast<LONG>(d->frameW);

    const UINT32 w = d->showW & ~1u, h = d->showH & ~1u;
    d->nv12.resize(static_cast<size_t>(w) * h * 3 / 2);
    // Y plane
    for (UINT32 y = 0; y < h; ++y)
        memcpy(&d->nv12[static_cast<size_t>(y) * w], data + static_cast<size_t>(y) * pitch, w);
    // UV plane starts after the (padded) luma height
    const BYTE* uv = data + static_cast<size_t>(pitch) * d->frameH;
    uint8_t* dstUv = &d->nv12[static_cast<size_t>(w) * h];
    for (UINT32 y = 0; y < h / 2; ++y)
        memcpy(dstUv + static_cast<size_t>(y) * w, uv + static_cast<size_t>(y) * pitch, w);
    d->showW = w;
    d->showH = h;
    d->hasFrame = true;

    if (locked2d) buffer2d->Unlock2D(); else buffer->Unlock();
    SafeRelease(buffer2d);
    SafeRelease(buffer);
    return true;
}

// Pulls every finished picture out of the decoder. Returns number of pictures.
int Drain(Decoder* d) {
    int got = 0;
    for (;;) {
        MFT_OUTPUT_STREAM_INFO info = {};
        d->mft->GetOutputStreamInfo(0, &info);
        const bool providesSamples =
            (info.dwFlags & (MFT_OUTPUT_STREAM_PROVIDES_SAMPLES | MFT_OUTPUT_STREAM_CAN_PROVIDE_SAMPLES)) != 0;

        MFT_OUTPUT_DATA_BUFFER out = {};
        IMFSample* mine = nullptr;
        if (!providesSamples) {
            IMFMediaBuffer* buf = nullptr;
            if (FAILED(MFCreateSample(&mine)) ||
                FAILED(MFCreateMemoryBuffer(std::max<DWORD>(info.cbSize, d->frameW * d->frameH * 3 / 2 + 4096), &buf))) {
                SafeRelease(mine);
                return got;
            }
            mine->AddBuffer(buf);
            SafeRelease(buf);
            out.pSample = mine;
        }
        DWORD status = 0;
        HRESULT hr = d->mft->ProcessOutput(0, 1, &out, &status);
        if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) {
            SafeRelease(mine);
            SafeRelease(out.pEvents);
            return got;
        }
        if (hr == MF_E_TRANSFORM_STREAM_CHANGE) {
            SafeRelease(mine);
            SafeRelease(out.pEvents);
            if (!ConfigureOutput(d)) return -1;
            continue;
        }
        if (FAILED(hr)) {
            SafeRelease(mine);
            SafeRelease(out.pEvents);
            SetError(d, "ProcessOutput", hr);
            return got > 0 ? got : -1;
        }
        if (out.pSample && CopyFrame(d, out.pSample)) ++got;
        if (out.pSample != mine) SafeRelease(out.pSample);
        SafeRelease(mine);
        SafeRelease(out.pEvents);
    }
}

IMFTransform* FindDecoder(bool hevc) {
    MFT_REGISTER_TYPE_INFO input = {MFMediaType_Video, hevc ? MFVideoFormat_HEVC : MFVideoFormat_H264};
    IMFActivate** list = nullptr;
    UINT32 count = 0;
    IMFTransform* mft = nullptr;
    // Synchronous (software) decoders: simple and fast; they use all CPU cores.
    HRESULT hr = MFTEnumEx(MFT_CATEGORY_VIDEO_DECODER,
                           MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_LOCALMFT | MFT_ENUM_FLAG_SORTANDFILTER,
                           &input, nullptr, &list, &count);
    if (SUCCEEDED(hr)) {
        for (UINT32 i = 0; i < count && !mft; ++i) {
            if (FAILED(list[i]->ActivateObject(IID_PPV_ARGS(&mft)))) mft = nullptr;
        }
        for (UINT32 i = 0; i < count; ++i) list[i]->Release();
        CoTaskMemFree(list);
    }
    return mft;
}

// ---- NV12 -> BGR24 with bilinear scaling and letterboxing -------------------

struct Coeffs {
    int y, rv, gu, gv, bu, yoff;
};

Coeffs MakeCoeffs(bool full, bool bt601) {
    // 16.16 fixed point
    if (bt601) {
        return full ? Coeffs{65536, 91881, 22554, 46802, 116130, 0}
                    : Coeffs{76309, 104597, 25675, 53279, 132201, 16};
    }
    return full ? Coeffs{65536, 103206, 12277, 30679, 121609, 0}
                : Coeffs{76309, 117489, 13975, 34925, 138438, 16};
}

inline uint8_t Clamp(int v) { return static_cast<uint8_t>(v < 0 ? 0 : (v > 255 ? 255 : v)); }

void ConvertRows(const Decoder* d, uint8_t* dst, int dw, int dh, int ox, int oy, int sw, int sh,
                 int rowStart, int rowEnd, const Coeffs& k) {
    const int w = static_cast<int>(d->showW), h = static_cast<int>(d->showH);
    const uint8_t* Y = d->nv12.data();
    const uint8_t* UV = Y + static_cast<size_t>(w) * h;
    // source step per output pixel, 16.16
    const int64_t stepX = (static_cast<int64_t>(w) << 16) / sw;
    const int64_t stepY = (static_cast<int64_t>(h) << 16) / sh;
    for (int y = rowStart; y < rowEnd; ++y) {
        uint8_t* row = dst + static_cast<size_t>(y) * dw * 3;
        const int iy = y - oy;
        if (iy < 0 || iy >= sh) {
            memset(row, 0, static_cast<size_t>(dw) * 3);
            continue;
        }
        if (ox > 0) {
            memset(row, 0, static_cast<size_t>(ox) * 3);
            memset(row + static_cast<size_t>(ox + sw) * 3, 0, static_cast<size_t>(dw - ox - sw) * 3);
        }
        int64_t sy = iy * stepY + stepY / 2 - 32768;
        if (sy < 0) sy = 0;
        int y0 = static_cast<int>(sy >> 16);
        int fy = static_cast<int>(sy & 0xFFFF) >> 8;  // 0..255
        int y1 = std::min(y0 + 1, h - 1);
        const uint8_t* r0 = Y + static_cast<size_t>(y0) * w;
        const uint8_t* r1 = Y + static_cast<size_t>(y1) * w;
        const uint8_t* c = UV + static_cast<size_t>(std::min(y0 / 2, h / 2 - 1)) * w;
        uint8_t* p = row + static_cast<size_t>(ox) * 3;
        int64_t sx = stepX / 2 - 32768;
        for (int x = 0; x < sw; ++x, sx += stepX, p += 3) {
            int64_t sxc = sx < 0 ? 0 : sx;
            int x0 = static_cast<int>(sxc >> 16);
            int fx = static_cast<int>(sxc & 0xFFFF) >> 8;
            int x1 = std::min(x0 + 1, w - 1);
            int top = r0[x0] * (256 - fx) + r0[x1] * fx;
            int bot = r1[x0] * (256 - fx) + r1[x1] * fx;
            int yy = ((top * (256 - fy) + bot * fy) >> 16) - k.yoff;
            int cx = (x0 / 2) * 2;
            int u = c[cx] - 128;
            int v = c[cx + 1] - 128;
            int luma = yy * k.y;
            p[2] = Clamp((luma + k.rv * v + 32768) >> 16);
            p[1] = Clamp((luma - k.gu * u - k.gv * v + 32768) >> 16);
            p[0] = Clamp((luma + k.bu * u + 32768) >> 16);
        }
    }
}

}  // namespace

extern "C" {

__declspec(dllexport) void* cb_create(int hevc) {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (!g_mfStarted) {
        if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_LITE))) return nullptr;
        g_mfStarted = true;
    }
    auto* d = new Decoder();
    d->hevc = hevc != 0;
    d->threads = std::max(2u, std::min(16u, std::thread::hardware_concurrency()));
    d->mft = FindDecoder(d->hevc);
    if (!d->mft) {
        delete d;
        return nullptr;
    }
    IMFAttributes* attrs = nullptr;
    if (SUCCEEDED(d->mft->GetAttributes(&attrs))) {
        attrs->SetUINT32(CODECAPI_AVLowLatencyMode, TRUE);
        attrs->SetUINT32(CODECAPI_AVDecNumWorkerThreads, d->threads);
        SafeRelease(attrs);
    }
    IMFMediaType* in = nullptr;
    MFCreateMediaType(&in);
    in->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    in->SetGUID(MF_MT_SUBTYPE, d->hevc ? MFVideoFormat_HEVC : MFVideoFormat_H264);
    in->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    HRESULT hr = d->mft->SetInputType(0, in, 0);
    SafeRelease(in);
    if (FAILED(hr) || !ConfigureOutput(d)) {
        SafeRelease(d->mft);
        delete d;
        return nullptr;
    }
    d->mft->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
    d->mft->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
    d->started = true;
    return d;
}

// Feeds one access unit (Annex-B). Returns pictures produced (0 = none yet), <0 = error.
__declspec(dllexport) int cb_decode(void* handle, const uint8_t* data, int len) {
    auto* d = static_cast<Decoder*>(handle);
    if (!d || !d->mft || len <= 0) return -1;
    IMFSample* sample = nullptr;
    IMFMediaBuffer* buffer = nullptr;
    if (FAILED(MFCreateSample(&sample)) || FAILED(MFCreateMemoryBuffer(static_cast<DWORD>(len), &buffer))) {
        SafeRelease(sample);
        SafeRelease(buffer);
        return -1;
    }
    BYTE* dst = nullptr;
    buffer->Lock(&dst, nullptr, nullptr);
    memcpy(dst, data, static_cast<size_t>(len));
    buffer->Unlock();
    buffer->SetCurrentLength(static_cast<DWORD>(len));
    sample->AddBuffer(buffer);
    sample->SetSampleTime(d->pts);
    sample->SetSampleDuration(166666);
    d->pts += 166666;

    int got = 0;
    HRESULT hr = d->mft->ProcessInput(0, sample, 0);
    if (hr == MF_E_NOTACCEPTING) {
        int n = Drain(d);
        if (n > 0) got += n;
        hr = d->mft->ProcessInput(0, sample, 0);
    }
    SafeRelease(buffer);
    SafeRelease(sample);
    if (FAILED(hr)) {
        SetError(d, "ProcessInput", hr);
        return got > 0 ? got : -1;
    }
    int n = Drain(d);
    if (n < 0) return got > 0 ? got : -1;
    return got + n;
}

__declspec(dllexport) int cb_frame_size(void* handle, int* w, int* h) {
    auto* d = static_cast<Decoder*>(handle);
    if (!d || !d->hasFrame) return 0;
    *w = static_cast<int>(d->showW);
    *h = static_cast<int>(d->showH);
    return 1;
}

// Converts the newest picture to BGR24 (dw x dh, top row first), keeping its shape
// (black bars if needed). Returns 1 on success.
__declspec(dllexport) int cb_to_bgr(void* handle, uint8_t* dst, int dw, int dh) {
    auto* d = static_cast<Decoder*>(handle);
    if (!d || !d->hasFrame || dw <= 0 || dh <= 0) return 0;
    const double scale = std::min(static_cast<double>(dw) / d->showW, static_cast<double>(dh) / d->showH);
    int sw = std::max(1, static_cast<int>(d->showW * scale + 0.5));
    int sh = std::max(1, static_cast<int>(d->showH * scale + 0.5));
    sw = std::min(sw, dw);
    sh = std::min(sh, dh);
    const int ox = (dw - sw) / 2, oy = (dh - sh) / 2;
    const Coeffs k = MakeCoeffs(d->fullRange, d->bt601);
    const int n = static_cast<int>(d->threads);
    std::vector<std::thread> workers;
    const int per = (dh + n - 1) / n;
    for (int t = 0; t < n; ++t) {
        int a = t * per, b = std::min(dh, a + per);
        if (a >= b) break;
        workers.emplace_back(ConvertRows, d, dst, dw, dh, ox, oy, sw, sh, a, b, std::cref(k));
    }
    for (auto& w : workers) w.join();
    return 1;
}

__declspec(dllexport) const char* cb_last_error(void* handle) {
    auto* d = static_cast<Decoder*>(handle);
    return d ? d->error.c_str() : "no decoder";
}

__declspec(dllexport) void cb_destroy(void* handle) {
    auto* d = static_cast<Decoder*>(handle);
    if (!d) return;
    if (d->mft) {
        d->mft->ProcessMessage(MFT_MESSAGE_NOTIFY_END_OF_STREAM, 0);
        d->mft->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
    }
    SafeRelease(d->mft);
    delete d;
}

}  // extern "C"

// ============================================================================
// MP4 recorder: BGR24 frames + 16-bit PCM audio -> H.264 + AAC .mp4
// (Windows' own encoders; uses the graphics chip when available.)
// ============================================================================

struct Recorder {
    IMFSinkWriter* writer = nullptr;
    DWORD videoStream = 0, audioStream = 0;
    int width = 0, height = 0, fps = 30;
    bool hasAudio = false;
    int rate = 0, channels = 0;
    LONGLONG start = -1;
    LONGLONG lastVideo = -1;
    std::mutex lock;
    std::string error;
    unsigned threads = 4;
};

static void BgrToBgra(const uint8_t* src, uint8_t* dst, int w, int h, unsigned threads) {
    auto work = [=](int a, int b) {
        for (int y = a; y < b; ++y) {
            const uint8_t* s = src + static_cast<size_t>(y) * w * 3;
            uint8_t* d = dst + static_cast<size_t>(y) * w * 4;
            for (int x = 0; x < w; ++x, s += 3, d += 4) {
                d[0] = s[0]; d[1] = s[1]; d[2] = s[2]; d[3] = 255;
            }
        }
    };
    std::vector<std::thread> pool;
    int per = (h + static_cast<int>(threads) - 1) / static_cast<int>(threads);
    for (int a = 0; a < h; a += per) pool.emplace_back(work, a, std::min(h, a + per));
    for (auto& t : pool) t.join();
}

extern "C" {

__declspec(dllexport) void* cb_rec_open(const wchar_t* path, int w, int h, int fps, int kbps,
                                        int audioRate, int audioChannels) {
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (!g_mfStarted) {
        if (FAILED(MFStartup(MF_VERSION, MFSTARTUP_LITE))) return nullptr;
        g_mfStarted = true;
    }
    auto* r = new Recorder();
    r->width = w & ~1; r->height = h & ~1; r->fps = fps > 0 ? fps : 30;
    r->threads = std::max(2u, std::min(16u, std::thread::hardware_concurrency()));

    IMFAttributes* attrs = nullptr;
    MFCreateAttributes(&attrs, 3);
    attrs->SetUINT32(MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, TRUE);
    attrs->SetGUID(MF_TRANSCODE_CONTAINERTYPE, MFTranscodeContainerType_MPEG4);
    HRESULT hr = MFCreateSinkWriterFromURL(path, nullptr, attrs, &r->writer);
    SafeRelease(attrs);
    if (FAILED(hr)) { delete r; return nullptr; }

    // Video: H.264 out, RGB32 in
    IMFMediaType* out = nullptr;
    MFCreateMediaType(&out);
    out->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    out->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
    out->SetUINT32(MF_MT_AVG_BITRATE, static_cast<UINT32>(std::max(1000, kbps)) * 1000u);
    out->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
    MFSetAttributeSize(out, MF_MT_FRAME_SIZE, r->width, r->height);
    MFSetAttributeRatio(out, MF_MT_FRAME_RATE, r->fps, 1);
    MFSetAttributeRatio(out, MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
    hr = r->writer->AddStream(out, &r->videoStream);
    SafeRelease(out);
    if (SUCCEEDED(hr)) {
        IMFMediaType* in = nullptr;
        MFCreateMediaType(&in);
        in->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        in->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_RGB32);
        in->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        in->SetUINT32(MF_MT_DEFAULT_STRIDE, static_cast<UINT32>(r->width * 4));  // top row first
        MFSetAttributeSize(in, MF_MT_FRAME_SIZE, r->width, r->height);
        MFSetAttributeRatio(in, MF_MT_FRAME_RATE, r->fps, 1);
        MFSetAttributeRatio(in, MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
        hr = r->writer->SetInputMediaType(r->videoStream, in, nullptr);
        SafeRelease(in);
    }
    if (FAILED(hr)) { SafeRelease(r->writer); delete r; return nullptr; }

    // Audio: AAC out, PCM in
    if (audioRate > 0 && audioChannels > 0) {
        int rate = (audioRate == 44100) ? 44100 : 48000;
        int ch = std::min(2, audioChannels);
        IMFMediaType* aout = nullptr;
        MFCreateMediaType(&aout);
        aout->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
        aout->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_AAC);
        aout->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
        aout->SetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND, rate);
        aout->SetUINT32(MF_MT_AUDIO_NUM_CHANNELS, ch);
        aout->SetUINT32(MF_MT_AUDIO_AVG_BYTES_PER_SECOND, 24000);  // 192 kbit/s
        HRESULT ahr = r->writer->AddStream(aout, &r->audioStream);
        SafeRelease(aout);
        if (SUCCEEDED(ahr)) {
            IMFMediaType* ain = nullptr;
            MFCreateMediaType(&ain);
            ain->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
            ain->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_PCM);
            ain->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
            ain->SetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND, rate);
            ain->SetUINT32(MF_MT_AUDIO_NUM_CHANNELS, ch);
            ain->SetUINT32(MF_MT_AUDIO_BLOCK_ALIGNMENT, ch * 2);
            ain->SetUINT32(MF_MT_AUDIO_AVG_BYTES_PER_SECOND, rate * ch * 2);
            ahr = r->writer->SetInputMediaType(r->audioStream, ain, nullptr);
            SafeRelease(ain);
        }
        if (SUCCEEDED(ahr)) { r->hasAudio = true; r->rate = rate; r->channels = ch; }
    }

    hr = r->writer->BeginWriting();
    if (FAILED(hr)) { SafeRelease(r->writer); delete r; return nullptr; }
    return r;
}

// Which audio format the recorder accepted (0 = no audio).
__declspec(dllexport) int cb_rec_audio_format(void* handle, int* rate, int* channels) {
    auto* r = static_cast<Recorder*>(handle);
    if (!r || !r->hasAudio) return 0;
    *rate = r->rate;
    *channels = r->channels;
    return 1;
}

static LONGLONG RelTime(Recorder* r, LONGLONG t) {
    if (r->start < 0) r->start = t;
    return std::max<LONGLONG>(0, t - r->start);
}

// bgr: width*height*3 bytes, top row first. time100ns: a shared clock (same for audio).
__declspec(dllexport) int cb_rec_video(void* handle, const uint8_t* bgr, long long time100ns) {
    auto* r = static_cast<Recorder*>(handle);
    if (!r || !r->writer) return 0;
    std::lock_guard<std::mutex> guard(r->lock);
    LONGLONG t = RelTime(r, time100ns);
    if (t <= r->lastVideo) t = r->lastVideo + 1;
    r->lastVideo = t;
    const DWORD size = static_cast<DWORD>(r->width) * r->height * 4;
    IMFMediaBuffer* buf = nullptr;
    IMFSample* sample = nullptr;
    if (FAILED(MFCreateMemoryBuffer(size, &buf)) || FAILED(MFCreateSample(&sample))) {
        SafeRelease(buf); SafeRelease(sample);
        return 0;
    }
    BYTE* dst = nullptr;
    buf->Lock(&dst, nullptr, nullptr);
    BgrToBgra(bgr, dst, r->width, r->height, r->threads);
    buf->Unlock();
    buf->SetCurrentLength(size);
    sample->AddBuffer(buf);
    sample->SetSampleTime(t);
    sample->SetSampleDuration(10000000LL / r->fps);
    HRESULT hr = r->writer->WriteSample(r->videoStream, sample);
    SafeRelease(buf); SafeRelease(sample);
    return SUCCEEDED(hr) ? 1 : 0;
}

// pcm: interleaved 16-bit samples in the format from cb_rec_audio_format.
__declspec(dllexport) int cb_rec_audio(void* handle, const int16_t* pcm, int frames, long long time100ns) {
    auto* r = static_cast<Recorder*>(handle);
    if (!r || !r->writer || !r->hasAudio || frames <= 0) return 0;
    std::lock_guard<std::mutex> guard(r->lock);
    LONGLONG t = RelTime(r, time100ns);
    const DWORD size = static_cast<DWORD>(frames) * r->channels * 2;
    IMFMediaBuffer* buf = nullptr;
    IMFSample* sample = nullptr;
    if (FAILED(MFCreateMemoryBuffer(size, &buf)) || FAILED(MFCreateSample(&sample))) {
        SafeRelease(buf); SafeRelease(sample);
        return 0;
    }
    BYTE* dst = nullptr;
    buf->Lock(&dst, nullptr, nullptr);
    memcpy(dst, pcm, size);
    buf->Unlock();
    buf->SetCurrentLength(size);
    sample->AddBuffer(buf);
    sample->SetSampleTime(t);
    sample->SetSampleDuration(static_cast<LONGLONG>(frames) * 10000000LL / r->rate);
    HRESULT hr = r->writer->WriteSample(r->audioStream, sample);
    SafeRelease(buf); SafeRelease(sample);
    return SUCCEEDED(hr) ? 1 : 0;
}

__declspec(dllexport) int cb_rec_close(void* handle) {
    auto* r = static_cast<Recorder*>(handle);
    if (!r) return 0;
    HRESULT hr = S_OK;
    {
        std::lock_guard<std::mutex> guard(r->lock);
        if (r->writer) {
            hr = r->writer->Finalize();
            SafeRelease(r->writer);
        }
    }
    delete r;
    return SUCCEEDED(hr) ? 1 : 0;
}


// Sharpen a BGR24 picture in place (amount 0..100). Uses all CPU cores.
__declspec(dllexport) void cb_sharpen(uint8_t* bgr, int w, int h, int amount) {
    if (!bgr || w < 3 || h < 3 || amount <= 0) return;
    const int k = std::min(100, amount) * 256 / 100;   // 0..256 (x/256)
    const size_t stride = static_cast<size_t>(w) * 3;
    std::vector<uint8_t> src(bgr, bgr + stride * h);
    unsigned n = std::max(2u, std::min(16u, std::thread::hardware_concurrency()));
    auto work = [&](int a, int b) {
        for (int y = std::max(1, a); y < std::min(h - 1, b); ++y) {
            const uint8_t* up = &src[(y - 1) * stride];
            const uint8_t* mid = &src[y * stride];
            const uint8_t* dn = &src[(y + 1) * stride];
            uint8_t* out = bgr + y * stride;
            for (size_t i = 3; i < stride - 3; ++i) {
                int blur = (up[i] + dn[i] + mid[i - 3] + mid[i + 3]) >> 2;
                int v = mid[i] + (((mid[i] - blur) * k) >> 7);   // up to 2x the detail
                out[i] = static_cast<uint8_t>(v < 0 ? 0 : (v > 255 ? 255 : v));
            }
        }
    };
    std::vector<std::thread> pool;
    int per = (h + static_cast<int>(n) - 1) / static_cast<int>(n);
    for (int a = 0; a < h; a += per) pool.emplace_back(work, a, std::min(h, a + per));
    for (auto& t : pool) t.join();
}

}  // extern "C"
