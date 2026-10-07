# localai (Ollama + embeddings + Whisper + FFmpeg on host, Linux Mint 22 / Ryzen 7 250 / Radeon 780M gfx1103)
RAM: qwen3:4b-instruct-2507-q4_K_M at 8K context with q8_0 KV cache: about 3200-3600 MB (2500 weights + ~600 KV + overhead); at f16 KV, about 3800 MB. qwen3-embedding:0.6b: about 800-1000 MB. whisper.cpp large-v3-turbo-q5_0: about 900-1300 MB while running (unloads when idle only if the process stops). faster-whisper large-v3-turbo int8 on CPU: about 1500-2000 MB. All local AI loaded at once: about 5000-6000 MB. On this machine (13 GiB visible, about 8 GiB used, 5 GiB already swapped) that does not fit next to Postgres, Redis and Langfuse (ClickHouse) unless Chrome usage goes down. Use OLLAMA_KEEP_ALIVE and MAX_LOADED_MODELS=2 so models unload when idle. Disk: Ollama about 1.4 GB (+1 GB ROCm from the script), qwen3 4B 2.5 GB, embedding model 0.64 GB, whisper model 0.55 GB, about 5-6 GB in total.

I checked this machine with read-only commands and nothing was installed. The GPU shows up in lspci as "AMD Phoenix3" (Radeon 780M, gfx1103). The UMA VRAM carve-out is 2.00 GiB and GTT is 6.65 GiB. The CPU is Zen 4 with AVX-512. These are already installed: mesa-vulkan-drivers 25.2.8 (RADV), libvulkan1 and zstd. These are missing: cmake, glslc, libvulkan-dev, spirv-headers, vulkan-tools and ffmpeg (only the libav* libraries are present). The renderD128 device has a uaccess ACL, so a process running as the logged-in user can reach the GPU. The ufw service is active, but I could not read its rules without root. At the time of the check, free RAM was about 4 GiB and 5 GiB of swap was in use, so memory is the real limit.

