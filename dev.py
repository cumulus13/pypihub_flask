#!/usr/bin/env python3
"""
File: dev.py
Author: Hadi Cahyadi <cumulus13@gmail.com>
Date: 2025-12-31
Description: File synchronization monitor for development
License: MIT
"""

import os
import sys

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

if len(sys.argv) > 1 and any('--debug' == arg for arg in sys.argv):
    print("🐞 Debug mode enabled [DEV]")
    os.environ["DEBUG"] = "1"
    os.environ['LOGGING'] = "1"
    os.environ.pop('NO_LOGGING', None)
    os.environ['TRACEBACK'] = "1"
    os.environ["LOGGING"] = "1"
    LOG_LEVEL = "DEBUG"
    print("start load 'pydebugger' module ...")
    print("finish load 'pydebugger' module ...")

elif str(os.getenv('DEBUG', '0')).lower() in ['1', 'true', 'ok', 'on', 'yes']:
    print("🐞 Debug mode enabled [DEV]")
    os.environ['LOGGING'] = "1"
    os.environ.pop('NO_LOGGING', None)
    os.environ['TRACEBACK'] = "1"
    os.environ["LOGGING"] = "1"
    LOG_LEVEL = "DEBUG"
    print("start load 'pydebugger' module ...")
    print("finish load 'pydebugger' module ...")
else:
    os.environ['NO_LOGGING'] = "1"

    def debug(*args, **kwargs):
        pass

exceptions = []

try:
    from richcolorlog import setup_logging, print_exception as tprint  # type: ignore
    logger = setup_logging('pypihub-dev', exceptions=exceptions, level=LOG_LEVEL)
except:
    import logging

    for exc in exceptions:
        logging.getLogger(exc).setLevel(logging.CRITICAL)
    
    try:
        from .custom_logging import get_logger  # type: ignore
    except ImportError:
        from custom_logging import get_logger  # type: ignore
        
    LOG_LEVEL = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    logger = get_logger('pypihub-dev', level=LOG_LEVEL)


import time
import hashlib
import shutil
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import argparse
try:
    from licface import CustomRichHelpFormatter
except ImportError:
    CustomRichHelpFormatter = argparse.HelpFormatter

# Try to import optional dependencies with fallbacks
try:
    from gntp.notifier import GrowlNotifier
    GROWL_AVAILABLE = True
except ImportError:
    GROWL_AVAILABLE = False
    GrowlNotifier = None

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text
    from rich.layout import Layout
    from rich.columns import Columns
    # from rich.spinner import Spinner
    # from rich.progress import Progress, SpinnerColumn, TextColumn
    # from rich.syntax import Syntax
    from rich import box
    RICH_AVAILABLE = True
    console = Console()
except ImportError:
    RICH_AVAILABLE = False

@dataclass
class FilePair:
    """Represents a source-target file pair for synchronization"""
    source: Path
    target: Path
    last_hash: Optional[str] = None
    last_sync: Optional[datetime] = None
    status: str = "pending"
    error_count: int = 0
    
    def validate(self) -> Tuple[bool, str]:
        """Validate the file pair"""
        if not self.source.exists():
            return False, f"Source file does not exist: {self.source}"
        if not self.source.is_file():
            return False, f"Source is not a file: {self.source}"
        return True, ""

