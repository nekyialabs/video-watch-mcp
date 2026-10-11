"""
Video Watch - Remote MCP Server on Modal
Fully cloud-hosted MCP server that lets Claude "watch" videos.

Three tools:
- video_listen: Transcript only (lightweight)
- video_see: Frames only (visual content)
- watch_video: Both (full experience)
"""

import modal
import base64
import json
import os
import subprocess
import tempfile
from pathlib import Path

# Image with all dependencies
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "curl")
    .pip_install(
        "curl_cffi",  # For browser impersonation (TikTok, Instagram, etc.)
        "brotli",     # For compression support
        "openai-whisper",
        "torch",
        "mcp[cli]",
        "starlette",
        "sse-starlette",
        "uvicorn",
    )
    # Install yt-dlp last and force latest — platforms break old versions constantly.
    # force_build=True rebuilds THIS layer on every deploy so yt-dlp is always current
    # (the heavy torch/whisper layer above stays cached, so the rebuild is cheap). Without
    # this, Modal caches the identical command and we silently ship a months-old yt-dlp
    # that TikTok/IG have already broken — which is exactly what happened.
    .run_commands("pip install --no-cache-dir --upgrade yt-dlp", force_build=True)
)

app = modal.App("video-watch-mcp", image=image)


def has_audio_stream(video_path: str) -> bool:
    """True if the downloaded file actually contains an audio stream."""
    probe = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_type",
        "-of", "json", video_path
    ], capture_output=True, text=True)
    try:
        return len(json.loads(probe.stdout).get("streams", [])) > 0
    except Exception:
        return False


def download_video(url: str, video_path: str) -> dict:
    """Download video with platform-aware fallbacks, return success/error."""
    is_tiktok = "tiktok.com" in url or "tiktok" in url.lower()

    # Format ladder. TikTok's bytevc1/h265 variants are frequently served
    # WITHOUT their audio track even though the format metadata claims aac —
    # the download succeeds and yields a mute file (verified 3 Jul 2026:
    # bytevc1_1080p = video-only in reality, h264_720p = real HE-AAC audio).
    # yt-dlp can't see the lie from metadata, so no selector alone is safe:
    # we verify the *downloaded file* with ffprobe and climb the ladder if
    # audio is missing. For TikTok, h264 first; watermarked "download" format
    # last (TikTok's own export — ugly but always has sound).
    general_fmt = "bestvideo*+bestaudio/best"
    if is_tiktok:
        fmts = [
            "b[vcodec^=h264]/bv*[vcodec^=h264]+ba",  # honest codec first
            general_fmt,                              # whatever merges
            "download",                               # watermarked, real audio
        ]
    else:
        fmts = [general_fmt]

    def attempts_for(fmt: str) -> list[list[str]]:
        base_args = [
            "yt-dlp",
            "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", video_path,
            "--no-playlist",
            "--socket-timeout", "30",
        ]
        # Strategy 1: browser impersonation (works for most platforms)
        attempts = [base_args + ["--impersonate", "chrome", url]]
        # Strategy 2: for TikTok, no impersonation but alternate API host
        if is_tiktok:
            attempts.append(base_args + [
                "--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com",
                url
            ])
        # Strategy 3: simplest possible — no impersonation
        attempts.append([
            "yt-dlp",
            "-f", fmt,
            "--merge-output-format", "mp4",
            "-o", video_path,
            "--no-playlist",
            url
        ])
        return attempts

    result = None
    mute_fallback = None  # best video-only download, in case nothing has audio
    for fmt in fmts:
        for attempt in attempts_for(fmt):
            if os.path.exists(video_path):
                os.remove(video_path)
            result = subprocess.run(attempt, capture_output=True, text=True)
            if result.returncode != 0:
                continue
            if has_audio_stream(video_path):
                return {"success": True}
            # Downloaded fine but mute — keep one copy as a last resort and
            # keep climbing the ladder for a version with sound.
            if mute_fallback is None:
                mute_fallback = video_path + ".mute.mp4"
                os.replace(video_path, mute_fallback)

    if mute_fallback is not None:
        os.replace(mute_fallback, video_path)
        return {"success": True, "warning": "no audio track available in any variant"}

    return {"success": False, "error": result.stderr[-500:] if result and result.stderr else "Unknown download error"}