OLLAMA
- The latest release is v0.40.0 (2026-09-25). The install script still uses `curl -fsSL https://ollama.com/install.sh | sh`. It needs zstd, puts the binary in /usr/local/bin and the libraries in /usr/local/lib/ollama, and creates the `ollama` user, adding it to the render and video groups. It writes /etc/systemd/system/ollama.service (User=ollama, Restart=always). When it sees an AMD GPU it also downloads ollama-linux-amd64-rocm.tar.zst, about 1 GB, which this GPU cannot use.
- Vulkan is included in the main amd64 tarball. Ollama's build_linux.sh leaves out only rocm* and mlx*, and the docs say "Vulkan is enabled by default when the backend is installed." `OLLAMA_VULKAN=1` is no longer needed. Use `OLLAMA_VULKAN=0` or `GGML_VK_VISIBLE_DEVICES=-1` to turn it off. To let Vulkan report free VRAM, the docs say to run `sudo setcap cap_perfmon+ep /usr/local/bin/ollama`. Without it, Ollama estimates from model size.
- ROCm does not support this GPU. gfx1103 is not on Ollama's ROCm v7 target list, which covers gfx1030, 1100, 1101, 1102, 1150, 1151, 1200, 1201 and others. The community workaround is `HSA_OVERRIDE_GFX_VERSION=11.0.0` (the brief mentioned 11.0.2, which is gfx1102). Vulkan is the supported route for the 780M, so use it and skip ROCm.
- iGPU memory caveat: llama.cpp has open issues saying the Vulkan backend does not count shared/GTT memory on iGPUs. With a 2 GiB carve-out, Ollama may load qwen3:4b only partly on the GPU. Check the PROCESSOR column in `ollama ps`.
- Speed (my estimate, not measured): the closest published number is a Ryzen Z1 Extreme, which has the same 12-CU gfx1103 GPU. In the llama.cpp Vulkan scoreboard (discussion #10879) it runs Llama-2-7B Q4_0 at about 18.8 tokens/s generating and 199 tokens/s reading the prompt. Generation is limited by memory bandwidth, so scaling to a 2.5 GB Q4_K_M model gives about 20–28 tokens/s on the Vulkan GPU. CPU-only on 8 Zen 4 cores should give about 12–20 tokens/s generating. The GPU mainly speeds up prompt reading: about 150–300 tokens/s versus about 40–80 on the CPU. Run a benchmark to confirm.
- Model tags, checked on ollama.com/library/qwen3/tags:
  - `qwen3:4b` is the same model as `qwen3:4b-thinking-2507-q4_K_M` (digest 359d7dd4bcda, 2.5 GB, 256K context). It always writes out its reasoning first, which is slow for an agent.
  - `qwen3:4b-instruct-2507-q4_K_M` (same as `qwen3:4b-instruct`, digest 0edcdef34593, 2.5 GB) is the one I recommend. There is also a q8_0 at 4.3 GB.
  - The original `qwen3:4b-q4_K_M` is 2.6 GB with 40K context.
  - `qwen3:8b` is 5.2 GB q4_K_M with 40K context. It is too large while Chrome and the other sessions are using the RAM.
  - Newer options: `qwen3.5:4b` (3.3 GB q4_K_M, accepts images, 256K context) and `qwen3.5:2b-q4_K_M` (1.9 GB). The only qwen3.8 size is 27b.
- RAM: Qwen3-4B's KV cache is about 144 KB per token at f16, which is roughly 0.6 GB at 4K context, 1.2 GB at 8K and 4.7 GB at 32K. With the 2.5 GB of weights, plan on about 3.5–4 GB at 8K context. Turning on flash attention and q8_0 KV cache roughly halves the cache. The default context is 4096 tokens.
- Embedding model for pgvector:
  - `qwen3-embedding:0.6b` (639 MB, 32K context, 1024 dimensions, adjustable from 32 to 1024, 100+ languages, MTEB multilingual 64.33) is my default. Queries use the prefix "Instruct: …\nQuery:". Use `vector(1024)`, which is under pgvector's 2000-dimension HNSW limit.
  - `embeddinggemma` (622 MB, 300M parameters, 768 dimensions, adjustable to 512/256/128, 2K context, multilingual) is the lighter alternative.
  - `bge-m3` (1.2 GB, 567M parameters, 8K context, 100+ languages, 1024 dimensions from the model card).
  - `nomic-embed-text` (274 MB, 2K context on Ollama, 768 dimensions, mainly English) is weak for pt-BR.
  - `embeddinggemma-2` (270m/440m/570m/740m, 768 dimensions, 256K context, multimodal) only just came out with v0.40.0. Wait before relying on it.

WHISPER
- whisper.cpp v1.9.5 was released 2026-10-06. To build it with Vulkan on Ubuntu 24.04 you need build-essential, cmake, glslc, libvulkan-dev and spirv-headers, all available in apt on noble. Its official Vulkan Dockerfile uses the same packages. The project publishes an image, `ghcr.io/ggml-org/whisper.cpp:main-vulkan`, which includes ffmpeg and Mesa RADV, and its README gives `--device /dev/dri` for AMD iGPUs. That image fits the Compose plan and avoids building on the host. It has no ENTRYPOINT, so the command runs the binary directly.
- Model files from HuggingFace ggerganov/whisper.cpp:
  - `ggml-large-v3-turbo-q5_0.bin` 547 MiB
  - `ggml-large-v3-turbo-q8_0.bin` 834 MiB
  - `ggml-large-v3-turbo.bin` 1549 MiB
  - `ggml-large-v3-q5_0.bin` 1031 MiB
  - `ggml-medium-q5_0.bin` 514 MiB
  - `ggml-small-q8_0.bin` 252 MiB
- whisper-server API: `POST /inference` (multipart: file, temperature, prompt, response_format=json|text|srt|vtt|verbose_json) and `POST /load`. Useful flags: `-l pt`, `--convert` (needs ffmpeg), `-fa`, `--vad -vm`, `--host`, and `--port` (default 8080). It has no OpenAI-compatible /v1/audio/transcriptions route, so LiteLLM needs a small adapter in front of it. The README warns against running the server as root.
- faster-whisper 1.2.1 (2025-10-31) runs on the CPU (int8) or NVIDIA CUDA only. It has no AMD or Vulkan support. It decodes audio with PyAV, so it does not need ffmpeg. The README's CPU benchmark (Small model, 13 min of audio) shows int8 at 1m42s versus whisper.cpp fp32 at 2m05s.
- Which is faster here: whisper.cpp with Vulkan on the 780M should be much faster than faster-whisper on the CPU. One data point: on Ryzen 4750U Vega graphics, large-v3-turbo ran at 3.4× real time on the GPU versus 0.8× on the CPU, and the 780M is several times stronger than that GPU. I have not measured it on this machine.
- For Portuguese (pt-BR), use large-v3-turbo-q5_0 with `-l pt`. OpenAI says turbo is about as accurate as large-v2, with the biggest losses in Thai and Cantonese, not Portuguese. It cannot translate. If quality is not good enough, try q8_0 or large-v3-q5_0. Distil-whisper models are English-only.

FFMPEG
- `sudo apt install ffmpeg` installs 7:6.1.1-3ubuntu5.
- Without sudo, BtbN/FFmpeg-Builds publishes static builds (ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz) that can be unpacked into ~/.local/bin.

OPTIONS WITHOUT SUDO
- Ollama: unpack the tarball into ~/.local/ollama and run it as a systemd --user service. It then runs as the logged-in user, who already has GPU access, and skips the ROCm download. Models go in ~/.ollama/models.
- whisper.cpp: the CPU-only prebuilt binary (whisper-bin-ubuntu-x64.tar.gz) or the Docker image (once Docker is installed).
- faster-whisper: install with pip or uv inside a virtual environment.
- cmake: `pip install cmake` works, but glslc, libvulkan-dev and spirv-headers still need apt, or the LunarG Vulkan SDK tarball.

## CMDS
# ===== 0. Check the GPU and Vulkan (needs sudo for apt) =====
sudo apt install -y vulkan-tools && vulkaninfo --summary | grep -E 'deviceName|driverName'   # expect AMD Radeon 780M / RADV PHOENIX

# ===== 1A. Ollama, system-wide (recommended) =====
curl -fsSL https://ollama.com/install.sh | sh        # zstd is already installed; this also pulls the ~1 GB ROCm tarball, which this GPU can't use
sudo setcap cap_perfmon+ep /usr/local/bin/ollama     # lets Vulkan report free VRAM; repeat after every upgrade
sudo systemctl edit ollama    # paste:
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_CONTEXT_LENGTH=8192"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_KEEP_ALIVE=10m"
Environment="OLLAMA_MAX_LOADED_MODELS=2"
Environment="OLLAMA_NUM_PARALLEL=1"
# Vulkan is on by default. Only if Vulkan misbehaves: Environment="OLLAMA_VULKAN=0"
# ROCm experiment, unsupported: Environment="HSA_OVERRIDE_GFX_VERSION=11.0.0"
sudo systemctl daemon-reload && sudo systemctl restart ollama
journalctl -u ollama -b | grep -iE 'vulkan|inference compute|total_vram'
# Firewall: Ollama has no authentication. Allow only the Docker bridge ranges, not the LAN
sudo ufw allow from 172.16.0.0/12 to any port 11434 proto tcp
# (Alternative: OLLAMA_HOST=172.17.0.1:11434 plus After=docker.service. The CLI then needs export OLLAMA_HOST=172.17.0.1:11434)

# ===== 1B. Ollama, user space (no sudo) =====
mkdir -p ~/.local/ollama && curl -fsSL https://ollama.com/download/ollama-linux-amd64.tar.zst | zstd -d | tar x -C ~/.local/ollama
mkdir -p ~/.config/systemd/user && cat > ~/.config/systemd/user/ollama.service <<'EOF'
[Unit]
Description=Ollama (user)
[Service]
ExecStart=%h/.local/ollama/bin/ollama serve
Environment="OLLAMA_HOST=0.0.0.0:11434" "OLLAMA_CONTEXT_LENGTH=8192" "OLLAMA_FLASH_ATTENTION=1" "OLLAMA_KV_CACHE_TYPE=q8_0" "OLLAMA_KEEP_ALIVE=10m" "OLLAMA_MAX_LOADED_MODELS=2"
Restart=always
[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now ollama
loginctl enable-linger "$USER"   # optional: keep it running after logout (may ask for polkit auth)

# ===== 2. Models =====
ollama pull qwen3:4b-instruct-2507-q4_K_M     # 2.5 GB, chat/tools model (no thinking output)
ollama pull qwen3-embedding:0.6b              # 639 MB, 1024 dimensions -> pgvector vector(1024)
# optional: ollama pull embeddinggemma (768 dims) | ollama pull qwen3:4b (thinking) | ollama pull qwen3.5:4b-q4_K_M
ollama run --verbose qwen3:4b-instruct-2507-q4_K_M "Resuma em uma frase: teste."   # prints eval rate tokens/s
ollama ps                                     # PROCESSOR column: 100% GPU vs CPU/GPU split
curl -s localhost:11434/api/embed -d '{"model":"qwen3-embedding:0.6b","input":"olá mundo"}' | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["embeddings"][0]))'

