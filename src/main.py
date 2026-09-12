import argparse
import asyncio
import contextlib
import json
import logging
import shutil
import sys
import threading
from pathlib import Path

from src.browser.groups import unique_profile_platforms
from src.core.config import DEFAULT_CONFIG, PROJECT_ROOT, load_config
from src.core.exceptions import BotError, ConfigurationError, HumanRequired
from src.core.logging import redact
from src.core.models import Platform

LOGIN_URLS = {
    Platform.APPLE: "https://secure.www.apple.com.cn/shop/account/home",
    Platform.JD: "https://passport.jd.com/new/login.aspx",
    Platform.TMALL: "https://login.tmall.com/",
    Platform.TAOBAO: "https://login.taobao.com/",
}


def output(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Apple 购买辅助 · 默认禁止最终提交订单")
    cli.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    cli.add_argument("--headless", action="store_true", help="无界面检查；人工登录仍打开可见浏览器")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="初始化本地配置、目录和数据库，不覆盖已有配置")
    login = commands.add_parser("login", help="手动登录并保留本机 profile")
    login.add_argument("platform", choices=[p.value for p in Platform])
    for name in ("check-login", "check-stock", "dry-run"):
        item = commands.add_parser(name)
        item.add_argument(
            "platform", choices=[p.value for p in Platform] + ["all"], default="all", nargs="?"
        )
    run = commands.add_parser(
        "run", help="按配置时间启动，控制台支持 resume / finish / stop / status"
    )
    run.add_argument("--now", action="store_true", help="跳过计划等待，立即执行")
    commands.add_parser("status", help="读取本地持久化运行记录；实时状态用 Web /status")
    web = commands.add_parser("web", help="启动仅本机可访问的状态页面和 JSON API")
    web.add_argument("--port", type=int, default=8765)
    doctor = commands.add_parser("doctor", help="检查依赖、浏览器、配置、数据库和系统时间")
    doctor.add_argument("--online", action="store_true", help="增加一次 Apple 公共页面网络检查")
    inspect = commands.add_parser("inspect", help="保存最小化页面诊断与遮罩布局截图")
    inspect.add_argument("platform", choices=[p.value for p in Platform])
    inspect.add_argument("url")
    inspect.add_argument("--output", type=Path)
    reconcile = commands.add_parser("reconcile", help="人工核查平台订单后处理遗留的非成功订单锁")
    reconcile.add_argument("owner", help="status 显示的完整订单锁 owner")
    reconcile.add_argument(
        "--confirmed-no-order",
        action="store_true",
        required=True,
        help="仅在已亲自确认没有已创建/待支付订单后使用",
    )
    return cli


def initialize(path: Path) -> dict:
    from src.storage.database import Database

    path.parent.mkdir(parents=True, exist_ok=True)
    created = not path.exists()
    if created:
        shutil.copyfile(PROJECT_ROOT / "config" / "config.example.yaml", path)
    config = load_config(path)
    for directory in (config.paths.profiles, config.paths.logs, config.paths.screenshots):
        directory.mkdir(parents=True, exist_ok=True)
    for platform in unique_profile_platforms():
        (config.paths.profiles / platform.value).mkdir(exist_ok=True)
    database = Database(config.paths.database)
    database.initialize()
    database.close()
    return {
        "status": "initialized",
        "config": str(path.resolve()),
        "created_config": created,
        "dry_run": config.app.dry_run,
        "auto_submit": config.order.auto_submit,
    }


def console_queue() -> asyncio.Queue:
    """Daemon input reader cannot keep a completed background run alive at process exit."""
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def read():
        try:
            for line in sys.stdin:
                if loop.is_closed():
                    break
                loop.call_soon_threadsafe(queue.put_nowait, line.strip())
        except (OSError, RuntimeError):
            pass

    threading.Thread(target=read, name="local-console-input", daemon=True).start()
    return queue


