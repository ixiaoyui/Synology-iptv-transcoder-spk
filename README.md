# IPTV Transcoder for Synology DSM

用于在黑群晖/群晖 DSM 宿主机上提供 IPTV 按需转码服务，解决浏览器播放问题：

1. **隔行扫描横纹**：1080i/TFF/BFF 源通过 QSV VPP 去隔行后输出 HLS。
2. **只有声音没有画面**：HEVC/H.265、MPEG-2 Video 等浏览器/MSE 不兼容源通过 QSV 转成 H.264 HLS。
3. **HDR10/HLG 颜色不对或浏览器不兼容**：仅在 **HEVC → H.264** 且源带 HDR transfer 时做 HDR→SDR，输出 BT.709。

当前包版本：`0.1.0-055`（`spk/INFO`）。HTTP 服务内部字符串为 `0.2.0`，以 SPK 包版本为准。

## 硬件链路（源码实际行为）

默认针对 Intel UHD630 / QSV，**全硬件**（无 CPU fallback）：

```text
Jellyfin ffmpeg
  -> QSV 硬解 h264_qsv / hevc_qsv / mpeg2_qsv
  -> QSV VPP vpp_qsv（可选 deinterlace=2 / 缩放 / format=nv12）
  -> 仅 HDR 时额外做色调映射（见下节双路径）
  -> QSV 硬编 h264_qsv
  -> HLS 输出
```

- 默认设备：`/dev/dri/renderD128`
- `IPTV_TRANSCODER_HARDWARE_ONLY=1` 表示只走 QSV；当前实现也没有软件转码路径
- ffmpeg/ffprobe 默认使用 Jellyfin 包内二进制

### HDR / HLG → SDR 色调映射（双路径）

**触发条件（同时满足）：**

1. operation 属于：
   - `qsv_hevc_to_h264`
   - `qsv_deinterlace_hevc_to_h264`
   - `qsv_hevc_to_h264_1080p`
2. `color_transfer` 属于：`arib-std-b67` / `smpte2084` / `hlg` / `pq`

**不会 tonemap 的情况：**

- 仅有 `bt2020` primaries/space、但没有 HDR transfer
- H.264 / MPEG-2 等非上述 HEVC operation
- 调用方没带 `color_transfer`，且服务侧因其它元数据已齐而跳过 probe 补齐时（会漏映射）

**位深分流（`core.py`）：**

| 源特征 | 滤镜路径 | 说明 |
|--------|----------|------|
| HDR + **8bit**（非 Main10 / 非 p010） | `vpp_qsv=...:format=nv12:out_color_*=bt709:tonemap=1:procamp=1:brightness=...:contrast=...` | 本平台 8bit HLG 不能直接走 OpenCL tonemap |
| HDR + **10bit**（profile/pix_fmt 含 `10` 或 `p010*`） | `setparams` → `vpp_qsv`（缩放/去隔行）→ `hwmap(opencl)` → `tonemap_opencl=bt2390:peak=100:desat=0` → `hwmap(qsv)` | 接近 Jellyfin 的 10bit HDR 路径 |

HDR 输出额外固定：

- 颜色元数据：`tv` / `bt709` / `bt709` / `bt709`
- `-bf 0`（关闭 B 帧，利于浏览器硬解）
- 音频强制 AAC 2ch 48kHz（即使源已是 AAC）

VPP 路径可调：

| 环境变量 | 代码默认 | 作用 |
|----------|----------|------|
| `IPTV_TRANSCODER_HDR_VPP_BRIGHTNESS` | `8` | 8bit VPP tonemap 后亮度（-100~100） |
| `IPTV_TRANSCODER_HDR_VPP_CONTRAST` | `1` | 8bit VPP tonemap 后对比度（0~10） |

若输出偏灰，可先试 `brightness=16` 或 `20`；过亮则降到 `8` 以下。这两项只影响 **8bit VPP** 路径，不影响 10bit OpenCL 路径。

### OpenCL 运行时依赖（仅 10bit HDR）