# ===== 3. Reaching host Ollama from docker-compose =====
services:
  litellm:
    extra_hosts: ["host.docker.internal:host-gateway"]
    environment:
      OLLAMA_API_BASE: http://host.docker.internal:11434
# litellm config.yaml model_list entry:
#  - model_name: local-qwen3-4b
#    litellm_params: { model: ollama_chat/qwen3:4b-instruct-2507-q4_K_M, api_base: "os.environ/OLLAMA_API_BASE" }
#  - model_name: local-embed
#    litellm_params: { model: ollama/qwen3-embedding:0.6b, api_base: "os.environ/OLLAMA_API_BASE" }
# pgvector: CREATE TABLE chunks(id bigserial primary key, content text, embedding vector(1024));
#           CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);

# ===== 4. FFmpeg =====
sudo apt install -y ffmpeg       # 6.1.1
# no-sudo: curl -L https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz | tar xJ -C ~/.local/opt && ln -s ~/.local/opt/ffmpeg-n8.1-latest-linux64-gpl-8.1/bin/ff* ~/.local/bin/
ffmpeg -i input.m4a -ar 16000 -ac 1 -c:a pcm_s16le output.wav

# ===== 5A. whisper.cpp in Docker with Vulkan (recommended, fits Compose) =====
  whisper:
    image: ghcr.io/ggml-org/whisper.cpp:main-vulkan
    devices: ["/dev/dri:/dev/dri"]
    volumes: ["./data/whisper-models:/models"]
    command: ["whisper-server","--host","0.0.0.0","--port","8080","-m","/models/ggml-large-v3-turbo-q5_0.bin","-l","pt","--convert","-fa","-t","8"]
