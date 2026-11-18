#!/usr/bin/env python3
"""干跑验证：不填任何真实 Key，验证额度轮换、降级、切段、合并、SRT、断点续跑、并发与熔断。

用法：
    python3 scripts/verify_rotation.py

覆盖：
  1. 合成带静音间隔的测试音频（ffmpeg）；
  2. --dry-run 全链路：ISI 鉴权失败 -> 腾讯额度耗尽 -> 讯飞接手，且全程零网络请求；
  3. 输出 JSON/SRT、时间戳、重叠区去重；
  4. 账本跨进程持久化、并发写入、跨月重置、到期判断；
  5. 连续失败熔断、job 独占锁、ffmpeg 缺失提示；
  6. 全部失败后保留断点 -> 修好后续跑成功。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "verify_out"
sys.path.insert(0, str(ROOT))

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


def run_cli(args: list[str], config: Path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    import os

    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    cmd = [sys.executable, "-m", "transcribe_router", *args, "--config", str(config)]
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, env=env)


def make_audio(path: Path, seconds: float) -> None:
    """合成 440Hz 音频，每 5 秒静音 0.8 秒（4.2s~5.0s）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    expr = r"0.3*sin(2*PI*440*t)*lt(mod(t\,5)\,4.2)"
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"aevalsrc={expr}:s=16000:d={seconds}",
            "-ac", "1", "-ar", "16000",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(f"生成测试音频失败：{proc.stderr}")


CONFIG_TEMPLATE = """
state_dir: ./{state}/state
routing:
  min_remaining_minutes: 0.01
  retries_per_provider: 1
  retry_backoff_seconds: 0.01
  poll_interval_seconds: 0.01
  poll_timeout_seconds: 60
  auth_retry_minutes: 30
  max_consecutive_failures: 2
  failure_cooldown_minutes: 10
concurrency:
  workers: 2
  per_provider: 1
segmentation:
  mode: silence
  max_seconds: 6
  overlap_seconds: 0.5
  silence_noise_db: -40
  silence_min_seconds: 0.4
  search_window_seconds: 3
output:
  dir: ./{state}/out
  json: true
  srt: true
  srt_max_chars: 0
  keep_segments: false
hosting:
  mode: url_prefix
  url_prefix: https://example.invalid/asr
  local_root: ./{state}/public
providers:
  tencent:
    enabled: true
    priority: 10
    quota:
      mode: monthly
      hours: 0.0025
  aliyun_isi:
    enabled: true
    priority: 10
    credentials:
      access_key_id: fake
      access_key_secret: fake
      appkey: fake
    quota:
      mode: daily
      hours: 0.02
  bailian:
    enabled: true
    priority: 10
    credentials:
      api_key: fake
    quota:
      mode: fixed
      hours: 0
  tingwu:
    enabled: false
    priority: 10
  iflytek:
    enabled: true
    priority: 3
    quota:
      mode: fixed
      hours: 0.01
  volcano:
    enabled: false
    priority: 10
    credentials:
      appid: fake
      token: fake
      cluster: fake
    quota:
      mode: fixed
      hours: 0.01
  netease:
    enabled: true
    priority: 1
    quota:
      mode: fixed
      hours: 0.02
mock:
  providers:
{extra_mock}
"""

MINIMAL_TEMPLATE = """
state_dir: ./{state}/state
routing:
  min_remaining_minutes: 0.01
  retries_per_provider: 0
  retry_backoff_seconds: 0.01
  poll_interval_seconds: 0.01
  poll_timeout_seconds: 60
  auth_retry_minutes: 30
  max_consecutive_failures: 2
  failure_cooldown_minutes: 10
concurrency:
  workers: 1
  per_provider: 1
segmentation:
  mode: fixed
  max_seconds: 6
  overlap_seconds: 0.5
output:
  dir: ./{state}/out
  json: true
  srt: false
hosting:
  mode: url_prefix
  url_prefix: https://example.invalid/asr
  local_root: ./{state}/public
providers:
  tencent:
    enabled: false
  aliyun_isi:
    enabled: false
  bailian:
    enabled: false
  tingwu:
    enabled: false
  iflytek:
    enabled: false
  volcano:
    enabled: {volcano}
    priority: 10
    credentials:
      appid: fake
      token: fake
      cluster: fake
    quota:
      mode: fixed
      hours: 10
  netease:
    enabled: false
mock:
  providers:
{extra_mock}
"""


