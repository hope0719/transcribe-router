"""命令行入口。

    python -m transcribe_router run ./audios -o ./out
    python -m transcribe_router quota
    python -m transcribe_router exhaust tencent --reason "本月 10 小时已用完"
    python -m transcribe_router reset tencent
"""

from __future__ import annotations

import argparse
import json
import sys

from .audio import collect_inputs, ensure_tools
from .config import load_config, resolve_output_dir
from .errors import AudioError, ConfigError
from .hosting import Hosting
from .ledger import open_ledger
from .pipeline import Pipeline
from .providers import PROVIDER_CLASSES, ProviderContext, RateLimiter, build_providers
from .router import Router
from .store import JobStore
from .utils import fmt_hours, fmt_ts, setup_logger

KNOWN_COMMANDS = {"run", "quota", "exhaust", "reset", "providers"}


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config.yaml", help="配置文件路径（默认 ./config.yaml）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出警告与错误")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="transcribe-router",
        description="批量录音转写 + 多 Provider 免费额度自动轮换",
    )
    _common(parser)
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="转写音频文件或目录")
    _common(run)
    run.add_argument("inputs", nargs="+", help="音频文件或目录（目录会递归扫描）")
    run.add_argument("-o", "--out", help="输出目录，默认取配置 output.dir")
    run.add_argument("--provider", help="强制只使用某一家（不自动降级）")
    run.add_argument("--dry-run", action="store_true", help="干跑：用 Mock provider 验证轮换/降级逻辑，不发真实请求")
    run.add_argument("--no-srt", action="store_true", help="不导出 SRT")
    run.add_argument("--no-json", action="store_true", help="不导出 JSON")
    run.add_argument("--workers", type=int, help="覆盖 concurrency.workers")

    quota = sub.add_parser("quota", help="查看各家剩余额度")
    _common(quota)
    quota.add_argument("--json", action="store_true", help="以 JSON 输出")

    exhaust = sub.add_parser("exhaust", help="手动标记某家额度已用完")
    _common(exhaust)
    exhaust.add_argument("provider", help="provider 名称，例如 tencent")
    exhaust.add_argument("--reason", default="手动标记用完", help="原因备注")

    reset = sub.add_parser("reset", help="清除某家的手动标记/鉴权失败，并清零本期用量")
    _common(reset)
    reset.add_argument("provider")
    reset.add_argument("--keep-total", action="store_true", help="保留累计用量（默认保留）")

    providers = sub.add_parser("providers", help="列出 provider 与排序")
    _common(providers)

    return parser


def _load(args):
    cfg = load_config(args.config)
    logger = setup_logger(verbose=args.verbose, quiet=args.quiet)
    return cfg, logger


def cmd_run(args) -> int:
    cfg, logger = _load(args)
    if args.workers:
        cfg.concurrency.workers = max(1, args.workers)

    try:
        ensure_tools()
        files = collect_inputs(args.inputs)
    except AudioError as exc:
        logger.error("%s", exc)
        return 1
    if not files:
        logger.error("没有找到可处理的音频文件")
        return 1

    out_dir = resolve_output_dir(cfg, args.out)
    if args.provider and args.provider not in cfg.providers:
        logger.error("未知 provider：%s（可选：%s）", args.provider, ", ".join(cfg.providers))
        return 1
    ledger = open_ledger(cfg)
    hosting = Hosting(cfg.hosting, cfg.path.parent)
    if not hosting.available:
        logger.info("未配置 hosting.url_prefix：只接受公网 URL 的 provider 会被自动跳过（讯飞/网易见外仍可本地直传）")

    ctx = ProviderContext(
        logger=logger,
        hosting=hosting,
        poll_interval=cfg.routing.poll_interval_seconds,
        poll_timeout=cfg.routing.poll_timeout_seconds,
        dry_run=args.dry_run,
        limiter=RateLimiter(),
    )

    if args.dry_run:
        logger.warning("== 干跑模式：不会发起任何真实 API 请求，所有 provider 由 Mock 代替 ==")

    providers, notes = build_providers(cfg, ctx, dry_run=args.dry_run, only=args.provider)
    for note in notes:
        logger.info("跳过：%s", note)
    if not providers:
        logger.error("没有可用的 provider：请检查 config.yaml 的 providers.*.enabled 与 credentials/.env")
        return 1

    router = Router(providers, ledger, cfg, logger, forced=args.provider, dry_run=args.dry_run)
    router.startup_report()

    store = JobStore(cfg.state_dir / "jobs")
    pipeline = Pipeline(cfg, ledger, router, store, hosting, logger)
    results = pipeline.run(
        files,
        out_dir,
        write_json=not args.no_json,
        write_srt=not args.no_srt,
    )

    logger.info("输出目录：%s", out_dir)
    for item in results:
        for path in item.outputs:
            logger.info("  %s", path)

    failed = [item for item in results if not item.ok]
    return 0 if not failed else 2


