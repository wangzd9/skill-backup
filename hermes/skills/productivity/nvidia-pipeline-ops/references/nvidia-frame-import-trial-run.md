# 对 A100 已有 NVIDIA 产物手动试跑 FRAME_IMPORT 导入

不跑真实 pipeline run，直接调 `scripts/nvidia_frame_importer.import_nvidia_frames` 验证导入器行为：复用生产 ssh 语义读远端文件，帧写本地测试栈 backend（`local_stack.sh --mode mock up` 起，backend 端口 18080）。适合"能不能直接对已有数据跑一遍导入流程"类验证。

## ssh 包装器必须复刻 run_ssh 语义（最易踩）
- 连接参数取**处理机生产 proxy** 的口径：`host="211.141.18.194"`、`port=60022`（从 `~/.ssh/config` 的 a100-wzd 段读，端口不是 22）、`user="xmyf"`、`key_file="~/.ssh/id_ed25519_a100_wzd"`（展开为绝对路径）。直接 import `scripts.lib.remote_execution.run_ssh` 调用最稳。
- 命令传递：`run_ssh` 把 argv 用 `shlex.quote` 拼成**单个 remote 字符串**再作为 ssh 的最后一个参数；包装器若改成把 argv 逐元素拼进本地 `ssh host a b c`，远端 shell 引号解析会整体错位——`sh -c "find ... -printf '%f\n' ..."` 的引号被吃掉后变成无参 `find` 从家目录递归，返回 200 万行且 `cat` 路径缺层级。表现是"枚举出 200 万 clip、试跑 clip 名为 `.`"，极易误判成生产代码 bug。

## 试跑前盘点产物完整性（按 clip 检查 3 个必需文件）
```
ssh a100-wzd 'for d in <infer_root>/<chunk>/infer/*/data; do
  s=$([ -f "$d/inference_index_strict.csv" ] && echo 1 || echo 0); \
  n=$([ -f "$d/navigation/navi_text.csv" ] && echo 1 || echo 0); \
  de=$([ -s "$d/navigation/clip_discription_llm.txt" ] && echo 1 || echo 0); \
  echo "$s $n $de"; done | sort | uniq -c'
```
- A100 真实产物现存于 `/home/xmyf/wzd/data/nvidia_infer_dataset/`（`/home/xmyf/wzd/test/rosbag-catalog/data/` 是 test 代理 run 的产物，infer 根常为空）。
- 已知事实：`data_sample_chunk0` 与 `chunk2`（100 clip/chunk，2026-09-22 批）的 `clip_discription_llm.txt` **全量缺失**——该批次早于 DESCRIPTION 步骤修复或步骤未跑全。导入按契约"描述为空即失败"，会如实报 `FRAME_IMPORT_FAILED: <clip> clip 级描述为空`；这是**数据批次问题不是代码 bug**，如实报告，不要绕门禁（如注入假描述）硬跑通。
- 其余文件形态（chunk0 实测）：strict/navi 各 1134 数据行（1135 含 header），7 路相机列（`camera_cross_left_120fov` 等，`_f{0-3}_idx/_ts/_diff_ms`），`camera_images` 图片 `NNNNNN_small.jpg`，`egomotion` npy 齐全（2268=1134×2）。

## 单 clip 试跑骨架
- `list_clips=lambda root: [target]` 限定一个 clip（全 chunk 100 clip 会跑 ~1 万 ssh 往返，试跑不要全量）。
- `file_checker=make_find_file_checker(ssh)`、`read_text=lambda p: ssh(["cat", p]).stdout`、`api` 指向 `http://127.0.0.1:18080` 的 backend（bulk 走 POST，其余返回 `{}`）。
- `run_id` 用带 `manual` 前缀的独立值（如 `manual-nvidia-frames-trial`），不碰真实 run。
- 结果核验：backend `GET /api/v1/frames?source_id=<run_id>` 行数 == strict 唯一 frame_id 数；抽查一行的 `camera_paths_json`（28 路键）、`timestamp_ns`（ego_ts×1000）、`navigation_text` 与源 csv 一致。