服务启动脚本会尽量继承 Jellyfin 包内：

- `LD_LIBRARY_PATH`
- `OCL_ICD_VENDORS` / `OPENCL_VENDOR_PATH`
- `LIBVA_DRIVERS_PATH`

这样 10bit 的 `tonemap_opencl=bt2390` 能用到 Jellyfin 自带的 Intel OpenCL / VA 驱动。  
**8bit VPP tonemap 不依赖 OpenCL。** Jellyfin 升级或卸载可能只影响 10bit HDR 路径。

### 分辨率

- `resolution=auto`：默认不改分辨率；源 ≥4K 时会把输出规格落到 4K 档（码率阶梯等）
- 显式 `720p` / `1080p` / `2k` / `4k`：由 `vpp_qsv` 缩放
- `qsv_hevc_to_h264_1080p`：固定缩到 1080p
- **色调映射本身不改分辨率**

## 安装

1. 在 DSM 套件中心选择“手动安装”。
2. 上传 `iptv-transcoder-0.1.0-055-x86_64.spk`。
3. 安装完成后编辑：

```text
/var/packages/iptv-transcoder/var/env
```

### 代码/套件启动默认值（未写 env 时）

与 `core.py` / `spk/scripts/start-stop-status` 一致：

| 变量 | 默认 |
|------|------|
| `IPTV_TRANSCODER_MAX_TRANSCODES` | `5` |
| `IPTV_TRANSCODER_IDLE_TIMEOUT` | `10`（秒） |
| `IPTV_TRANSCODER_HLS_TIME` | `2` |
| `IPTV_TRANSCODER_HLS_GOP` | `100` |
| `IPTV_TRANSCODER_QSV_LOW_POWER_H264` | `1` |
| `IPTV_TRANSCODER_HDR_VPP_BRIGHTNESS` | `8` |
| `IPTV_TRANSCODER_HDR_VPP_CONTRAST` | `1` |
| `IPTV_TRANSCODER_ALLOWED_UPSTREAMS` | `192.168.1.1:7088` |

### 生产建议（UHD630 与 Jellyfin 共用 GPU 时）

建议在 env 里显式写上更保守的值，而不是依赖上述代码默认：

```text
IPTV_TRANSCODER_PUBLIC_BASE_URL=http://NAS_IP:18096
IPTV_TRANSCODER_FFMPEG=/var/packages/Jellyfin/target/bin/ffmpeg
IPTV_TRANSCODER_FFPROBE=/var/packages/Jellyfin/target/bin/ffprobe
IPTV_TRANSCODER_QSV_DEVICE=/dev/dri/renderD128
IPTV_TRANSCODER_ALLOWED_UPSTREAMS=192.168.1.1:7088
IPTV_TRANSCODER_QSV_LOW_POWER_H264=1
IPTV_TRANSCODER_MAX_TRANSCODES=1
IPTV_TRANSCODER_IDLE_TIMEOUT=90
IPTV_TRANSCODER_HDR_VPP_BRIGHTNESS=8
IPTV_TRANSCODER_HDR_VPP_CONTRAST=1
```

把 `NAS_IP` 改为黑群晖宿主机 IP，例如：

```text
IPTV_TRANSCODER_PUBLIC_BASE_URL=http://192.168.1.100:18096
```

如果 IPTV 上游不是 `192.168.1.1:7088`，修改：

```text
IPTV_TRANSCODER_ALLOWED_UPSTREAMS=实际IP:端口
```

然后在 DSM 套件中心重启 IPTV Transcoder。完整示例见 `examples/env.example`。

## API

所有 `/api/*` 请求需要：

```text
X-API-Key: <与 /var/packages/iptv-transcoder/var/env 中 IPTV_TRANSCODER_API_KEY 相同>
```

### 动态启动转码

IPTVWeb 推荐使用动态接口传入当前频道 URL 和固定白名单操作：