async def run_console(runtime, platforms, immediate, dry_run):
    # Show existing transaction/session blocks before asking for a new purchase.
    # Runtime.start repeats this check after any human confirmation wait.
    for platform in Platform:
        runtime._require_session_idle(platform)
    plan = runtime.plan_snapshot()
    output({"purchase_plan": plan})
    queue = console_queue()
    if plan.get("products") and (platforms is None or Platform.APPLE in platforms):
        print(
            "开始前一次确认：按展示的优先级、规格、数量和预算购买 Apple 中国大陆官网直售商品；"
            "使用当前账户结算时选中的已保存地址，首次完整读取后在本机绑定，"
            "内容变化或无法核验时暂停。付款方式按计划执行，提交开关保持本机配置。"
        )
        while not plan.get("approved"):
            print(f"核对后请输入 confirm-start {plan['digest']}；plan 重看，stop 退出。")
            parts = (await queue.get()).split()
            if parts == ["stop"]:
                return 0
            if parts == ["plan"]:
                plan = runtime.plan_snapshot()
                output({"purchase_plan": plan})
            elif len(parts) == 2 and parts[0] == "confirm-start":
                try:
                    output(await runtime.approve_plan(parts[1]))
                    plan = runtime.plan_snapshot()
                except BotError as exc:
                    print(redact(str(exc)))
        print("本次条件已确认；相同计划沿用本机确认，不重复询问。")
    await runtime.start(platforms, immediate=immediate, dry_run=dry_run)
    print(
        "控制命令：plan / resume [platform] / finish / stop / status。"
        "finish 结束本机自动任务并留页，不取消订单或解除交易保护；stop 退出并关闭程序浏览器。"
        "仅页面内容变化时，可核对后用 confirm-address apple 或 confirm-market apple 更新依据。"
    )
    reader = asyncio.create_task(queue.get())
    stop_requested = False
    worker_failed = False
    finished_locally = False
    try:
        while runtime.task and not runtime.task.done():
            done, _ = await asyncio.wait(
                [runtime.task, reader], return_when=asyncio.FIRST_COMPLETED
            )
            if reader in done:
                parts = reader.result().split()
                reader = asyncio.create_task(queue.get())
                if not parts:
                    continue
                try:
                    if parts[0] == "resume":
                        await runtime.resume(Platform(parts[1]) if len(parts) > 1 else None)
                    elif parts[0] == "stop":
                        stop_requested = True
                        await runtime.stop()
                    elif parts[0] == "finish":
                        output(await runtime.finish_task())
                        finished_locally = True
                    elif parts[0] == "status":
                        output(runtime.snapshot())
                    elif parts[0] == "plan":
                        output(runtime.plan_snapshot())
                    elif parts[0] == "approve-plan" and len(parts) == 2:
                        if runtime.task.done():
                            raise ConfigurationError(
                                "当前工作已结束；请先核对结果，不能继续批准购买"
                            )
                        output(await runtime.approve_plan(parts[1]))
                    elif parts[0] in {"confirm-address", "confirm-market"} and len(parts) == 2:
                        output(
                            await runtime.confirm_checkout(
                                Platform(parts[1]), parts[0].split("-")[1]
                            )
                        )
                    else:
                        print(
                            "可用命令：plan / resume [platform] / finish / stop / status"
                        )
                except (ValueError, BotError) as exc:
                    print(redact(str(exc)))
        if runtime.task:
            try:
                await runtime.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                worker_failed = True
                # Error text may contain page content or credentials; retain only its type.
                with contextlib.suppress(Exception):
                    logging.getLogger(__name__).error("Run task failed: %s", type(exc).__name__)
                print(f"运行异常：{type(exc).__name__}；页面已保留，请核对后输入 stop。")
        with contextlib.suppress(Exception):
            output(runtime.snapshot())
        # Ambiguous submission / unclassified errors may finish the worker, but the
        # browser is still the user's evidence. Only stop or browser close ends this hold.
        while (
            not stop_requested
            and (
                worker_failed
                or finished_locally
                or any(
                    p["state"] in {"WAITING_HUMAN", "READY_TO_SUBMIT", "SUCCESS"}
                    for p in runtime.snapshot().get("platforms", {}).values()
                )
            )
            and any(runtime.manager.current_page(p) is not None for p in Platform)
        ):
            done, _ = await asyncio.wait([reader], timeout=1)
            if reader not in done:
                continue
            command = reader.result().strip()
            reader = asyncio.create_task(queue.get())
            if command == "stop":
                break
            if command == "status":
                output(runtime.snapshot())
            elif command == "finish":
                output(await runtime.finish_task())
                finished_locally = True
            elif command == "plan":
                output(runtime.plan_snapshot())
            elif command.startswith(("resume", "approve-plan")):
                print("当前工作已暂停结束；请先核对订单结果。resume 不会解除订单锁或重新提交。")
        return 2 if worker_failed else 0
    finally:
        reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader


async def check(runtime, command, platforms):
    results = {}
    queue = None
    for platform in platforms:
        runtime._require_session_idle(platform)
        adapter = runtime.adapters[platform]
        targets = runtime.config.targets([platform])
        if command == "check-stock" and not targets:
            results[platform.value] = {"status": "BLOCKED", "reason": "No configured product URL"}
            continue
        if command == "check-login":
            if not targets:
                results[platform.value] = {
                    "login": "UNKNOWN",
                    "reason": "No configured product URL",
                }
                continue
            results[platform.value] = await runtime.check_login(platform)
            continue
        for _, product, url in targets:
            while True:
                try:
                    await adapter.open_product(product, url)
                    skus = await adapter.check_stock()
                    results[f"{platform.value}/{product}"] = [
                        s.model_dump(mode="json") for s in skus
                    ]
                    break
                except HumanRequired:
                    output(
                        {
                            "state": "WAITING_HUMAN",
                            "platform": platform.value,
                            "message": (
                                "需要人工操作。浏览器保持打开；输入 resume 重查或 stop 退出。"
                            ),
                        }
                    )
                    queue = queue or console_queue()
                    while True:
                        text = await queue.get()
                        if text == "stop":
                            return
                        if text == "resume":
                            break
    output(results)


