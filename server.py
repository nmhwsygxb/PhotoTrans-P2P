"""
PhotoTrans-P2P 电脑端 — WebRTC 信令服务器

功能：
- HTTP 服务：/  服务前端页面, /api/files 文件列表 API
- WebSocket 信令：/ws  接收浏览器 WebRTC offer/answer/ice
- WebRTC 电脑端（aiortc）：与浏览器建立 P2P 数据通道

依赖：
    pip install aiohttp aiortc

运行：
    python server.py --root "你的网盘目录" --port 8080

部署：
    cloudflared tunnel --no-autoupdate route dns \\
        --hostname phototrans.trycloudflare.com \\
        --service tcp://localhost:8080
    手机浏览器打开 https://phototrans.trycloudflare.com
"""
import argparse
import asyncio
import json
import os
import signal
import socket
import sys
from pathlib import Path

from aiohttp import web
from aiohttp.web_ws import WSMsgType
from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    RTCIceServer,
    RTCIceCandidate,
    RTCConfiguration,
)

# 诊断: 开启 aioice / aiortc 的详细日志（打洞过程）
import logging
logging.basicConfig(level=logging.DEBUG, format="%(name)s %(levelname)s %(message)s")
for lg in ["aioice", "aiortc"]:
    logging.getLogger(lg).setLevel(logging.DEBUG)

# ============ 全局配置 ============

ROOT = Path.cwd()
PORT = 8080

STUN_SERVERS = [
    # Cloudflare STUN —— 国内可达（l.google.com 在境内无 IPv4 记录，必须换）
    RTCIceServer(urls="stun:162.159.207.0:3478"),
    RTCIceServer(urls="stun:stun.cloudflare.com:3478"),
]

def make_config():
    """构造 RTCConfiguration 对象（aiortc 1.15 要求对象而非 dict）。"""
    return RTCConfiguration(iceServers=STUN_SERVERS)

peers = {}  # 活跃连接计数


def parse_candidate(s: str, sdp_mid, sdp_mlindex):
    """把 candidate 字符串解析成 aiortc RTCIceCandidate 组件化字段。

    格式: candidate:<foundation> <component> <protocol> <priority> <ip> <port> typ <type> ...
    """
    parts = s.split()
    if not parts or not parts[0].startswith("candidate:"):
        return None
    try:
        foundation = parts[0].split(":", 1)[1]
        component = int(parts[1])
        protocol = parts[2]
        priority = int(parts[3])
        ip = parts[4]
        port = int(parts[5])
        cand_type = parts[7] if len(parts) > 7 else "host"
        # 可选: raddr <ip> rport <port>
        related_address = None
        related_port = None
        i = 8
        while i < len(parts):
            if parts[i] == "raddr" and i + 1 < len(parts):
                related_address = parts[i + 1]
            elif parts[i] == "rport" and i + 1 < len(parts):
                related_port = int(parts[i + 1])
            i += 1
        return RTCIceCandidate(
            component=component,
            foundation=foundation,
            ip=ip,
            port=port,
            priority=priority,
            protocol=protocol,
            type=cand_type,
            relatedAddress=related_address,
            relatedPort=related_port,
            sdpMid=sdp_mid,
            sdpMLineIndex=sdp_mlindex,
        )
    except (ValueError, IndexError) as e:
        print(f"[ICE解析失败] {s[:60]} err={e}")
        return None


# ============ HTTP 路由 ============

async def http_index(request):
    """服务前端页面（static/index.html）。"""
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html")
    return web.Response(
        text="<h1>PhotoTrans-P2P</h1><p>static/index.html 不存在</p>",
        content_type="text/html",
    )