# download the model once:
docker run --rm -v "$PWD/data/whisper-models:/models" ghcr.io/ggml-org/whisper.cpp:main-vulkan download-ggml-model.sh large-v3-turbo-q5_0 /models
docker run --rm --device /dev/dri -v "$PWD/data/whisper-models:/models" ghcr.io/ggml-org/whisper.cpp:main-vulkan whisper-bench -m /models/ggml-large-v3-turbo-q5_0.bin -t 8

# ===== 5B. whisper.cpp built on the host with Vulkan =====
sudo apt install -y build-essential cmake git glslc libvulkan-dev spirv-headers
git clone --branch v1.9.5 https://github.com/ggml-org/whisper.cpp.git ~/src/whisper.cpp && cd ~/src/whisper.cpp
cmake -B build -DGGML_VULKAN=1 -DCMAKE_BUILD_TYPE=Release && cmake --build build -j8 --config Release
sh ./models/download-ggml-model.sh large-v3-turbo-q5_0
./build/bin/whisper-bench -m models/ggml-large-v3-turbo-q5_0.bin -t 8
./build/bin/whisper-cli -m models/ggml-large-v3-turbo-q5_0.bin -l pt -f output.wav -otxt
./build/bin/whisper-server --host 0.0.0.0 --port 8080 -m models/ggml-large-v3-turbo-q5_0.bin -l pt --convert -fa -t 8
curl 127.0.0.1:8080/inference -F file=@audio.m4a -F temperature=0.0 -F response_format=json

# ===== 5C. faster-whisper on CPU (no sudo) =====
python3 -m venv ~/.venvs/fw && ~/.venvs/fw/bin/pip install faster-whisper==1.2.1
~/.venvs/fw/bin/python -c 'from faster_whisper import WhisperModel as W; m=W("large-v3-turbo",device="cpu",compute_type="int8",cpu_threads=8); s,i=m.transcribe("audio.m4a",language="pt",vad_filter=True); print("".join(x.text for x in s))'