```bash
curl -X POST 'http://NAS_IP:18096/api/transcode/start' \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_KEY' \
  -d '{
    "channel_id": "cctv4k",
    "input_url": "http://192.168.1.1:7088/rtp/239.1.1.1:5002",
    "operation": "qsv_hevc_to_h264",
    "color_transfer": "arib-std-b67",
    "color_primaries": "bt2020",
    "color_space": "bt2020nc",
    "video_profile": "Main 10",
    "pix_fmt": "yuv420p10le",
    "width": 3840,
    "height": 2160
  }'
```

HDR 频道请尽量带上 `color_transfer`（以及 profile / pix_fmt）。  
服务会在缺少部分元数据时自动 ffprobe 补齐；但若 codec/宽高/fps 已齐而 **唯独缺少 color_transfer**，当前实现可能跳过补齐，导致不 tonemap。

返回示例：

```json
{
  "ok": true,
  "channel_id": "cctv4k",
  "status": "started",
  "operation": "qsv_hevc_to_h264",
  "hls_url": "http://NAS_IP:18096/hls/cctv4k/master.m3u8"
}
```

### 固定操作白名单

```text
qsv_h264
qsv_deinterlace
qsv_hevc_to_h264
qsv_deinterlace_hevc_to_h264
qsv_mpeg2_to_h264
qsv_mpeg2_deinterlace_to_h264
qsv_hevc_to_h264_1080p
```

服务不接受任意 ffmpeg 参数。

可选常用字段：

| 字段 | 说明 |
|------|------|
| `resolution` | `auto` / `720p` / `1080p` / `2k` / `4k` |
| `quality_preset` | `default` / `low` / `medium` / `high`（低功耗码率阶梯） |
| `global_quality` | 1–51；仅在关闭 low_power 时进入 `-global_quality` |
| `force_aac` | 强制 AAC；HDR tonemap 时也会强制 AAC |
| `width` / `height` / `fps` / 颜色字段 | 供 filter / HLS GOP / HDR 判定使用 |

### 心跳和停止

```bash
curl -X POST -H 'X-API-Key: YOUR_KEY' http://NAS_IP:18096/api/transcode/cctv4k/heartbeat
curl -X POST -H 'X-API-Key: YOUR_KEY' http://NAS_IP:18096/api/transcode/cctv4k/stop
```

- 代码默认：`IDLE_TIMEOUT=10` 秒无心跳就停该路 ffmpeg
- 生产建议：写成 `90`，避免页面短暂失去焦点就杀任务

同一 `channel_id` 且 job 参数相同：再次 `start` 会复用已有进程并刷新心跳，不会重复起 ffmpeg。

### 探测频道

```bash
curl -X POST 'http://NAS_IP:18096/api/probe' \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: YOUR_KEY' \
  -d '{"channel_id":"demo","input_url":"http://192.168.1.1:7088/rtp/239.1.1.1:5002"}'
```

返回里会有 `input.color_transfer`、`hardware_plan.filter`、`suggested_operation` 等。  
看 `hardware_plan.filter` 是否含 `tonemap=1` 或 `tonemap_opencl=`，是确认色调映射是否启用的最快方法。

## 资源和日志

```text
/var/packages/iptv-transcoder/var/hls
/var/packages/iptv-transcoder/var/logs/service.log
/var/packages/iptv-transcoder/var/logs/<channel_id>.log
```

- `service.log` 超过 5MiB 时在启动前轮转为 `service.log.1`
- 单频道 ffmpeg 日志超过 10MiB 时在下一次启动该频道前轮转为 `<channel_id>.log.1`
- 每次启动同一频道前会清理该频道旧 HLS 分片；输出使用 `delete_segments`
- 频道日志开头有 `cmd: ...` 行，可直接核对真实 ffmpeg 命令与 filter
- 自动 HLS 时机按源帧率推算：`25fps -> 目标 2s / GOP 50`，`50fps -> 目标 2s / GOP 100`（受 `HLS_TIME` / `HLS_GOP` 约束）
- `IPTV_TRANSCODER_HLS_TIME`：自动模式目标分片秒数
- `IPTV_TRANSCODER_HLS_GOP`：自动模式 GOP 上限
- 请求里显式传 `hls_time` / `gop` / `keyint_min` 时优先用手写值
- `IPTV_TRANSCODER_QSV_LOW_POWER_H264=1` 时：`h264_qsv` 加 `-low_power 1`，并按分辨率 + `low/medium/high` 使用 `b:v/maxrate/bufsize`，**不再**使用 `global_quality`
- 管理界面可调 `720p / 1080p / 2K / 4K` × `low / medium / high` 三档码率