class SyncMonitor:
    """Main synchronization monitor class"""
    
    def __init__(
        self,
        file_pairs: List[FilePair],
        check_interval: float = 1.0,
        enable_notifications: bool = True,
        config_file: Optional[Path] = None
    ):
        self.file_pairs = file_pairs
        self.check_interval = check_interval
        self.enable_notifications = enable_notifications
        self.running = False
        self.last_update = datetime.now()
        
        # Load configuration if provided
        self.config = self._load_config(config_file) if config_file else {}
        
        # Initialize notifier
        self.notifier = self._init_notifier() if enable_notifications else None
        
        # Statistics
        self.stats = {
            'sync_count': 0,
            'error_count': 0,
            'start_time': None,
            'last_sync': None,
            'total_files': len(file_pairs),
            'synced_files': 0,
            'uptime': timedelta(0)
        }
        
        # Live display components
        self.live = None
        self.layout = None
        if RICH_AVAILABLE:
            self._setup_layout()
    
    def _setup_layout(self):
        """Setup Rich layout for live display"""
        self.layout = Layout()
        
        # Split into main content and status bar
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main", ratio=1),
            Layout(name="footer", size=3)
        )
        
        # Split main into left (files) and right (stats)
        self.layout["main"].split_row(
            Layout(name="files", ratio=2),
            Layout(name="stats", ratio=1)
        )
    
    def _create_header(self) -> Panel:
        """Create header panel"""
        title = Text("📁 PyPIHub Sync Monitor", style="bold cyan")
        subtitle = Text("Real-time file synchronization", style="dim")
        
        if self.running:
            status = Text(" 🟢 ") + Text("RUNNING ", style="bold #00FF00")
        else:
            status = Text(" 🟥 ") + Text("STOPPED ", style="bold white on red")
        
        return Panel(
            Group(title, subtitle),
            title=status,
            border_style="cyan",
            padding=(0, 1)
        )
    
    def _create_files_table(self) -> Table:
        """Create files status table"""
        table = Table(
            title="[bold]Files Being Monitored[/bold]",
            box=box.ROUNDED,
            show_header=True,
            header_style="bold magenta",
            expand=True
        )
        
        table.add_column("#", style="dim", width=3)
        table.add_column("File", style="cyan", width=25)
        table.add_column("Status", width=10)
        table.add_column("Last Sync", width=12)
        table.add_column("Size", width=8, justify="right")
        
        for idx, pair in enumerate(self.file_pairs, 1):
            # File name with truncation if needed
            filename = pair.source.name
            if len(filename) > 20:
                filename = filename[:17] + "..."
            
            # Status styling
            if pair.status == "synced":
                status_icon = "✅"
                status_text = "synced"
                status_style = "green"
            elif pair.status == "error":
                status_icon = "❌"
                status_text = f"error({pair.error_count})"
                status_style = "red"
            elif pair.status == "syncing":
                status_icon = "🔄"
                status_text = "syncing"
                status_style = "yellow"
            else:
                status_icon = "⏳"
                status_text = "pending"
                status_style = "dim"
            
            # Last sync time
            if pair.last_sync:
                last_sync = pair.last_sync.strftime("%H:%M:%S")
                if (datetime.now() - pair.last_sync).seconds < 10:
                    sync_style = "bold green"
                elif (datetime.now() - pair.last_sync).seconds < 60:
                    sync_style = "green"
                else:
                    sync_style = "dim"
            else:
                last_sync = "never"
                sync_style = "dim"
            
            # File size
            try:
                size = pair.source.stat().st_size
                if size < 1024:
                    size_text = f"{size}B"
                elif size < 1024 * 1024:
                    size_text = f"{size/1024:.1f}KB"
                else:
                    size_text = f"{size/(1024*1024):.1f}MB"
            except:
                size_text = "N/A"
            
            table.add_row(
                f"{idx}",
                filename,
                f"{status_icon} {status_text}",
                f"[{sync_style}]{last_sync}[/]",
                f"[dim]{size_text}[/]"
            )
        
        return table
    
    def _create_stats_panel(self) -> Panel:
        """Create statistics panel"""
        # Uptime calculation
        if self.stats['start_time']:
            uptime = datetime.now() - self.stats['start_time']
            self.stats['uptime'] = uptime
            uptime_str = str(uptime).split('.')[0]  # Remove microseconds
        else:
            uptime_str = "00:00:00"
        
        # Create stats content
        stats_content = []
        
        # Uptime and interval
        
        stats_content.append(Text("📈 Uptime: ", style="dim") + Text(f"{uptime_str}", style="cyan"))
        stats_content.append(
            Text(f"⏰ Interval: ", style="dim") + Text(f"{self.check_interval}s", style="cyan")
        )
        stats_content.append(Text(""))  # Spacer
        
        # Sync statistics
        sync_rate = self.stats['sync_count'] / max(self.stats['uptime'].seconds, 1)
        stats_content.append(
            Text(f"📊 Total Syncs: ", style="dim") + \
            Text(f"{self.stats['sync_count']}", style="green")
        )

        stats_content.append(
            Text(f"⚡ Sync Rate: ", style="dim") + \
            Text(f"{sync_rate:.2f}/sec", style="yellow")
        )
        
        # Error statistics
        if self.stats['error_count'] > 0:
            stats_content.append(
                Text(f"❌ Errors: ", style="dim") + \
                Text(f"{self.stats['error_count']}", style="bold red")
            )
        else:
            stats_content.append(
                Text(f"✅ Errors: ", style="dim") + \
                Text(f"{self.stats['error_count']}", style="bold red")
            )
        
        # File statistics
        synced = sum(1 for p in self.file_pairs if p.status == "synced")
        self.stats['synced_files'] = synced
        stats_content.append(Text(""))  # Spacer
        stats_content.append(
            Text(f"📁 Total Files: ", style="dim") + \
            Text(f"{self.stats['total_files']}", style="bold #00FFFF")
        )
        stats_content.append(
            Text(f"✅ Synced: ", style="dim") + \
            Text(f"{synced}", style="bold #00FF00")
        )
        stats_content.append(
            Text(f"⏳ Pending: ", style="dim") + \
            Text(f"{self.stats['total_files'] - synced}", style="bold #FFFF00"))
        
        # Last sync time
        if self.stats['last_sync']:
            last_sync_diff = (datetime.now() - self.stats['last_sync']).seconds
            if last_sync_diff < 5:
                last_sync_style = "bold green"
            elif last_sync_diff < 60:
                last_sync_style = "green"
            else:
                last_sync_style = "yellow"
            
            last_sync_str = self.stats['last_sync'].strftime("%H:%M:%S")
            stats_content.append(Text(""))  # Spacer
            stats_content.append(
                Text(f"🕒 Last Sync: ", style="dim") + \
                Text(f"{last_sync_str}", style=last_sync_style)
            )
        
        return Panel(
            Group(*stats_content),
            title="[bold]Statistics[/bold]",
            border_style="magenta",
            padding=(0, 1)
        )
    
    def _create_footer(self) -> Panel:
        """Create footer panel"""
        now = datetime.now()
        footer_text = Columns([
            Text(f"📅 {now.strftime('%Y-%m-%d')}", style="dim"),
            Text(f"🕐 {now.strftime('%H:%M:%S')}", style="dim"),
            Text("Press ", style="dim"),
            Text("Ctrl+C", style="bold red"),
            Text("to stop", style="dim"),
        ])
        
        return Panel(
            footer_text,
            border_style="dim",
            padding=(0, 1)
        )
    
    def _create_live_display(self) -> Group:
        """Create the complete live display"""
        if not self.layout:
            return Group(Text("Rich not available", style="bold red"))
        
        # Update layout with current content
        self.layout["header"].update(self._create_header())
        self.layout["files"].update(self._create_files_table())
        self.layout["stats"].update(self._create_stats_panel())
        self.layout["footer"].update(self._create_footer())
        
        return self.layout
    
    def _display_status_live(self):
        """Display status using Live display"""
        if not RICH_AVAILABLE or not self.live:
            return
        
        self.live.update(self._create_live_display())
    
    def _load_config(self, config_path: Path) -> Dict:
        """Load configuration from JSON file"""
        try:
            with open(config_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load config: {e}")
            return {}
    
    def _init_notifier(self):
        """Initialize notification system"""
        if not GROWL_AVAILABLE:
            logger.warning("Growl notifications not available (gntp not installed)")
            return None
        
        try:
            icon_file = Path(__file__).parent / "pypihub.png"
            icon_data = None
            
            if icon_file.exists():
                with open(icon_file, "rb") as f:
                    icon_data = f.read()
            
            growl = GrowlNotifier(  # type: ignore
                applicationName="PyPIHub Sync",
                notifications=["file_changed", "sync_error"],
                defaultNotifications=["file_changed"],
                applicationIcon=icon_data,
            )
            growl.register()
            return growl
        except Exception as e:
            logger.exception(f"Failed to initialize notifier: {e}")
            return None
    
    def _send_notification(self, title: str, message: str, note_type: str = "file_changed"):
        """Send notification if enabled"""
        if self.notifier and self.enable_notifications:
            try:
                icon_file = Path(__file__).parent / "pypihub.png"
                icon_data = None
                
                if icon_file.exists():
                    with open(icon_file, "rb") as f:
                        icon_data = f.read()
                
                self.notifier.notify(
                    noteType=note_type,
                    title=title,
                    description=message,
                    icon=icon_data,
                    sticky=False,
                    priority=1,
                )
            except Exception as e:
                logger.warning(f"Failed to send notification: {e}")
    
    @staticmethod
    def calculate_file_hash(file_path: Path) -> Optional[str]:
        """Calculate SHA256 hash of a file"""
        try:
            hasher = hashlib.sha256()
            with open(file_path, 'rb') as f:
                # Read in chunks to handle large files
                for chunk in iter(lambda: f.read(4096), b''):
                    hasher.update(chunk)
            return hasher.hexdigest()
        except Exception as e:
            logger.exception(f"Failed to hash file {file_path}: {e}")
            return None
    
    def sync_file(self, file_pair: FilePair) -> bool:
        """Synchronize a single file pair"""
        try:
            # Update status to syncing
            file_pair.status = "syncing"
            self._display_status_live()
            
            # Calculate hashes
            source_hash = self.calculate_file_hash(file_pair.source)
            if source_hash is None:
                file_pair.status = "error"
                file_pair.error_count += 1
                return False
            
            target_hash = None
            if file_pair.target.exists():
                target_hash = self.calculate_file_hash(file_pair.target)
            
            # Check if synchronization is needed
            if source_hash == target_hash:
                file_pair.status = "synced"
                self._display_status_live()
                return True
            
            # Perform synchronization
            logger.info(f"Syncing {file_pair.source} -> {file_pair.target}")
            
            # Ensure target directory exists
            file_pair.target.parent.mkdir(parents=True, exist_ok=True)
            
            # Copy file with metadata
            shutil.copy2(file_pair.source, file_pair.target)
            
            # Update file pair status
            file_pair.last_hash = source_hash
            file_pair.last_sync = datetime.now()
            file_pair.status = "synced"
            
            # Update statistics
            self.stats['sync_count'] += 1
            self.stats['last_sync'] = datetime.now()
            self.last_update = datetime.now()
            
            # Log and notify
            message = f"{file_pair.source.name} synchronized successfully"
            logger.info(message)
            
            if RICH_AVAILABLE:
                console.print(f"[green]✅[/green] {message}")
            
            self._send_notification(
                "File Synchronized",
                f"{file_pair.source.name} → {file_pair.target}",
                "file_changed"
            )
            
            return True
            
        except Exception as e:
            error_msg = f"Failed to sync {file_pair.source}: {e}"
            logger.exception(error_msg)
            
            file_pair.status = "error"
            file_pair.error_count += 1
            self.stats['error_count'] += 1
            
            if RICH_AVAILABLE:
                console.print(f"[red]❌[/red] {error_msg}")
            
            self._send_notification(
                "Sync Error",
                error_msg,
                "sync_error"
            )
            
            return False
        finally:
            self._display_status_live()
    
    def validate_all_pairs(self) -> bool:
        """Validate all file pairs before starting"""
        all_valid = True
        for pair in self.file_pairs:
            is_valid, message = pair.validate()
            if not is_valid:
                logger.exception(message)
                all_valid = False
        return all_valid
    
    def display_initial_info(self):
        """Display initial information"""
        if RICH_AVAILABLE:
            console.clear()
            console.print(Panel(
                "[bold cyan]📁 PyPIHub Sync Monitor[/bold cyan]\n"
                "[dim]Starting file synchronization...[/dim]",
                border_style="cyan"
            ))
            
            # Show file pairs
            table = Table(title="File Pairs to Monitor", box=box.SIMPLE)
            table.add_column("Source", style="cyan")
            table.add_column("Target", style="magenta")
            table.add_column("Status", style="green")
            
            for pair in self.file_pairs:
                table.add_row(
                    str(pair.source),
                    str(pair.target),
                    "[yellow]⏳ pending[/]"
                )
            
            console.print(table)
            console.print("\n[dim]Initializing live display...[/dim]\n")
    
    def run(self):
        """Main monitoring loop"""
        self.running = True
        self.stats['start_time'] = datetime.now()
        
        # Display initial information
        self.display_initial_info()
        
        # Validate before starting
        if not self.validate_all_pairs():
            logger.exception("Validation failed. Exiting.")
            return
        
        # Initial sync
        logger.info("Performing initial sync...")
        for pair in self.file_pairs:
            self.sync_file(pair)
        
        # Setup live display
        if RICH_AVAILABLE:
            with Live(
                self._create_live_display(),
                console=console,
                refresh_per_second=4,  # Smooth refresh rate
                screen=True,  # Clear screen on start
                vertical_overflow="visible"
            ) as self.live:
                self._monitor_loop()
        else:
            # Fallback to simple display
            print("\n[Sync Monitor Started]")
            print("Press Ctrl+C to stop\n")
            self._monitor_loop()
    
    def _monitor_loop(self):
        """Main monitoring loop without Live context manager"""
        try:
            while self.running:
                start_time = time.time()
                
                # Sync all files
                for pair in self.file_pairs:
                    self.sync_file(pair)
                    time.sleep(0.1)  # Small delay between files for smooth display
                
                # Update display
                self._display_status_live()
                
                # Calculate sleep time to maintain consistent interval
                elapsed = time.time() - start_time
                sleep_time = max(0.1, self.check_interval - elapsed)
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user")
            print("\n[bold yellow]Monitor stopped[/bold yellow]" if RICH_AVAILABLE else "\nMonitor stopped")
        except Exception as e:
            logger.exception(f"Monitor error: {e}")
            raise
        finally:
            self.stop()
    
    def stop(self):
        """Stop the monitor"""
        self.running = False
        logger.info("Monitor stopped")
        
        # Display final statistics
        if self.stats['start_time']:
            duration = datetime.now() - self.stats['start_time']
            logger.info(f"Total runtime: {duration}")
            
            if RICH_AVAILABLE:
                # Final summary
                console.print(Panel(
                    f"[bold]Summary:[/bold]\n"
                    f"  Runtime: {str(duration).split('.')[0]}\n"
                    f"  Files synced: {self.stats['sync_count']}\n"
                    f"  Errors: {self.stats['error_count']}\n"
                    f"  Files monitored: {self.stats['total_files']}",
                    title="[bold green]✓ Monitor Finished[/bold green]",
                    border_style="green"
                ))
            else:
                print(f"\n{'='*60}")
                print("Summary:")
                print(f"  Runtime: {duration}")
                print(f"  Files synced: {self.stats['sync_count']}")
                print(f"  Errors: {self.stats['error_count']}")
                print(f"  Files monitored: {self.stats['total_files']}")
                print(f"{'='*60}")

def create_file_pairs_from_config(config: Dict) -> List[FilePair]:
    """Create file pairs from configuration"""
    file_pairs = []
    
    # Method 1: Direct mapping from config
    if 'file_mappings' in config:
        for mapping in config['file_mappings']:
            source = Path(mapping['source']).resolve()
            target = Path(mapping['target']).resolve()
            file_pairs.append(FilePair(source, target))
    
    # Method 2: Source and target directories
    elif 'source_dir' in config and 'target_dir' in config:
        source_dir = Path(config['source_dir']).resolve()
        target_dir = Path(config['target_dir']).resolve()
        
        # Get all files from source directory
        for pattern in config.get('patterns', ['*.py', '*.ini', '*.txt']):
            for source_file in source_dir.rglob(pattern):
                if source_file.is_file():
                    # Maintain relative path structure
                    rel_path = source_file.relative_to(source_dir)
                    target_file = target_dir / rel_path
                    file_pairs.append(FilePair(source_file, target_file))
    
    return file_pairs

def main():
    """Main entry point
    
    example config file:
    {
      "source_dir": "./pypihub",
      "target_dir": "c:/PROJECTS/containers/pypihub/pypihub",
      "patterns": ["*.py", "*.ini", "*.txt", "*.json"],
      "check_interval": 1.0,
      "enable_notifications": true
    }
    """
    
    NAME = os.path.basename(os.getcwd())

    parser = argparse.ArgumentParser(
        description=f"File synchronization monitor for: '{NAME}'", 
        prog='dev', 
        formatter_class=CustomRichHelpFormatter
    )
    parser.add_argument('--config', '-c', type=Path, help='Configuration file (JSON)')
    parser.add_argument('--interval', '-i', type=float, default=1.0, help='Check interval in seconds')
    parser.add_argument('--no-notify', action='store_true', help='Disable notifications')
    parser.add_argument('--validate', action='store_true', help='Validate configuration and exit')
    parser.add_argument('--simple', action='store_true', help='Use simple console output (no rich)')
    
    args = parser.parse_args()
    
    # Load configuration
    if args.config and args.config.exists():
        with open(args.config, 'r') as f:
            config = json.load(f)
        file_pairs = create_file_pairs_from_config(config)
    else:
        # Fallback to hardcoded paths
        script_dir = Path(__file__).parent
        file_pairs = [
            FilePair(
                script_dir / 'pypihub' / 'pypihub.py',
                Path(r'c:\PROJECTS\containers\pypihub\pypihub\pypihub.py')
            ),
            FilePair(
                script_dir / 'pypihub' / 'pypihub.ini',
                Path(r'c:\PROJECTS\containers\pypihub\pypihub\pypihub.ini')
            ),
            FilePair(
                script_dir / 'pypihub' / 'database.py',
                Path(r'c:\PROJECTS\containers\pypihub\pypihub\database.py')
            ),
            FilePair(
                script_dir / 'pypihub' / 'logger.py',
                Path(r'c:\PROJECTS\containers\pypihub\pypihub\logger.py')
            ),
            FilePair(
                script_dir / 'pypihub' / 'settings.py',
                Path(r'c:\PROJECTS\containers\pypihub\pypihub\settings.py')
            ),
            FilePair(
                script_dir / 'pypihub' / '__init__.py',
                Path(r'c:\PROJECTS\containers\pypihub\pypihub\__init__.py')
            ),
        ]
    
    # Force simple mode if requested
    global RICH_AVAILABLE
    if args.simple:
        RICH_AVAILABLE = False
    
    # Create monitor
    monitor = SyncMonitor(
        file_pairs=file_pairs,
        check_interval=args.interval,
        enable_notifications=not args.no_notify,
        config_file=args.config
    )
    
    if args.validate:
        print("Configuration validation:")
        for i, pair in enumerate(file_pairs, 1):
            is_valid, message = pair.validate()
            status = "✅" if is_valid else "❌"
            print(f"{i}. {status} {pair.source} -> {pair.target}")
            if not is_valid:
                print(f"   Error: {message}")
        sys.exit(0 if all(p.validate()[0] for p in file_pairs) else 1)
    
    # Run monitor
    try:
        monitor.run()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
