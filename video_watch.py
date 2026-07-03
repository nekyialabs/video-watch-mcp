"""
Video Watch - Modal service that processes videos for Claude to "watch"
Downloads video, extracts frames, transcribes audio, returns everything.
"""

import modal
import base64
import tempfile
import subprocess
import json
from pathlib import Path

# Define the container image with all our dependencies
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "curl")
    .pip_install(
        "curl_cffi",  # For browser impersonation (TikTok, Instagram, etc.)
        "brotli",     # For compression support
        "openai-whisper",
        "torch",
        "fastapi[standard]",
    )
    # Install yt-dlp last and force latest — platforms break old versions constantly
    .run_commands("pip install --no-cache-dir --upgrade yt-dlp")
)

app = modal.App("video-watch", image=image)


def extract_frames(video_path: str, output_dir: str, fps: float = 0.5) -> list[str]:
    """Extract frames from video at specified fps. Returns list of frame paths."""
    output_pattern = f"{output_dir}/frame_%04d.jpg"

    # Add timestamp overlay to frames
    subprocess.run([
        "ffmpeg", "-i", video_path,
        "-vf", f"fps={fps},drawtext=text='%{{pts\\:hms}}':x=10:y=10:fontsize=24:fontcolor=white:box=1:boxcolor=black@0.5",
        "-q:v", "2",
        output_pattern
    ], capture_output=True, check=True)

    # Return sorted list of frame paths
    frames = sorted(Path(output_dir).glob("frame_*.jpg"))
    return [str(f) for f in frames]


def extract_audio(video_path: str, output_path: str) -> str:
    """Extract audio from video as wav file.

    Uses forgiving flags to handle TikTok/mobile mp4s that sometimes have
    odd audio stream timing or muxing. Logs ffmpeg stderr on failure so
    the caller (or logs) can see the exact cause rather than just a
    non-zero exit.
    """
    result = subprocess.run([
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-f", "wav",
        "-avoid_negative_ts", "make_zero",
        output_path
    ], capture_output=True, text=True)
    if result.returncode != 0:
        stderr_tail = (result.stderr or "")[-600:]
        raise RuntimeError(f"ffmpeg audio extraction failed (exit {result.returncode}): {stderr_tail}")
    return output_path


@app.function(gpu="T4", timeout=300)
def process_video(url: str, fps: float = 0.5, max_frames: int = 20):
    """
    Download and process a video URL.
    Returns frames as base64 and transcript.

    Args:
        url: Video URL (TikTok, YouTube, etc.)
        fps: Frames per second to extract (default 0.5 = 1 frame every 2 seconds)
        max_frames: Maximum number of frames to return
    """
    import whisper

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = f"{tmpdir}/video.mp4"
        audio_path = f"{tmpdir}/audio.wav"
        frames_dir = f"{tmpdir}/frames"
        Path(frames_dir).mkdir()

        # Download video with yt-dlp (multi-strategy with fallbacks)
        is_tiktok = "tiktok.com" in url or "tiktok" in url.lower()
        fmt = "bv*[height<=720]+ba/b[height<=720]/bv+ba/b"

        base_args = [
            "yt-dlp", "-f", fmt, "--merge-output-format", "mp4",
            "-o", video_path, "--no-playlist", "--socket-timeout", "30",
        ]

        # Try with browser impersonation first
        result = subprocess.run(base_args + ["--impersonate", "chrome", url], capture_output=True, text=True)

        # TikTok fallback: try extractor args
        if result.returncode != 0 and is_tiktok:
            result = subprocess.run(base_args + [
                "--extractor-args", "tiktok:api_hostname=api22-normal-c-useast2a.tiktokv.com", url
            ], capture_output=True, text=True)

        # Final fallback: simplest possible
        if result.returncode != 0:
            result = subprocess.run([
                "yt-dlp", "-f", "best", "-o", video_path, "--no-playlist", url
            ], capture_output=True, text=True)

        if result.returncode != 0:
            return {
                "success": False,
                "error": f"Download failed: {result.stderr[-500:] if result.stderr else 'Unknown error'}"
            }

        # Get video duration
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", video_path
        ], capture_output=True, text=True)

        duration = 0
        try:
            duration = float(json.loads(probe.stdout)["format"]["duration"])
        except:
            pass

        # Extract frames
        frame_paths = extract_frames(video_path, frames_dir, fps)

        # Limit frames
        if len(frame_paths) > max_frames:
            # Sample evenly
            step = len(frame_paths) / max_frames
            frame_paths = [frame_paths[int(i * step)] for i in range(max_frames)]

        # Convert frames to base64
        frames_b64 = []
        for fp in frame_paths:
            with open(fp, "rb") as f:
                frames_b64.append(base64.b64encode(f.read()).decode("utf-8"))

        # Extract and transcribe audio
        transcript = ""
        try:
            extract_audio(video_path, audio_path)
            model = whisper.load_model("base")  # Use base model for speed
            result = model.transcribe(audio_path)
            transcript = result["text"]
        except Exception as e:
            transcript = f"[Transcription failed: {e}]"

        return {
            "success": True,
            "duration_seconds": duration,
            "frame_count": len(frames_b64),
            "frames": frames_b64,  # List of base64 JPEG images
            "transcript": transcript,
            "url": url
        }


@app.function()
@modal.fastapi_endpoint(method="POST")
def watch(request: dict):
    """
    Web endpoint to process a video.
    POST with {"url": "https://...", "fps": 0.5, "max_frames": 20}
    """
    url = request.get("url")
    if not url:
        return {"success": False, "error": "No URL provided"}

    fps = request.get("fps", 0.5)
    max_frames = request.get("max_frames", 20)

    # Call the GPU function
    result = process_video.remote(url, fps, max_frames)
    return result


@app.local_entrypoint()
def main(url: str = ""):
    """Test locally with: modal run video_watch.py --url 'https://...'"""
    if not url:
        print("Usage: modal run video_watch.py --url 'VIDEO_URL'")
        return

    print(f"Processing: {url}")
    result = process_video.remote(url)

    if result["success"]:
        print(f"Duration: {result['duration_seconds']:.1f}s")
        print(f"Frames: {result['frame_count']}")
        print(f"Transcript: {result['transcript'][:500]}...")
    else:
        print(f"Error: {result['error']}")