## RISKS
- The tokens/s figures for qwen3:4b on this 780M are scaled from the Ryzen Z1 Extreme (same gfx1103) llama.cpp Vulkan result (Llama-2-7B Q4_0: tg128 18.77 t/s, pp512 199 t/s). They were not measured on this laptop and depend on RAM speed and type (DDR5 vs LPDDR5x). Benchmark with `ollama run --verbose`.
- The iGPU has only a 2 GiB UMA carve-out. llama.cpp issues report that the Vulkan backend does not count GTT/shared memory on iGPUs, so Ollama may load the model partly on the CPU. I could not confirm how Ollama v0.40 sizes GPU memory on this APU. Raising the UMA frame buffer in BIOS takes RAM away from the OS.
- HSA_OVERRIDE_GFX_VERSION=11.0.0 (or 11.0.2) for ROCm on gfx1103 is a community workaround. Ollama does not document it for this GPU, and Ollama's ROCm needs the ROCm v7 driver. I did not test it. I recommend Vulkan instead.
- The install script installs the ROCm tarball (~1 GB) whenever it detects an AMD GPU, and this GPU can't use it. The setcap cap_perfmon capability is lost when Ollama is upgraded.
- OLLAMA_HOST=0.0.0.0 exposes an API with no authentication. The ufw service is active, but I could not read its rules without root, so the LAN exposure and whether containers can reach port 11434 are unverified.
- host.docker.internal:host-gateway is documented by Docker, but the docs page does not state the default IP (usually 172.17.0.1 for docker0). Binding Ollama only to 172.17.0.1 requires Docker to start first.
- The whisper.cpp Vulkan vs faster-whisper CPU speed comparison on the 780M is an estimate, not a benchmark. Run whisper-bench and a faster-whisper timing on a real pt-BR clip.
- Whisper large-v3-turbo quality for pt-BR: OpenAI names Thai and Cantonese as the most degraded languages and does not quantify Portuguese. I did not find a WER figure for pt-BR. Turbo cannot translate.
- whisper-server has no OpenAI-compatible /v1/audio/transcriptions endpoint, so routing it through LiteLLM needs a small adapter, or a faster-whisper-based OpenAI-compatible server such as speaches (not researched).
- The embedding dimensions for bge-m3 (1024) and nomic-embed-text (768) come from the model cards, not the Ollama pages. I did not check whether Ollama's /api/embed accepts a `dimensions` parameter for MRL truncation, so store the full 1024 dimensions.
- embeddinggemma-2 and qwen3.5 have only just been released. I did not check tool-calling or thinking support for qwen3.5 small models. qwen3.8 exists only as a 27b model.
- faster-whisper's last release is 1.2.1 (Oct 2025) and it supports only CPU or NVIDIA CUDA. The ghcr whisper.cpp main-vulkan image tags are not pinned by version (registry API needs auth).

## SOURCES
https://docs.ollama.com/linux
https://raw.githubusercontent.com/ollama/ollama/main/docs/gpu.mdx
https://raw.githubusercontent.com/ollama/ollama/main/docs/faq.mdx
https://ollama.com/install.sh
https://raw.githubusercontent.com/ollama/ollama/main/scripts/build_linux.sh
https://raw.githubusercontent.com/ollama/ollama/main/Dockerfile
https://github.com/ollama/ollama/releases (v0.40.0, 2026-09-25)
https://ollama.com/library/qwen3/tags
https://ollama.com/library/qwen3.5/tags
https://ollama.com/library/qwen3.8/tags
https://ollama.com/library/qwen3-embedding/tags
https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
https://ollama.com/library/embeddinggemma
https://ollama.com/library/embeddinggemma-2/tags
https://ollama.com/library/bge-m3
https://ollama.com/library/nomic-embed-text
https://github.com/ggml-org/llama.cpp/discussions/10879
https://github.com/ggml-org/llama.cpp/issues/15120
https://raw.githubusercontent.com/ggml-org/llama.cpp/master/docs/build.md
https://www.phoronix.com/news/ollama-0.12.11-Vulkan
https://github.com/ggml-org/whisper.cpp (README, v1.9.5 released 2026-10-06)
https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/examples/server/README.md
https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/.devops/main-vulkan.Dockerfile
https://huggingface.co/ggerganov/whisper.cpp/tree/main
https://raw.githubusercontent.com/SYSTRAN/faster-whisper/master/README.md
https://pypi.org/project/faster-whisper/ (1.2.1)
https://github.com/openai/whisper/discussions/2363
https://models.handy.computer/models/whisper-large-v3-turbo
https://docs.docker.com/reference/cli/docker/container/run/
https://github.com/BtbN/FFmpeg-Builds/releases
Local read-only probes: lspci, /sys/class/drm/card2/device/mem_info_vram_total|gtt_total, dpkg -l, apt-cache policy, free -g