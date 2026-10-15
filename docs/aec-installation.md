# AEC Installation Guide

**Applies to:** Amazon Linux 2 / Amazon Linux 2023 (production server)

---

## Overview

The AEC module (`data-plane/app/transport/aec.py`) wraps SpeexDSP, a C library for
acoustic echo cancellation. The Python binding (`speexdsp-python`) compiles a native
extension at install time and requires build tools and the SpeexDSP development headers.

---

## Step-by-step install

### 1. Install system dependencies

```bash
sudo yum install -y gcc-c++ swig speex-devel speexdsp-devel
```

| Package | Purpose |
|---------|---------|
| `gcc-c++` | C++ compiler (the extension is C++) |
| `swig` | Generates the Python ↔ C++ binding from `speexdsp.i` |
| `speex-devel` | Speex codec headers (indirect dependency) |
| `speexdsp-devel` | SpeexDSP DSP headers — contains `speex/speex_echo.h` required by the extension |

> **Note:** `speex-devel` and `speexdsp-devel` are separate packages on Amazon Linux.
> Installing only `speex-devel` will succeed but the build will fail with
> `fatal error: speex/speex_echo.h: No such file or directory`.

### 2. Install the Python package

```bash
sudo /opt/codelexin/data-plane/.venv/bin/pip install \
  "speexdsp @ git+https://github.com/xiongyihui/speexdsp-python.git"
```

This compiles the extension against the system SpeexDSP headers. It takes ~30 seconds.

### 3. Restart the data-plane service

```bash
sudo systemctl restart codelexin-data-plane
```

### 4. Verify AEC is active

```bash
sudo journalctl -u codelexin-data-plane -n 50 | grep "AEC init"
```

Expected output:
```
AEC init | filter_tail_ms=400 max_delay_ms=500 warmup_ms=600
```

One line appears per call at call start. If the line is absent, AEC failed to load and
the data-plane is running without echo cancellation (calls still work — see Fallback).

---

## Troubleshooting

### `error: command 'swig' failed: No such file or directory`

```bash
sudo yum install -y swig
```

### `error: command 'g++' failed: No such file or directory`

```bash
sudo yum install -y gcc-c++
```

### `fatal error: speex/speex_echo.h: No such file or directory`

`speex-devel` is installed but `speexdsp-devel` is not. The two headers live in
separate packages on Amazon Linux:

```bash
sudo yum install -y speexdsp-devel
```

Verify the header is now present:

```bash
ls /usr/include/speex/speex_echo.h
```

---

## Other distributions

| Distribution | Install command |
|-------------|----------------|
| Ubuntu / Debian | `sudo apt-get install -y gcc g++ swig libspeex-dev libspeexdsp-dev` |
| Amazon Linux 2 / AL2023 | `sudo yum install -y gcc-c++ swig speex-devel speexdsp-devel` |
| Fedora / RHEL | `sudo dnf install -y gcc-c++ swig speex-devel speexdsp-devel` |

---

## Graceful fallback

If `speexdsp` is not installed or fails to import, `create_aec()` returns `None` and
logs a warning at startup:

```
speexdsp not installed — AEC disabled. Install: pip install git+https://github.com/xiongyihui/speexdsp-python.git
```

Both the classic pipeline (`TwilioTransport`) and the realtime pipeline
(`OpenAIRealtimeTwilioTransport`) check for `None` before calling AEC methods — calls
proceed normally without echo cancellation. The echo guard in the realtime pipeline
remains active as a timing-based safety net.
