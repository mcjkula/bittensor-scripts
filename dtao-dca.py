import asyncio
import bittensor as bt
from retry import retry
import time
import logging
from datetime import datetime, timedelta, time as dt_time
from typing import Dict, Tuple, List
from rich.console import Console, Group
from rich.table import Table
from rich.columns import Columns
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich import box
import os
import json
from pathlib import Path


SUBNET_CONFIGS: Dict[int, Tuple[float, str]] = {
    1: (0.01, "validator-SS58"), # Enter the NETUID, amount to stake and the validator you want to staked with
    2: (0.01, "validator-SS58"),
    3: (0.01, "validator-SS58"),
    4: (0.01, "validator-SS58"),
    5: (0.01, "validator-SS58"),
}

ROOT_NETUID = 0
ROOT_HOTKEY = "validator-SS58" # Enter the validator/hotkey you are staked with on your root stake (it only supports one)
DIVIDEND_CHECK_INTERVAL = timedelta(seconds=60)
MIN_ROOT_STAKE = 1 # Please enter the minimal amount you want to have on root, everything above it will be distributed across subnets
MIN_STAKE_THRESHOLD = 0.00055

AUTO_MODE = True

SCHEDULE_FILE = Path("staking_schedule.json")

logging.basicConfig(
    filename='staking_operations.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

console = Console()
header_style = Style(color="bright_cyan", bold=True)
value_style = Style(color="white", bold=True)

wallet = bt.wallet(name="default")
wallet.unlock_coldkey()

history_log: List[str] = []

def read_schedule() -> dict:
    if not SCHEDULE_FILE.exists():
        initial_data = {
            "next_staking": "1999-01-01T00:00:00"
        }
        with open(SCHEDULE_FILE, 'w') as f:
            json.dump(initial_data, f)
        logger.info("Created staking_schedule.json with initial timestamp")
        return {"next_staking": datetime.fromisoformat(initial_data["next_staking"])}

    with open(SCHEDULE_FILE, 'r') as f:
        data = json.load(f)
        return {
            "next_staking": datetime.fromisoformat(data["next_staking"]) if data["next_staking"] else None
        }

def write_schedule(next_staking: datetime) -> None:
    data = {
        "next_staking": next_staking.isoformat(),
    }
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(data, f)

def next_staking_time(reference_time: datetime = None) -> datetime:
    """Calculate next stake time at 00:00, 06:00, 12:00, or 18:00 UTC""" # You would need to change this method to achieve different intervals of the scheduled stake
    ref_time = reference_time or datetime.utcnow()

    current_hour = ref_time.hour
    next_hour = ((current_hour // 6) + 1) * 6

    if next_hour >= 24:
        next_day = ref_time.date() + timedelta(days=1)
        return datetime.combine(next_day, dt_time(0, 0)).replace(tzinfo=ref_time.tzinfo)
    else:
        return datetime.combine(ref_time.date(), dt_time(next_hour, 0)).replace(tzinfo=ref_time.tzinfo)

def append_history(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    history_log.append(entry)
    if len(history_log) > 5:
        del history_log[0]
    logger.info(f"History: {message}")

def user_confirmation(prompt: str) -> bool:
    if AUTO_MODE:
        return True
    return console.input(prompt + " (y/n): ").strip().lower() == 'y'

async def get_stake(subtensor: bt.AsyncSubtensor, coldkey: str, hotkey: str, netuid: int) -> bt.Balance:
    try:
        stake = await subtensor.get_stake(coldkey_ss58=coldkey, hotkey_ss58=hotkey, netuid=netuid)
        logger.info(f"[Net {netuid}] Current stake: {stake.tao:.5f} TAO")
        return stake
    except Exception as e:
        logger.error(f"Failed to get stake for {hotkey} on net {netuid}: {e}")
        return bt.Balance(0)

async def get_balance(subtensor: bt.AsyncSubtensor, address: str) -> bt.Balance:
    try:
        balance = await subtensor.get_balance(address)
        logger.info(f"Balance: {balance.tao:.5f} TAO")
        return balance
    except Exception as e:
        logger.error(f"Failed to get balance for {address}: {e}")
        return bt.Balance(0)

@retry(Exception, tries=3, delay=2, backoff=2, max_delay=30)
async def unstake_excess(subtensor: bt.AsyncSubtensor, wallet: bt.wallet, netuid: int, hotkey: str, amount: float) -> float:
    coldkey = wallet.coldkeypub.ss58_address
    try:
        initial = await get_stake(subtensor, coldkey, hotkey, netuid)

        max_safe_unstake = max(0, initial.tao - MIN_ROOT_STAKE)
        actual_unstake = min(amount, max_safe_unstake)

        if actual_unstake <= 0:
            logger.warning(f"[Net {netuid}] Unsafe unstake attempt: {amount:.5f} TAO requested, but only {max_safe_unstake:.5f} TAO available.")
            append_history(f"Unstake blocked on Net {netuid} (would breach minimum)")
            return 0

        logger.info(f"[Net {netuid}] Request to unstake {actual_unstake:.5f} TAO (Safe limit: {max_safe_unstake:.5f} TAO)")

        if not user_confirmation(f"Unstake {actual_unstake:.5f} TAO on network {netuid}?"):
            logger.info("Unstake cancelled by user")
            append_history(f"Cancelled unstaking on Net {netuid}")
            return 0

        await subtensor.unstake(
            wallet=wallet,
            netuid=netuid,
            hotkey_ss58=hotkey,
            amount=bt.Balance.from_tao(actual_unstake)
        )
        await asyncio.sleep(3)
        new_stake = await get_stake(subtensor, coldkey, hotkey, netuid)

        if new_stake.tao < MIN_ROOT_STAKE:
            deficit = MIN_ROOT_STAKE - new_stake.tao
            logger.warning(f"[Net {netuid}] EMERGENCY RESTAKE NEEDED: {deficit:.5f} TAO")
            restake_result = await stake_dividend(subtensor, wallet, netuid, hotkey, deficit)
            if restake_result > 0:
                append_history(f"Emergency restake: {deficit:.5f} TAO on Net {netuid}")
            else:
                append_history(f"Emergency restake FAILED on Net {netuid}")
            new_stake = await get_stake(subtensor, coldkey, hotkey, netuid)

        logger.info(f"[Net {netuid}] Unstaked. Final stake: {new_stake.tao:.5f} TAO")
        append_history(f"Unstaked {actual_unstake:.5f} TAO on Net {netuid}")
        return actual_unstake
    except Exception as e:
        logger.error(f"Unstaking failed on Net {netuid}: {e}")
        append_history(f"Unstaking failed on Net {netuid}: {e}")
        return 0
    finally:
        await asyncio.sleep(15)

@retry(Exception, tries=3, delay=2, backoff=2, max_delay=30)
async def stake_dividend(subtensor: bt.AsyncSubtensor, wallet: bt.wallet, netuid: int, hotkey: str, amount: float) -> float:
    coldkey = wallet.coldkeypub.ss58_address
    try:
        initial = await get_stake(subtensor, coldkey, hotkey, netuid)

        logger.info(f"[Net {netuid}] Request to stake {amount:.5f} TAO (Current stake: {initial.tao:.5f} TAO)")
        if not user_confirmation(f"Stake {amount:.5f} TAO on network {netuid}?"):
            logger.info("Staking cancelled by user")
            append_history(f"Cancelled staking on Net {netuid}")
            return 0

        await subtensor.add_stake(
            wallet=wallet,
            netuid=netuid,
            hotkey_ss58=hotkey,
            amount=bt.Balance.from_tao(amount)
        )
        await asyncio.sleep(3)
        new_stake = await get_stake(subtensor, coldkey, hotkey, netuid)

        logger.info(f"[Net {netuid}] Staked. New stake: {new_stake.tao:.5f} TAO (was {initial.tao:.5f} TAO)")
        append_history(f"Staked {amount:.5f} TAO on Net {netuid}")
        return amount
    except Exception as e:
        logger.error(f"Staking failed on Net {netuid}: {e}")
        append_history(f"Staking failed on Net {netuid}: {e}")
        return 0
    finally:
        await asyncio.sleep(15)

async def process_subnet(subtensor: bt.AsyncSubtensor, wallet: bt.wallet, netuid: int, amount: float, hotkey: str) -> float:
    try:
        start = time.monotonic()
        staked = await stake_dividend(subtensor, wallet, netuid, hotkey, amount)
        duration = time.monotonic() - start
        logger.info(f"Staked {amount:.5f} TAO on Net {netuid} in {duration:.2f}s")
        return staked
    except Exception as e:
        logger.error(f"Staking error on Net {netuid}: {str(e)}")
        append_history(f"Staking error on Net {netuid}: {e}")
        return 0

def create_dividend_panel(current_stake: float, excess: float, required_excess: float, next_check: timedelta) -> Panel:
    panel_width = (console.width // 2)

    table = Table.grid(padding=(0, 0))
    table.add_column(justify="left", style=header_style, width=panel_width // 2)
    table.add_column(justify="right", style=value_style, width=panel_width // 2)

    table.add_row("Current Stake:", f"{current_stake:.5f} TAO")
    table.add_row("Minimum Stake:", f"{MIN_ROOT_STAKE:.5f} TAO")
    table.add_row("Current Excess:", f"[cyan]{excess:.5f} TAO[/cyan]")
    table.add_row("Required Excess:", f"{required_excess:.5f} TAO")
    table.add_row("")
    mins, secs = divmod(next_check.seconds, 60)
    table.add_row("Next Update In:", f"{mins}m {secs}s")
    status = "Active" if excess >= required_excess else "Waiting"
    table.add_row("Status:", f"[{'green' if status=='Active' else 'yellow'}]{status}[/{'green' if status=='Active' else 'yellow'}]")

    return Panel(
        table,
        title="[bold magenta]Dividend Management[/bold magenta]",
        border_style="magenta",
        box=box.ROUNDED,
        padding=(1, 2),
        width=panel_width,
        height=12
    )

def create_staking_panel(next_staking: datetime, balance: float, total_required: float) -> Panel:
    panel_width = (console.width // 2)

    table = Table.grid(padding=(0, 0))
    table.add_column(justify="left", style=header_style, width=panel_width // 2)
    table.add_column(justify="right", style=value_style, width=panel_width // 2)

    table.add_row("Next Staking:", next_staking.strftime("%Y-%m-%d %H:%M:%S"))
    table.add_row("Current Balance:", f"{balance:.5f} TAO")
    table.add_row("Required Total:", f"{total_required:.5f} TAO")
    table.add_row("")
    table.add_row("")
    status = "Ready" if balance >= total_required else "Insufficient"
    table.add_row("Funding Status:", f"[{'green' if status=='Ready' else 'red'}]{status}[/{'green' if status=='Ready' else 'red'}]")

    return Panel(
        table,
        title="[bold green]Scheduled Staking[/bold green]",
        border_style="green",
        box=box.ROUNDED,
        padding=(1, 2),
        width=panel_width,
        height=12
    )

def create_subnet_panel(subnet_stakes: Dict[int, float]) -> Panel:
    table = Table(title="Subnet Stakes (ÃŽÂ±)", box=box.ROUNDED, show_header=True, header_style="bold magenta", expand=True)
    table.add_column("Subnet", justify="right", style="cyan")
    table.add_column("Validator", style="white")
    table.add_column("Staked", justify="right", style="bold green")
    for netuid, (_, hotkey) in SUBNET_CONFIGS.items():
        stake = subnet_stakes.get(netuid, 0.0)
        validator = hotkey
        table.add_row(str(netuid), validator, f"{stake:.5f} ÃŽÂ±")
    return Panel(table, title="[bold blue]Subnet Allocations[/bold blue]", border_style="blue", box=box.ROUNDED, padding=(1, 1))

def create_history_panel(history: List[str]) -> Panel:
    table = Table(show_header=True, header_style="bold yellow", box=box.ROUNDED, expand=True)
    table.add_column("Time", width=8)
    table.add_column("Event", style="white")
    for entry in history[-10:]:
        if len(entry) > 50:
            event_str = entry
        else:
            event_str = entry
        parts = entry.split("] ", 1)
        if len(parts) == 2:
            time_str = parts[0].lstrip("[")
            event_str = parts[1]
        else:
            time_str = ""
        table.add_row(time_str, event_str)
    return Panel(table, title="[bold]Operation History[/bold]", border_style="yellow", box=box.ROUNDED, padding=(1, 2))

async def staking_manager(subtensor: bt.AsyncSubtensor, wallet: bt.wallet, live: Live):
    coldkey = wallet.coldkeypub.ss58_address
    root_hotkey = ROOT_HOTKEY
    total_required = sum(amount for amount, _ in SUBNET_CONFIGS.values())
    last_div_check = datetime.utcnow()

    current_stake = await get_stake(subtensor, coldkey, root_hotkey, ROOT_NETUID)
    balance = await get_balance(subtensor, wallet.coldkeypub.ss58_address)
    excess = current_stake.tao - MIN_ROOT_STAKE
    required_excess = MIN_STAKE_THRESHOLD * len(SUBNET_CONFIGS)
    next_div_check = last_div_check + DIVIDEND_CHECK_INTERVAL
    time_until_div = next_div_check - datetime.utcnow()

    subnet_stakes = {netuid: 0.0 for netuid in SUBNET_CONFIGS.keys()}

    schedule = read_schedule()
    original_next_staking = schedule["next_staking"] or next_staking_time()
    next_staking = original_next_staking

    if datetime.utcnow() > original_next_staking:
        console.print("[yellow]âš ï¸  Recovering missed scheduled stake[/yellow]")
        next_staking = datetime.utcnow()
        append_history("Recovering missed scheduled stake")

    async def update_dashboard():
        nonlocal current_stake, balance, excess, required_excess, next_div_check, time_until_div

        try:
            current_stake = await get_stake(subtensor, coldkey, root_hotkey, ROOT_NETUID)
            balance = await get_balance(subtensor, wallet.coldkeypub.ss58_address)

            for netuid in SUBNET_CONFIGS:
                stake = await get_stake(subtensor, coldkey, SUBNET_CONFIGS[netuid][1], netuid)
                subnet_stakes[netuid] = stake.tao

            excess = current_stake.tao - MIN_ROOT_STAKE
            required_excess = MIN_STAKE_THRESHOLD * len(SUBNET_CONFIGS)
            next_div_check = last_div_check + DIVIDEND_CHECK_INTERVAL
            time_until_div = next_div_check - datetime.utcnow()

            dividend_panel = create_dividend_panel(current_stake.tao, excess, required_excess, time_until_div)
            staking_panel = create_staking_panel(next_staking, balance.tao, total_required)
            subnet_panel = create_subnet_panel(subnet_stakes)
            history_panel = create_history_panel(history_log)

            top_row = Columns(
              [dividend_panel, staking_panel],
              equal=True,
              expand=False,
              padding=0,
              align="left"
            )

            dashboard = Group(
              top_row,
              subnet_panel,
              history_panel
            )

            live.update(dashboard)
        except Exception as e:
            logger.error(f"Dashboard update failed: {e}")
            live.update(Panel("[red]Dashboard update failed: Check logs[/red]", title="[bold]ALPHA Stake Manager[/bold]", border_style="red", box=box.ROUNDED))

    dashboard = Group(
        create_dividend_panel(current_stake.tao, excess, required_excess, timedelta(0)),
        create_staking_panel(next_staking, balance.tao, total_required),
        create_subnet_panel(subnet_stakes),
        create_history_panel(history_log)
    )
    live.update(dashboard)

    while True:
        try:
            await update_dashboard()

            if datetime.utcnow() >= last_div_check + DIVIDEND_CHECK_INTERVAL:
                if current_stake.tao > MIN_ROOT_STAKE and excess >= required_excess:
                    actual_unstaked = await unstake_excess(subtensor, wallet, ROOT_NETUID, root_hotkey, excess)
                    if actual_unstaked > 0:
                        per_subnet = actual_unstaked / len(SUBNET_CONFIGS)
                        successful_subnets = 0
                        for netuid, (amount, hotkey) in SUBNET_CONFIGS.items():
                            try:
                                await process_subnet(subtensor, wallet, netuid, per_subnet, hotkey)
                                successful_subnets += 1
                                await update_dashboard()
                                await asyncio.sleep(0)
                            except Exception as e:
                                logger.error(f"Subnet {netuid} processing failed: {e}")
                                append_history(f"Subnet {netuid} distribution failure")
                        efficiency = (successful_subnets / len(SUBNET_CONFIGS) * 100)
                        append_history(f"Distributed {actual_unstaked:.5f} TAO (Coverage: {efficiency:.1f}%)")
                    else:
                        append_history("No funds available for distribution")
                else:
                    append_history("Dividend check - insufficient excess")
                last_div_check = datetime.utcnow()

            if datetime.utcnow() >= next_staking and balance.tao >= total_required:
                for netuid, (amount, hotkey) in SUBNET_CONFIGS.items():
                    await process_subnet(subtensor, wallet, netuid, amount, hotkey)

                new_next = original_next_staking + STAKE_INTERVAL
                while new_next < datetime.utcnow():
                    new_next += STAKE_INTERVAL

                write_schedule(new_next)
                next_staking = new_next
                original_next_staking = new_next
                append_history(f"Scheduled stake completed. Next at {new_next.strftime('%H:%M UTC')}")

            await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Manager error: {str(e)}")
            live.update(Panel(
                "[red]Error occurred - check logs[/red]",
                title="[bold]ALPHA Stake Manager[/bold]",
                border_style="red",
                box=box.ROUNDED
            ))
            append_history("Error occurred in manager")
            await asyncio.sleep(10)

async def main():
    console.print(Panel.fit(
        "[bold #38bdf8]Initializing ALPHA Stake Manager...[/bold #38bdf8]",
        title="Startup Sequence",
        border_style="#1e40af",
        style="on #0F172A"
    ))

    subtensor = bt.AsyncSubtensor('ws://subvortex.info:9944')
    await subtensor.initialize()

    with Live(console=console, refresh_per_second=1, vertical_overflow="visible") as live:
        try:
            await staking_manager(subtensor, wallet, live)
        except KeyboardInterrupt:
            console.print("[bold red]Shutting down gracefully...[/bold red]")

if __name__ == "__main__":
    asyncio.run(main())