def get_duration(video_path: str) -> float:
    """Get video duration in seconds."""
    probe = subprocess.run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", video_path
    ], capture_output=True, text=True)

    try:
        return float(json.loads(probe.stdout)["format"]["duration"])
    except:
        return 0


def extract_frames(video_path: str, output_dir: str, fps: float = 0.5, max_frames: int = 5) -> list[str]:
    """Extract frames from video, return as base64 list."""
    output_pattern = f"{output_dir}/frame_%04d.jpg"

    # Smaller frames (480px width), more compression
    subprocess.run([
        "ffmpeg", "-i", video_path,
        "-vf", f"fps={fps},scale=480:-1,drawtext=text='%{{pts\\:hms}}':x=10:y=10:fontsize=18:fontcolor=white:box=1:boxcolor=black@0.5",
        "-q:v", "6",
        output_pattern
    ], capture_output=True, check=True)

    frame_paths = sorted(Path(output_dir).glob("frame_*.jpg"))

    # Limit frames
    if len(frame_paths) > max_frames:
        step = len(frame_paths) / max_frames
        frame_paths = [frame_paths[int(i * step)] for i in range(max_frames)]

    frames_b64 = []
    for fp in frame_paths:
        with open(fp, "rb") as f:
            frames_b64.append(base64.b64encode(f.read()).decode("utf-8"))

    return frames_b64


def transcribe_audio(video_path: str, audio_path: str) -> str:
    """Extract and transcribe audio.

    Uses forgiving flags (-y, -f wav, -avoid_negative_ts make_zero) for
    TikTok/mobile mp4s that often have odd audio stream timing. On failure
    surfaces real ffmpeg stderr instead of a bare CalledProcessError.
    """
    import whisper

    # Extract audio
    result = subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-f", "wav",
        "-avoid_negative_ts", "make_zero",
        audio_path
    ], capture_output=True, text=True)
    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-600:]
        raise RuntimeError(f"ffmpeg audio extraction failed (exit {result.returncode}): {stderr_tail}")

    # Transcribe
    model = whisper.load_model("base")
    result = model.transcribe(audio_path)
    return result["text"]


@app.function(gpu="T4", timeout=300)
def process_listen(url: str):
    """Audio/transcript only - lightweight."""
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = f"{tmpdir}/video.mp4"
        audio_path = f"{tmpdir}/audio.wav"

        dl = download_video(url, video_path)
        if not dl["success"]:
            return {"success": False, "error": dl["error"]}

        duration = get_duration(video_path)

        try:
            transcript = transcribe_audio(video_path, audio_path)
        except Exception as e:
            transcript = f"[Transcription failed: {e}]"

        return {
            "success": True,
            "duration_seconds": duration,
            "transcript": transcript,
            "url": url
        }