async def http_files(request):
    """文件列表 API — 返回根目录下的所有文件。"""
    files = []
    try:
        for p in ROOT.iterdir():
            if p.is_file() and not p.name.startswith("."):
                files.append({
                    "name": p.name,
                    "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"files": files, "root": str(ROOT)})


async def http_download(request):
    """文件下载 — 经 CF Tunnel HTTP 中继传输（打洞失败也可靠）。"""
    filename = request.match_info["filename"]
    root_real = ROOT.resolve()
    target = (root_real / filename).resolve()
    # 路径穿越防护：只允许根目录内的文件
    if not target.is_relative_to(root_real) or not target.is_file():
        return web.Response(status=404, text="Not Found")
    return web.FileResponse(target)


# ============ WebSocket 信令 ============

async def handle_ws(request):
    """WebSocket 信令 — 与浏览器交换 WebRTC SDP/ICE。"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # 每个 WebSocket 连接对应一个 RTCPeerConnection
    pc = RTCPeerConnection(configuration=make_config())
    peer_id = id(pc)
    peers[peer_id] = pc

    # 监听 ICE 连接状态变化，推送给浏览器
    @pc.on("iceconnectionstatechange")
    async def on_ice_change():
        try:
            state = pc.iceConnectionState
            print(f"[PC] ICE 状态变化: {state}")
            await ws.send_str(json.dumps({"type": "ice-state", "state": state}))
        except Exception:
            pass

    # 电脑端自己的 ICE 候选收集（诊断用）
    @pc.on("icecandidate")
    async def on_icecandidate(candidate):
        try:
            if candidate:
                print(f"[PC候选] {candidate.candidate[:90]}")
            else:
                print("[PC候选] 收集完成")
        except Exception:
            pass

    # 浏览器创建 data channel 时，通知浏览器
    @pc.on("datachannel")
    async def on_datachannel(channel):
        try:
            await ws.send_str(json.dumps({
                "type": "channel-open",
                "label": channel.label,
            }))
        except Exception:
            pass
        # 接收 data channel 消息（测试用 echo）
        @channel.on("message")
        async def on_message(msg):
            try:
                await ws.send_str(json.dumps({"type": "echo", "msg": msg}))
            except Exception:
                pass

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    t = data.get("type")
                    print(f"[WS] 收到类型: {t}")

                    if t == "offer":
                        # 浏览器创建 offer → 我们设为远端描述 → 创建 answer 发回
                        print("[WS] 收到 offer, 设置远端描述...")
                        await pc.setRemoteDescription(
                            RTCSessionDescription(sdp=data["sdp"], type="offer")
                        )
                        print("[WS] 创建 answer...")
                        answer = await pc.createAnswer()
                        await pc.setLocalDescription(answer)
                        print(f"[WS] 发送 answer (ice={pc.iceGatheringState})")
                        # 打印 answer 里的候选行（诊断）
                        for line in (pc.localDescription.sdp or "").splitlines():
                            if line.startswith("a=candidate") or line.startswith("m="):
                                print(f"[answerSDP] {line[:110]}")
                        await ws.send_str(json.dumps({
                            "type": "answer",
                            "sdp": pc.localDescription.sdp,
                            "desc_type": pc.localDescription.type,
                        }))

                    elif t == "answer":
                        # 浏览器回 answer（如果我们在 offer）
                        print("[WS] 收到 answer, 设置远端描述...")
                        await pc.setRemoteDescription(
                            RTCSessionDescription(sdp=data["sdp"], type=data["type"])
                        )

                    elif t == "ice":
                        # 交换 ICE 候选（candidate 字符串解析为组件化字段）
                        print(f"[WS] 收到 ICE 候选: {str(data.get('candidate'))[:60]}")
                        cand = parse_candidate(
                            data.get("candidate", ""),
                            data.get("sdpMid"),
                            data.get("sdpMLineIndex"),
                        )
                        if cand:
                            await pc.addIceCandidate(cand)
                        else:
                            print("[WS] ICE 候选解析失败, 跳过")

                except Exception as e:
                    print(f"[信令错误] {e}")

    except Exception:
        pass
    finally:
        await pc.close()
        peers.pop(peer_id, None)

    return ws


# ============ 启动 ============

async def main():
    app = web.Application()
    app.router.add_get("/", http_index)
    app.router.add_get("/api/files", http_files)
    app.router.add_get("/download/{filename}", http_download)
    app.router.add_get("/ws", handle_ws)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    print("=" * 50)
    print("PhotoTrans-P2P 启动")
    print(f"  共享目录: {ROOT}")
    print(f"  本机地址: http://localhost:{PORT}")
    # 自动打印局域网地址（手机连同一 WiFi 时直接访问）
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
        print(f"  局域网:   http://{lan_ip}:{PORT}  （手机连同一网络时用这个，速度最快）")
    except Exception:
        pass
    print(f"  公网:     需要另开 start_public.bat（Cloudflare 隧道，外面也能访问）")
    print("=" * 50)

    # 优雅退出
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set_result, None)
        except NotImplementedError:
            pass  # Windows 不支持信号

    await stop
    await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PhotoTrans-P2P 电脑端")
    parser.add_argument("--root", help="网盘根目录（默认读 config.json 的 share_dir）")
    parser.add_argument("--port", type=int, help="服务端口（默认读 config.json 的 port）")
    parser.add_argument("--config", default="config.json", help="配置文件路径（默认 ./config.json）")
    args = parser.parse_args()

    # 读配置：config.json 提供默认值，命令行参数优先
    # 配置文件始终相对脚本目录查找，与启动时的当前目录无关
    ROOT = None
    PORT = None
    cfg_path = Path(__file__).parent / args.config
    if not cfg_path.exists():
        print(f"[配置] 未找到 {cfg_path.name}，使用默认值。可复制 {cfg_path.name[:-5]}.example.json 为 {cfg_path.name} 自定义。")
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            ROOT = Path(cfg.get("share_dir", "")).expanduser() if cfg.get("share_dir") else None
            PORT = int(cfg.get("port", 0)) or None
        except Exception as e:
            print(f"[配置] 读取 {cfg_path} 失败，使用命令行参数: {e}")

    if args.root:
        ROOT = Path(args.root)
    if args.port:
        PORT = args.port

    # 仍未配置 → 使用 ./share 目录（不存在则创建）
    if ROOT is None:
        ROOT = Path(__file__).parent / "share"
    if not ROOT.exists():
        ROOT.mkdir(parents=True, exist_ok=True)
        print(f"[配置] 共享目录不存在，已自动创建: {ROOT}")
    if not ROOT.is_dir():
        print(f"[错误] 共享目录不是文件夹: {ROOT}")
        sys.exit(1)

    if PORT is None:
        PORT = 8082

    asyncio.run(main())