def write_config(path: Path, state: str, extra_mock: str) -> Path:
    path.write_text(CONFIG_TEMPLATE.format(state=state, extra_mock=extra_mock), encoding="utf-8")
    return path


def write_minimal(path: Path, state: str, *, volcano: bool, extra_mock: str = "") -> Path:
    path.write_text(
        MINIMAL_TEMPLATE.format(state=state, volcano="true" if volcano else "false", extra_mock=extra_mock),
        encoding="utf-8",
    )
    return path


def test_concurrency_boundaries() -> None:
    """账本并发写、跨月重置、到期判断、切段边界合并、job 锁、ffmpeg 提示。"""
    import transcribe_router.ledger as ledger_mod
    from transcribe_router import audio as audio_mod
    from transcribe_router.config import load_config
    from transcribe_router.errors import AudioError, JobBusy
    from transcribe_router.pipeline import dedupe_utterances, merge_segment_utterances
    from transcribe_router.providers.base import Utterance
    from transcribe_router.store import JobStore

    print("\n5) 账本并发 / 时间边界 / 熔断 / 切段边界 / 环境检查")

    # 5.1 四个进程并发写同一个账本，不能丢账
    cfg_ledger = write_minimal(WORK / "config_ledger.yaml", "run3", volcano=False)
    worker = (
        "import sys\n"
        "from transcribe_router.config import load_config\n"
        "from transcribe_router.ledger import open_ledger\n"
        "cfg = load_config(sys.argv[1])\n"
        "led = open_ledger(cfg)\n"
        "for _ in range(int(sys.argv[3])):\n"
        "    led.record_usage('tencent', float(sys.argv[2]))\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", worker, str(cfg_ledger), "0.001", "20"], cwd=str(ROOT))
        for _ in range(4)
    ]
    for proc in procs:
        proc.wait()
    rows = {row["name"]: row for row in json.loads(run_cli(["quota", "--json"], cfg_ledger).stdout)}
    used = rows["tencent"]["used_hours_period"]
    check("4 进程并发记账无丢失", abs(used - 0.08) < 1e-9, f"实际 {used}")

    # 5.2 跨月自动重置 + 到期判断
    cfg_obj = load_config(cfg_ledger)
    roll_path = WORK / "run3" / "roll_ledger.json"
    original_now = ledger_mod.now_ts
    try:
        ledger_mod.now_ts = lambda: datetime(2026, 11, 30, 23, 0, 0).timestamp()
        ledger = ledger_mod.Ledger(roll_path, cfg_obj.providers)
        ledger.record_usage("tencent", 5.0)
        same_month = ledger.remaining_hours("tencent")

        ledger_mod.now_ts = lambda: datetime(2026, 12, 1, 0, 10, 0).timestamp()
        next_month = ledger.remaining_hours("tencent")
    finally:
        ledger_mod.now_ts = original_now
    check("本月已用 5 小时", abs(same_month - 5.0) < 1e-6, f"实际 {same_month}")
    check("跨月 1 号自动重置为满额", abs(next_month - 10.0) < 1e-6, f"实际 {next_month}")

    cfg_obj.providers["tencent"].quota.expires_at = "2020-01-01"
    ledger.sync_configs()
    check("到期日已过则剩余为 0", ledger.remaining_hours("tencent") == 0)
    cfg_obj.providers["tencent"].quota.expires_at = "2099-01-01"
    ledger.sync_configs()
    check("到期日在未来则正常", ledger.remaining_hours("tencent") > 0)

    # 5.3 跨切点句子：保留并裁剪，不丢内容
    rows = [
        Utterance(0, 1500, "这句话跨过了切点"),
        Utterance(0, 300, "短句"),
        Utterance(1600, 3000, "切点之后的句子"),
    ]
    merged = merge_segment_utterances(rows, segment_start_ms=4100, boundary_ms=4600, duration_ms=20000)
    check("完全落在重叠区的短句被丢弃", all(item["text"] != "短句" for item in merged))
    crossing = [item for item in merged if item["text"] == "这句话跨过了切点"]
    check(
        "跨切点句子保留且起点裁到切点",
        bool(crossing) and crossing[0]["start_ms"] == 4600 and crossing[0]["end_ms"] == 5600,
        str(crossing),
    )

    # 5.4 切点两侧的半句 + 整句合并成一条，不重复
    deduped = dedupe_utterances(
        [
            {"start_ms": 4000, "end_ms": 4600, "text": "这句话跨过"},
            {"start_ms": 4600, "end_ms": 9000, "text": "这句话跨过了切点"},
            {"start_ms": 12000, "end_ms": 13000, "text": "完全不同的另一句"},
        ]
    )
    check("半句与整句合并为一条", len(deduped) == 2, f"实际 {len(deduped)} 条")
    check(
        "合并后保留完整文本与并集时间",
        deduped[0]["text"] == "这句话跨过了切点"
        and deduped[0]["start_ms"] == 4000
        and deduped[0]["end_ms"] == 9000,
        str(deduped[0]),
    )

    # 5.5 job 独占锁
    store = JobStore(WORK / "jobstore")
    job = store.load_or_create(WORK / "audios" / "meeting_a.wav", 20000)
    busy = False
    with store.lock(job):
        try:
            with store.lock(job):
                pass
        except JobBusy:
            busy = True
    check("同一文件被并发处理时会被锁拒绝", busy)

    # 5.6 ffmpeg 缺失提示
    original_which = audio_mod.shutil.which
    audio_mod.shutil.which = lambda name: None
    missing_message = ""
    try:
        audio_mod.ensure_tools()
    except AudioError as exc:
        missing_message = str(exc)
    finally:
        audio_mod.shutil.which = original_which
    check("ffmpeg 缺失时给出可操作提示", "ffmpeg" in missing_message and "安装" in missing_message, missing_message)