def cmd_quota(args) -> int:
    cfg, _ = _load(args)
    ledger = open_ledger(cfg)
    rows = ledger.all_status()

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    header = f"{'provider':<12}{'产品':<30}{'模式':<8}{'总额度':>9}{'本期已用':>10}{'剩余':>10}  {'到期':<20}{'状态'}"
    print(header)
    print("-" * len(header))
    mode_text = {"monthly": "每月", "daily": "每日", "fixed": "一次性"}
    for row in rows:
        print(
            f"{row['name']:<12}{row['display'][:28]:<30}{mode_text.get(row['mode'], row['mode']):<8}"
            f"{fmt_hours(row['quota_hours']):>9}{fmt_hours(row['used_hours_period']):>10}"
            f"{fmt_hours(row['remaining_hours']):>10}  "
            f"{fmt_ts(row['expires_ts']) if row['expires_ts'] else '无':<20}{row['state']}"
        )
    print("")
    print(f"账本文件：{cfg.state_dir / 'ledger.json'}")
    return 0


def cmd_exhaust(args) -> int:
    cfg, logger = _load(args)
    if args.provider not in cfg.providers:
        logger.error("未知 provider：%s（可选：%s）", args.provider, ", ".join(cfg.providers))
        return 1
    ledger = open_ledger(cfg)
    ledger.mark_exhausted(args.provider, args.reason)
    logger.info("已标记 %s 额度用完：%s", cfg.providers[args.provider].display, args.reason)
    logger.info("下次运行会自动跳过这家；要恢复请执行 reset %s", args.provider)
    return 0


def cmd_reset(args) -> int:
    cfg, logger = _load(args)
    if args.provider not in cfg.providers:
        logger.error("未知 provider：%s", args.provider)
        return 1
    ledger = open_ledger(cfg)
    ledger.reset(args.provider, keep_total=args.keep_total or True)
    logger.info("已重置 %s：手动标记与本期用量清零", cfg.providers[args.provider].display)
    return 0


def cmd_providers(args) -> int:
    cfg, _ = _load(args)
    ledger = open_ledger(cfg)
    rows = []
    for name, pcfg in cfg.providers.items():
        cls = PROVIDER_CLASSES[name]
        probe = cls(pcfg, ProviderContext(logger=None, hosting=None))  # 只用于读凭据字段
        missing = probe.missing_credentials()
        rows.append((pcfg.priority, name, pcfg, missing, ledger.remaining_hours(name)))

    rows.sort(key=lambda item: (-item[0], -item[4]))
    print(f"{'#':<3}{'provider':<12}{'产品':<32}{'优先级':<7}{'剩余':>10}  {'凭据'}")
    print("-" * 92)
    for index, (priority, name, pcfg, missing, remaining) in enumerate(rows, start=1):
        credential_text = "已配置" if not missing else f"缺少 {','.join(missing)}"
        flag = " [实验性]" if pcfg.experimental else ""
        print(f"{index:<3}{name:<12}{pcfg.display[:30] + flag:<32}{priority:<7}{fmt_hours(remaining):>10}  {credential_text}")
    print("")
    print("排序规则：priority 大者先用；同 priority 内按剩余额度从多到少。")
    print("只想用某一家：run <音频> --provider <name>")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(token in KNOWN_COMMANDS for token in argv):
        argv = ["run"] + argv

    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    handlers = {
        "run": cmd_run,
        "quota": cmd_quota,
        "exhaust": cmd_exhaust,
        "reset": cmd_reset,
        "providers": cmd_providers,
    }
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断（进度已保存，重跑可续传）", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
