# Video Watch MCP

Let Claude "watch" videos with you. Send a TikTok, YouTube, or any video link - Claude sees the frames and reads the transcript.

**Fully cloud-hosted.** No local processing. Works on Claude Desktop, Claude mobile, anywhere MCP works.

## What it does

Three tools, pick based on content:

| Tool | Returns | Best for |
|------|---------|----------|
| `video_listen` | Transcript only | Talking heads, podcasts, commentary |
| `video_see` | Frames only | Dance, visual art, memes, scenery |
| `watch_video` | Both | When audio AND visuals both matter |

1. You send Claude a video link
2. Claude picks the right tool (or you tell it which)
3. Cloud service downloads, extracts what's needed
4. Claude receives just what it needs - no context bloat
5. You watch it "together"

## Quick Start (5 minutes)

### 1. Create a Modal account

Go to [modal.com](https://modal.com) and sign up. Free tier gives you $30/month in credits - enough for thousands of short videos.

### 2. Install Modal CLI

```bash
pip install modal
modal token set --token-id YOUR_TOKEN_ID --token-secret YOUR_TOKEN_SECRET
```

(Get your token from Modal's dashboard after signup)

### 3. Deploy

```bash
git clone https://github.com/nekyialabs/video-watch-mcp.git
cd video-watch-mcp
modal deploy mcp_remote.py
```

You'll get a URL like: `https://yourusername--video-watch-mcp-mcp-server.modal.run`

### 4. Add to Claude Desktop

Edit your `claude_desktop_config.json`:

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
**Mac:** `~/Library/Application Support/Claude/claude_desktop_config.json`

Add under `mcpServers`:

```json
{
  "mcpServers": {
    "video-watch": {
      "url": "https://yourusername--video-watch-mcp-mcp-server.modal.run"
    }
  }
}
```

### 5. Use it

Restart Claude Desktop. Send any video link and ask Claude to watch it:

> "Watch this with me: https://tiktok.com/..."

Claude will see the frames and read the transcript.

## Supported Platforms

Anything [yt-dlp](https://github.com/yt-dlp/yt-dlp) supports:

- TikTok
- YouTube
- Instagram Reels
- Twitter/X videos
- Reddit videos
- Facebook
- Vimeo
- And [1000+ more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

## Cost

With Modal's free tier ($30/month credits):

| Video Length | Approx. Cost | Videos per Month |
|--------------|--------------|------------------|
| 30 sec       | ~$0.002      | ~15,000          |
| 5 min        | ~$0.01       | ~3,000           |
| 30 min       | ~$0.05       | ~600             |

You'll never hit the limit with normal use.

## How it works

```
You send a link
       ↓
Claude calls watch_video(url)
       ↓
Modal spins up a container with ffmpeg + whisper
       ↓
yt-dlp downloads the video
       ↓
ffmpeg extracts frames (with timestamps burned in)
       ↓
Whisper transcribes the audio
       ↓
Returns frames as images + transcript text
       ↓
Claude sees everything, you discuss it together
```

## Files

- `mcp_remote.py` - The full MCP server (deploy this)
- `video_watch.py` - Standalone video processor with web endpoint (if you just want the API)

## Configuration

In `mcp_remote.py` you can adjust:

- `fps` - Frames per second to extract (default 0.5 = one frame every 2 seconds)
- `max_frames` - Maximum frames to return (default 10, max 20)
- `whisper model` - Using "base" for speed, can use "small" or "medium" for accuracy

## Updating

Pull the latest code and redeploy — that's it:

```bash
cd video-watch-mcp
git pull
modal deploy mcp_remote.py
```

Your MCP URL stays the same, no client config changes needed.

**Coming from the old `maryfellowes/video-watch-mcp` repo?** That repo has been removed. Update your remote:

```bash
git remote set-url origin https://github.com/nekyialabs/video-watch-mcp.git
git pull
modal deploy mcp_remote.py
```

## Limitations

- Very long videos (30+ min) may timeout
- Audio-only content won't have frames (obviously)
- Some DRM-protected content won't download
- Whisper transcription is good but not perfect

## Privacy

- Videos are processed in ephemeral containers - nothing stored
- No logs of what you watch
- Your Modal account, your data

## License

MIT - do whatever you want with it.

---

Built by [Codependent AI](https://codependentai.io).

## Support

Built by [Codependent AI](https://codependentai.io).

<a href="https://ko-fi.com/codependentai"><img src="https://img.shields.io/badge/Ko--fi-Support%20Us-ff5e5b?logo=ko-fi&logoColor=white" alt="Ko-fi" /></a>