async def inspect_page(runtime, platform: Platform, url: str, destination: Path):
    runtime._require_session_idle(platform)
    adapter = runtime.adapters[platform]
    path = await adapter.inspect(url, destination)
    evidence = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    state = evidence.get("metadata", {}).get("state", "")
    output({"inventory": str(path.resolve()), "state": state, "commerce_selectors": "UNKNOWN"})
    if state != "WAITING_HUMAN":
        return
    print("需要人工操作；浏览器保持打开。处理后输入 resume，或输入 stop 退出。")
    queue = console_queue()
    while runtime.manager.current_page(platform) is not None:
        try:
            command = await asyncio.wait_for(queue.get(), timeout=1)
        except TimeoutError:
            continue
        if command == "stop":
            return
        if command == "resume":
            verification = await adapter.detect_verification()
            if not verification.required:
                output({"state": "STOPPED", "message": "验证页已解除；inspect 检查结束，未下单"})
                return
            print("页面仍需人工处理；程序不会自动点击验证控件。")


async def async_main(args) -> int:
    from src.diagnostics import doctor
    from src.runtime import Runtime
    from src.storage.database import Database

    config = load_config(args.config)
    if args.headless:
        config = config.model_copy(update={"app": config.app.model_copy(update={"headless": True})})
    if args.command == "doctor":
        result = await doctor(config, args.online)
        output(result)
        return (
            2
            if any(isinstance(v, dict) and v.get("status") == "FAIL" for v in result.values())
            else 0
        )
    if args.command in {"status", "reconcile"}:
        database = Database(config.paths.database)
        database.initialize()
        try:
            if args.command == "reconcile":
                from src.browser.session import ProfileLock

                # A live browser may still have an in-flight submission. Require its
                # owner to close before accepting the human order-history confirmation.
                with contextlib.ExitStack() as stack:
                    for platform in unique_profile_platforms():
                        lock = ProfileLock(config.paths.profiles / platform.value)
                        lock.acquire()
                        stack.callback(lock.release)
                    result = database.reconcile_order(
                        args.owner, confirmed_no_order=args.confirmed_no_order
                    )
                output({"reconciled": result})
            else:
                output(
                    {
                        "source": "SQLite history; use Web /status for live state",
                        "runs": database.recent("runs", 10),
                        "events": database.recent("events", 20),
                        "order_guard": database.guard_status(),
                    }
                )
        finally:
            database.close()
        return 0
    runtime = Runtime(config)
    try:
        if args.command == "login":
            platform = Platform(args.platform)
            runtime._require_login_idle(platform)
            status = await runtime.manager.manual_login(platform, LOGIN_URLS[platform])
            output(
                {
                    "platform": platform.value,
                    "login": status.value,
                    "message": "本机 profile 已保留；运行 check-login 可核实登录状态",
                }
            )
        elif args.command == "inspect":
            destination = args.output or config.paths.screenshots / "inspections"
            await inspect_page(runtime, Platform(args.platform), args.url, destination)
        elif args.command in {"check-login", "check-stock"}:
            platforms = list(Platform) if args.platform == "all" else [Platform(args.platform)]
            await check(runtime, args.command, platforms)
        elif args.command in {"dry-run", "run"}:
            platforms = (
                None
                if args.command == "run" or args.platform == "all"
                else [Platform(args.platform)]
            )
            return await run_console(
                runtime,
                platforms,
                immediate=args.command == "dry-run" or args.now,
                dry_run=True if args.command == "dry-run" else None,
            )
        return 0
    finally:
        await runtime.close()


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            output(initialize(args.config))
            return 0
        if args.command == "web":
            import uvicorn

            from src.web.app import create_app

            if not 1024 <= args.port <= 65535:
                raise ConfigurationError("Port must be between 1024 and 65535")
            print(f"本机状态页面：http://127.0.0.1:{args.port}")
            uvicorn.run(
                create_app(load_config(args.config)),
                host="127.0.0.1",
                port=args.port,
                access_log=False,
            )
            return 0
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("已停止自动操作；结果不明的订单锁保留。")
        return 130
    except BotError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 2
    except Exception as exc:
        # Never echo raw browser/network exceptions containing credentials or page content.
        print(f"操作失败：{type(exc).__name__}；请运行 doctor 并查看脱敏诊断。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