@app.function(timeout=300)
def process_see(url: str, max_frames: int = 5):
    """Frames only - no GPU needed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = f"{tmpdir}/video.mp4"
        frames_dir = f"{tmpdir}/frames"
        Path(frames_dir).mkdir()

        dl = download_video(url, video_path)
        if not dl["success"]:
            return {"success": False, "error": dl["error"]}

        duration = get_duration(video_path)
        frames = extract_frames(video_path, frames_dir, fps=0.5, max_frames=max_frames)

        return {
            "success": True,
            "duration_seconds": duration,
            "frame_count": len(frames),
            "frames": frames,
            "url": url
        }


@app.function(gpu="T4", timeout=300)
def process_watch(url: str, max_frames: int = 5):
    """Full experience - frames + transcript."""
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = f"{tmpdir}/video.mp4"
        audio_path = f"{tmpdir}/audio.wav"
        frames_dir = f"{tmpdir}/frames"
        Path(frames_dir).mkdir()

        dl = download_video(url, video_path)
        if not dl["success"]:
            return {"success": False, "error": dl["error"]}

        duration = get_duration(video_path)
        frames = extract_frames(video_path, frames_dir, fps=0.5, max_frames=max_frames)

        try:
            transcript = transcribe_audio(video_path, audio_path)
        except Exception as e:
            transcript = f"[Transcription failed: {e}]"

        return {
            "success": True,
            "duration_seconds": duration,
            "frame_count": len(frames),
            "frames": frames,
            "transcript": transcript,
            "url": url
        }


@app.function()
@modal.asgi_app()
def mcp_server():
    """ASGI app that serves MCP over SSE."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.responses import JSONResponse
    from sse_starlette.sse import EventSourceResponse

    async def handle_sse(request):
        async def event_generator():
            yield {
                "event": "endpoint",
                "data": json.dumps({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized"
                })
            }
        return EventSourceResponse(event_generator())

    def format_duration(seconds: float) -> str:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{minutes}:{secs:02d}"

    async def handle_mcp(request):
        body = await request.json()
        method = body.get("method", "")
        params = body.get("params", {})
        request_id = body.get("id")

        if method == "initialize":
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "video-watch", "version": "2.0.0"}
                }
            })

        elif method == "tools/list":
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "video_listen",
                            "description": "Get transcript of a video. Lightweight - returns only the spoken/audio content as text. Best for: talking head videos, podcasts, commentary, interviews, tutorials with narration.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "url": {"type": "string", "description": "Video URL"}
                                },
                                "required": ["url"]
                            }
                        },
                        {
                            "name": "video_see",
                            "description": "Get visual frames from a video. Returns key frames as images, no audio transcription. Best for: dance videos, visual art, scenery, silent clips, memes, anything where visuals matter more than audio.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "url": {"type": "string", "description": "Video URL"},
                                    "max_frames": {"type": "integer", "description": "Max frames (default 5)", "default": 5}
                                },
                                "required": ["url"]
                            }
                        },
                        {
                            "name": "watch_video",
                            "description": "Full video experience - frames AND transcript. Uses more context but gives complete picture. Use when both visuals and audio matter.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "url": {"type": "string", "description": "Video URL"},
                                    "max_frames": {"type": "integer", "description": "Max frames (default 5)", "default": 5}
                                },
                                "required": ["url"]
                            }
                        }
                    ]
                }
            })

        elif method == "tools/call":
            tool_name = params.get("name")
            args = params.get("arguments", {})
            url = args.get("url")

            if not url:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": "Error: No URL provided"}]}
                })

            # Route to appropriate processor
            if tool_name == "video_listen":
                result = process_listen.remote(url)

                if not result.get("success"):
                    return JSONResponse({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}]}
                    })

                content = [{
                    "type": "text",
                    "text": f"**Video:** {url}\n**Duration:** {format_duration(result.get('duration_seconds', 0))}\n\n**Transcript:**\n{result.get('transcript', '[No transcript]')}"
                }]

            elif tool_name == "video_see":
                max_frames = min(args.get("max_frames", 5), 10)
                result = process_see.remote(url, max_frames)

                if not result.get("success"):
                    return JSONResponse({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}]}
                    })

                content = [{
                    "type": "text",
                    "text": f"**Video:** {url}\n**Duration:** {format_duration(result.get('duration_seconds', 0))}\n**Frames:** {result.get('frame_count', 0)}"
                }]

                for frame_b64 in result.get("frames", []):
                    content.append({"type": "image", "data": frame_b64, "mimeType": "image/jpeg"})

            elif tool_name == "watch_video":
                max_frames = min(args.get("max_frames", 5), 10)
                result = process_watch.remote(url, max_frames)

                if not result.get("success"):
                    return JSONResponse({
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"content": [{"type": "text", "text": f"Error: {result.get('error')}"}]}
                    })

                content = [{
                    "type": "text",
                    "text": f"**Video:** {url}\n**Duration:** {format_duration(result.get('duration_seconds', 0))}\n**Frames:** {result.get('frame_count', 0)}\n\n**Transcript:**\n{result.get('transcript', '[No transcript]')}"
                }]

                for frame_b64 in result.get("frames", []):
                    content.append({"type": "image", "data": frame_b64, "mimeType": "image/jpeg"})
            else:
                return JSONResponse({
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                })

            return JSONResponse({
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": content}
            })

        return JSONResponse({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        })

    async def health(request):
        return JSONResponse({"status": "ok", "service": "video-watch-mcp", "version": "2.0.0"})

    routes = [
        Route("/", handle_mcp, methods=["POST"]),
        Route("/sse", handle_sse),
        Route("/health", health),
    ]

    return Starlette(routes=routes)