## 安全边界

- 默认监听 `0.0.0.0:18096` / 管理口 `18097`，只在内网使用，不要公网暴露
- API 需要 Key；Key 未配置时鉴权 fail-closed
- 不接受任意 ffmpeg 参数，只接受固定 operation 白名单
- `channel_id` 仅允许 ASCII 字母/数字/点/下划线/短横线
- 默认上游白名单 `IPTV_TRANSCODER_ALLOWED_UPSTREAMS=192.168.1.1:7088`；空列表拒绝所有输入；`*` 才表示放开（不推荐）
- 代码默认最大并发 **5** 路；UHD630 上建议生产改为 **1**（必要时再试 2）
- 卸载默认保留 `/var/packages/iptv-transcoder/var`，避免误删配置和日志

## 故障排查

检查 QSV 设备：

```bash
ls -l /dev/dri/
```

检查 Jellyfin ffmpeg QSV / OpenCL 相关能力：

```bash
/var/packages/Jellyfin/target/bin/ffmpeg -hide_banner -decoders | grep qsv
/var/packages/Jellyfin/target/bin/ffmpeg -hide_banner -encoders | grep qsv
/var/packages/Jellyfin/target/bin/ffmpeg -hide_banner -filters | grep -E 'qsv|tonemap'
```

转码失败优先看：

```text
/var/packages/iptv-transcoder/var/logs/<channel_id>.log
/var/packages/iptv-transcoder/var/logs/service.log
```

常见核对点：

1. `cmd:` 是否包含期望的 `tonemap=1`（8bit）或 `tonemap_opencl=bt2390`（10bit）
2. 是否带上了正确的 `color_transfer`
3. 10bit 失败时，Jellyfin OpenCL / `LD_LIBRARY_PATH` 是否仍可用
4. 是否被 `MAX_TRANSCODES` 打回 429，或被过短的 `IDLE_TIMEOUT` 误杀

## Smoke 验收

仓库提供可在真实运行环境使用的 API smoke 脚本：

```bash
tools/smoke_api.sh --base-url http://NAS_IP:18096 --api-key KEY
```

连通探测：

```bash
tools/smoke_api.sh \
  --base-url http://NAS_IP:18096 \
  --api-key KEY \
  --input-url http://192.168.1.1:7088/rtp/239.1.1.1:5002 \
  --channel-id smoke-demo
```

脚本会检查：

1. 根健康信息 `/`
2. 详细健康信息 `/api/health/details`
3. 配置接口 `/api/config`
4. 状态接口 `/api/status`
5. 可选探测接口 `/api/probe`

验证一轮真实转码生命周期：

```bash
tools/smoke_api.sh \
  --base-url http://NAS_IP:18096 \
  --api-key KEY \
  --input-url http://192.168.1.1:7088/rtp/239.1.1.1:5002 \
  --channel-id smoke-demo \
  --exercise-transcode
```

这会额外检查：

1. `POST /api/transcode/start`
2. `POST /api/transcode/<id>/heartbeat`
3. `POST /api/transcode/<id>/stop`

说明：

- 刚启动后的 heartbeat 可能短暂返回 `starting`、`missing_playlist`、`empty_playlist` 或 `missing_segments`
- `tools/smoke_api.sh` 会把这些视为正常暖机状态并短暂重试
- 如果 smoke 在转码生命周期检查中途失败，脚本会 best-effort 调用一次 `stop`，避免验收任务残留

Web 侧联动验收见配套项目 `iptv-web` 的 `tools/README.smoke.md`。