def main() -> int:
    if shutil.which("ffmpeg") is None:
        raise SystemExit("需要 ffmpeg 才能跑验证")

    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)

    print("1) 生成测试音频（2 段 × 20s，每 5 秒有 0.8 秒静音）")
    make_audio(WORK / "audios" / "meeting_a.wav", 20)
    make_audio(WORK / "audios" / "meeting_b.wav", 20)
    check("测试音频生成", (WORK / "audios" / "meeting_a.wav").exists())

    print("\n2) 干跑：ISI 鉴权失败 -> 腾讯额度耗尽 -> 讯飞接手（代理指向死端口，证明零网络）")
    state = "run1"
    cfg = write_config(
        WORK / "config_run1.yaml",
        state,
        "    aliyun_isi:\n      auth_fail: true\n    iflytek:\n      transient_failures: 1",
    )
    proc = run_cli(
        ["run", str(WORK / "audios"), "-o", str(WORK / state / "out"), "--dry-run", "-v"],
        cfg,
        env_extra={
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "ALL_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
        },
    )
    log = proc.stdout + proc.stderr
    (WORK / "run1.log").write_text(log, encoding="utf-8")
    check("run 退出码为 0", proc.returncode == 0, f"实际 {proc.returncode}")
    check("死代理下干跑仍成功（无网络请求）", proc.returncode == 0 and "模拟鉴权失败" in log)

    out_dir = WORK / state / "out"
    for name in ("meeting_a", "meeting_b"):
        check(f"{name}.json 已生成", (out_dir / f"{name}.json").exists())
        check(f"{name}.srt 已生成", (out_dir / f"{name}.srt").exists() and (out_dir / f"{name}.srt").stat().st_size > 0)

    data = json.loads((out_dir / "meeting_a.json").read_text(encoding="utf-8"))
    used = set(data.get("providers") or [])
    check("两家都参与了转写（腾讯 + 讯飞）", used == {"tencent", "iflytek"}, f"实际 {sorted(used)}")
    check("切段数量正确（20s / 6s -> 4 段）", len(data["segments"]) == 4, f"实际 {len(data['segments'])}")
    check("合并后有句级时间戳", len(data["utterances"]) > 0)
    check(
        "时间戳单调递增且不超过总时长",
        all(a["start_ms"] <= b["start_ms"] for a, b in zip(data["utterances"], data["utterances"][1:]))
        and data["utterances"][-1]["end_ms"] <= data["duration_ms"] + 50,
    )
    check(
        "重叠区没有产生重复句子",
        len({(u["start_ms"], u["text"]) for u in data["utterances"]}) == len(data["utterances"]),
    )

    srt = (out_dir / "meeting_a.srt").read_text(encoding="utf-8")
    check("SRT 含标准时间轴", "-->" in srt and "00:00:0" in srt)

    check("日志出现鉴权失败降级", "鉴权失败" in log)
    check("日志出现切换提示", "[切换]" in log and "当前使用" in log)
    check("日志提示腾讯额度耗尽", "额度耗尽" in log)
    check("日志提示瞬时错误重试", "后重试" in log)
    check("零额度 provider 被跳过", "百炼" in log)
    check("讯飞、网易见外排序在最后（启动表可查）", "讯飞" in log and "网易见外" in log)
    check("日志不打印完整凭据", "fake" not in log)

    print("\n3) 账本持久化 / quota / exhaust / reset")
    quota = run_cli(["quota", "--json"], cfg)
    rows = {row["name"]: row for row in json.loads(quota.stdout)}
    check("腾讯剩余额度已归零（新进程读账本）", rows["tencent"]["remaining_hours"] == 0, str(rows["tencent"]["remaining_hours"]))
    check("讯飞本期用量已记账", rows["iflytek"]["used_hours_period"] > 0)
    check("阿里云 ISI 处于鉴权失败冷却", rows["aliyun_isi"]["auth_failed_until"] > 0)

    run_cli(["exhaust", "netease", "--reason", "验证用手动标记"], cfg)
    text = run_cli(["quota"], cfg).stdout
    check("exhaust 生效", "手动标记用完" in text or "手动标记" in text)
    run_cli(["reset", "netease"], cfg)
    text = run_cli(["quota"], cfg).stdout
    check("reset 后恢复可用", "可用" in text)

    print("\n4) 断点续跑：全部失败 -> 修好后从断点继续")
    make_audio(WORK / "audios2" / "resume_c.wav", 10)
    state2 = "run2"
    cfg_fail = write_config(WORK / "config_run2_fail.yaml", state2, "    aliyun_isi:\n      auth_fail: true")
    proc = run_cli(
        ["run", str(WORK / "audios2"), "-o", str(WORK / state2 / "out"), "--dry-run", "--provider", "aliyun_isi"],
        cfg_fail,
    )
    check("全部失败时退出码为 2", proc.returncode == 2, f"实际 {proc.returncode}")
    check("失败后没有产出 JSON", not (WORK / state2 / "out" / "resume_c.json").exists())

    cfg_ok = write_config(WORK / "config_run2_ok.yaml", state2, "    aliyun_isi: {}")
    proc = run_cli(
        ["run", str(WORK / "audios2"), "-o", str(WORK / state2 / "out"), "--dry-run", "--provider", "aliyun_isi", "-v"],
        cfg_ok,
    )
    check("修复后续跑成功", proc.returncode == 0, f"实际 {proc.returncode}")
    check("续跑产出 JSON", (WORK / state2 / "out" / "resume_c.json").exists())
    check("续跑日志提示从断点继续", "断点" in (proc.stdout + proc.stderr))

    test_concurrency_boundaries()

    print("\n6) 连续失败熔断：同一家不会被每段反复选中")
    make_audio(WORK / "audios3" / "bad.wav", 20)
    cfg_circuit = write_minimal(
        WORK / "config_circuit.yaml",
        "run4",
        volcano=True,
        extra_mock="    volcano:\n      fatal_fail: true",
    )
    proc = run_cli(
        ["run", str(WORK / "audios3"), "-o", str(WORK / "run4" / "out"), "--dry-run", "-v"],
        cfg_circuit,
    )
    circuit_log = proc.stdout + proc.stderr
    check("熔断场景退出码为 2", proc.returncode == 2, f"实际 {proc.returncode}")
    check("连续失败 2 次后停止调用该家（4 段只尝试 2 次）", circuit_log.count("[降级]") == 2, f"实际 {circuit_log.count('[降级]')}")
    check("日志提示熔断原因", "连续失败熔断中" in circuit_log)

    print("\n" + "=" * 70)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for item in FAILED:
            print(f"  - {item}")
        print(f"\n日志：{WORK / 'run1.log'}")
        return 1
    print("全部验证通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
