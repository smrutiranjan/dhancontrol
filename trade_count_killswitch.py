#!/usr/bin/env python3
"""
Trade-count monitor and Dhan P&L exit controller.

Reads optional limits from cred.yml, configures and verifies Dhan's automatic
P&L exit when enabled, treating broker-accepted registration as armed even if
the status endpoint initially reports PENDING, then polls Dhan's order list
every five seconds. When
the configured number of orders reaches TRADED status, it activates Dhan's
broker kill switch. It also stops on a non-trading day or at the daily
Asia/Kolkata cutoff (15:10). Logs are written under ./log.

For testing startup outside market days or hours, set ``test_mode: true`` in
``cred.yml`` (or set ``DHAN_TEST_MODE=1``) to bypass the startup calendar and
run-window checks. This still connects to the configured broker account and
does not bypass the daily 15:10 IST cutoff.

See trade_count_killswitch.md for configuration, startup, and operational
details.
"""

import math
import os
import signal
import sys
import threading
import time
import yaml
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # type: ignore[misc, assignment]
import logging

try:
    from holiday_stop_trade_killswitch import is_indian_equity_trading_day
except ImportError:
    is_indian_equity_trading_day = None  # type: ignore[misc, assignment]

# Import Dhan API with proper error handling
try:
    from dhanhq import dhanhq, DhanContext
    DHAN_AVAILABLE = True
except ImportError as e:
    print(f"Error: Dhan API not available - {e}")
    dhanhq = None
    DhanContext = None
    DHAN_AVAILABLE = False

class TradeCountKillSwitch:
    """Subclass may set log_file_prefix and app_banner_title for distinct cron logs."""

    log_file_prefix = "trade_count_killswitch"
    app_banner_title = "Trade Count Kill Switch Monitor"
    # Daily exit — India Standard Time (NSE cash session context)
    auto_stop_time_ist = (15, 10)  # (hour, minute) = 3:10 PM IST; process exits completely after this

    def __init__(self):
        """Initialize the trade count kill switch monitor"""
        # Setup logging first
        self.setup_logging()
        # Load user settings (max_trades) from cred.yml before anything uses them
        self.load_config()
        self._exit_if_outside_run_window()
        self._exit_if_non_trading_day()

        # Cooperative stop: SIGTERM from stop_trade_count_killswitch.sh, SIGINT from terminal
        self._stop_monitoring = threading.Event()
        self._session_end_stop = False  # True when daily 3:10 PM IST cutoff triggers exit
        self._install_stop_signals()

        # max_trades comes from cred.yml (see load_config); kill switch fires at that trade count.
        self.monitoring_interval = 5  # Check every 5 seconds (compliant with Dhan recommendations)
        
        self.dhan = None
        self.credentials = None
        self.kill_switch_activated = False
        self.activation_count = 0
        self.max_activations = 2  # Allow up to 2 activations
        self.pnl_exit_configured = False
        self._order_api_broken = False  # tracks get_order_list() failure state, to log only on transitions

        self.log("=" * 60)
        self.log(type(self).app_banner_title)
        self.log(f"Max Trades: {self.max_trades}")
        self.log(f"Monitoring Interval: {self.monitoring_interval} seconds")
        self.log(f"Max Activations: {self.max_activations}")
        if self.pnl_exit_config is not None:
            self.log(
                "P&L Exit: "
                f"profit ₹{self.pnl_exit_config['profit_value']:,.2f}, "
                f"loss ₹{self.pnl_exit_config['loss_value']:,.2f}, "
                f"products {self.pnl_exit_config['product_type']}, "
                f"enable kill switch {self.pnl_exit_config['enable_kill_switch']}"
            )
        hh, mm = getattr(type(self), "auto_stop_time_ist", (15, 10))
        self.log(f"Daily auto-stop: {hh:02d}:{mm:02d} IST — process exits completely (Asia/Kolkata)")
        self.log("=" * 60)
        
        # Load credentials and initialize
        if not self.load_credentials():
            self.log("❌ Failed to load credentials. Exiting.", level="ERROR")
            sys.exit(1)
        
        # Test API connection
        if not self.test_api_connection():
            self.log("❌ Failed to test API connection. Exiting.", level="ERROR")
            sys.exit(1)

        self.log("✅ Initialization complete. Starting monitoring...")
        self.log("")
    
    def setup_logging(self):
        """Setup logging to both console and file"""
        try:
            # Get log directory
            if getattr(sys, 'frozen', False):
                base_path = os.path.dirname(sys.executable)
            else:
                base_path = os.path.dirname(os.path.abspath(__file__))
            
            log_dir = os.path.join(base_path, 'log')
            os.makedirs(log_dir, exist_ok=True)
            
            # Log file with date (subclasses may override log_file_prefix)
            prefix = getattr(type(self), "log_file_prefix", "trade_count_killswitch")
            log_file = os.path.join(log_dir, f"{prefix}_{datetime.now().strftime('%Y%m%d')}.log")
            
            # Configure logging
            # Only add StreamHandler if stdout is a TTY (interactive mode)
            # This prevents duplicate logs when stdout is redirected to a file (e.g., via cron)
            handlers = [logging.FileHandler(log_file, mode='a', encoding='utf-8')]
            
            # Mirror logs to the desktop wrapper when explicitly requested.
            if sys.stdout.isatty() or os.environ.get("DHAN_GUI") == "1":
                handlers.append(logging.StreamHandler(sys.stdout))
            
            logging.basicConfig(
                level=logging.INFO,
                format='[%(asctime)s] %(levelname)s: %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S',
                handlers=handlers
            )
            
            # Force immediate flush
            sys.stdout.flush()
            sys.stderr.flush()
            
            self.logger = logging.getLogger(__name__)
            self.log_file = log_file
            self.log(f"Logging initialized. Log file: {log_file}")
            
        except Exception as e:
            # Fallback to print if logging setup fails
            print(f"Warning: Failed to setup logging: {e}")
            sys.stdout.flush()
            self.logger = None
            self.log_file = None

    def _base_path(self) -> str:
        if getattr(sys, "frozen", False):
            return os.path.dirname(sys.executable)
        return os.path.dirname(os.path.abspath(__file__))

    def load_config(self) -> None:
        """
        Load user-tunable settings from cred.yml. Falls back to safe defaults if a key is
        absent or the file can't be read.

        Keys (all optional):
            test_mode: bypass weekday, market-hours, and exchange-calendar startup checks
            max_trades: number of completed trades that trips the kill switch, e.g. 16
            max_daily_profit: positive daily profit threshold in rupees
            max_daily_loss: positive daily loss threshold in rupees
            pnl_exit:
                enabled: true
                product_type: [INTRADAY, DELIVERY]
                enable_kill_switch: false
        """
        # Default (used when the key is missing or the file can't be read)
        self.max_trades = 16
        self.pnl_exit_config = None
        self.test_mode = os.environ.get("DHAN_TEST_MODE") == "1"

        cred_file = os.path.join(self._base_path(), "cred.yml")
        try:
            with open(cred_file, "r") as f:
                cfg = yaml.load(f, Loader=yaml.FullLoader) or {}
        except Exception as e:
            self.log(f"config: could not read {cred_file} for settings ({e}); using defaults.", level="WARNING")
            return
        if not isinstance(cfg, dict):
            raise ValueError("config: cred.yml must contain a YAML mapping")

        if "test_mode" in cfg:
            if not isinstance(cfg["test_mode"], bool):
                raise ValueError("config: test_mode must be a boolean in cred.yml")
            self.test_mode = self.test_mode or cfg["test_mode"]

        if self.test_mode:
            self.log(
                "Startup test mode enabled; weekday, market-hours, and exchange-calendar "
                "checks are bypassed. Broker/API access remains enabled.",
                level="WARNING",
            )

        if "max_trades" in cfg:
            try:
                max_trades = int(cfg["max_trades"])
                if max_trades < 1:
                    raise ValueError("must be >= 1")
                self.max_trades = max_trades
            except (TypeError, ValueError) as e:
                self.log(f"config: invalid max_trades={cfg['max_trades']!r} ({e}); keeping default {self.max_trades}.", level="WARNING")

        pnl_cfg = cfg.get("pnl_exit", {})
        if not isinstance(pnl_cfg, dict):
            raise ValueError("config: pnl_exit must be a mapping in cred.yml")
        enabled = pnl_cfg.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ValueError("config: pnl_exit.enabled must be a boolean")
        if not enabled:
            return

        try:
            if "max_daily_profit" not in cfg or "max_daily_loss" not in cfg:
                raise ValueError(
                    "max_daily_profit and max_daily_loss must both be set at the top level "
                    "of cred.yml when pnl_exit.enabled is true"
                )
            if isinstance(cfg["max_daily_profit"], bool) or isinstance(cfg["max_daily_loss"], bool):
                raise ValueError("max_daily_profit and max_daily_loss must be numeric thresholds")
            profit_value = float(cfg["max_daily_profit"])
            loss_value = float(cfg["max_daily_loss"])
            product_type = pnl_cfg.get("product_type", ["INTRADAY", "DELIVERY"])
            enable_kill_switch = pnl_cfg.get("enable_kill_switch", False)

            if (
                not math.isfinite(profit_value)
                or not math.isfinite(loss_value)
                or profit_value <= 0
                or loss_value <= 0
            ):
                raise ValueError("max_daily_profit and max_daily_loss must be finite positive amounts")
            if not isinstance(product_type, list) or not product_type:
                raise ValueError("product_type must be a non-empty list")
            product_type = [str(value).upper() for value in product_type]
            if any(value not in {"INTRADAY", "DELIVERY"} for value in product_type):
                raise ValueError("product_type values must be INTRADAY or DELIVERY")
            if not isinstance(enable_kill_switch, bool):
                raise ValueError("enable_kill_switch must be a boolean")
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(f"config: invalid P&L exit settings in cred.yml: {e}") from e

        self.pnl_exit_config = {
            "profit_value": profit_value,
            "loss_value": loss_value,
            "product_type": product_type,
            "enable_kill_switch": enable_kill_switch,
        }

    def _exit_if_non_trading_day(self) -> None:
        """Skip credentials/API when Indian equity session is closed (see holiday_stop_trade_killswitch)."""
        if self.test_mode:
            self.log("Startup test mode: skipping the exchange-calendar check.", level="WARNING")
            return
        if is_indian_equity_trading_day is None:
            self.log(
                "holiday_stop: holiday_stop_trade_killswitch module not importable; skipping calendar check",
                level="WARNING",
            )
            return
        today = self._now_ist().date()
        trading, reason = is_indian_equity_trading_day(today, base_dir=self._base_path())
        if trading:
            return
        self.log("=" * 60)
        self.log(
            f"holiday_stop: today is holiday_stop (non-trading {today.isoformat()}); exiting before API/credentials."
        )
        self.log(f"holiday_stop: calendar detail: {reason}")
        self.log("=" * 60)
        sys.exit(0)

    def _exit_if_outside_run_window(self) -> None:
        """Skip startup outside weekdays 09:15-15:10 in Asia/Kolkata."""
        if self.test_mode:
            self.log(
                "Startup test mode: skipping the weekday and market-hours check. "
                "Broker/API access remains enabled.",
                level="WARNING",
            )
            return
        now = self._now_ist()
        outside_window = (
            now.weekday() >= 5
            or (now.hour, now.minute) < (9, 15)
            or (now.hour, now.minute) >= (15, 10)
        )
        if outside_window:
            self.log(
                "Outside the Monday-Friday, 09:15-15:10 IST run window; exiting before credentials/API.",
            )
            sys.exit(0)

    def _install_stop_signals(self) -> None:
        """Register SIGINT/SIGTERM so stop_trade_count_killswitch.sh can exit the loop promptly."""

        def _handler(signum: int, frame: Any) -> None:
            self._stop_monitoring.set()

        try:
            signal.signal(signal.SIGINT, _handler)
        except ValueError:
            pass
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, _handler)
            except ValueError:
                pass
        if hasattr(signal, "SIGBREAK"):
            try:
                signal.signal(signal.SIGBREAK, _handler)
            except ValueError:
                pass

    def _interruptible_sleep(self, seconds: float) -> bool:
        """Wait up to ``seconds`` seconds. Returns True if stop was requested (signal / stop script)."""
        return self._stop_monitoring.wait(timeout=seconds)

    def _now_ist(self) -> datetime:
        """Current time in Asia/Kolkata (IST). Falls back to UTC+05:30 if tzdata/zoneinfo missing."""
        try:
            if ZoneInfo is not None:
                return datetime.now(ZoneInfo("Asia/Kolkata"))
        except Exception:
            pass
        ist = timezone(timedelta(hours=5, minutes=30))
        return datetime.now(ist)

    def _daily_cutoff_reached(self) -> bool:
        """True when IST clock is at or past ``auto_stop_time_ist`` (default 3:10 PM)."""
        now = self._now_ist()
        h, m = getattr(type(self), "auto_stop_time_ist", (15, 10))
        return (now.hour, now.minute) >= (h, m)

    def _seconds_until_daily_cutoff_ist(self) -> float:
        """Seconds until today's IST cutoff, or 0.0 if already past cutoff."""
        now = self._now_ist()
        h, m = getattr(type(self), "auto_stop_time_ist", (15, 10))
        cutoff = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now >= cutoff:
            return 0.0
        return max(0.0, (cutoff - now).total_seconds())

    @staticmethod
    def _kill_switch_text_means_active(text: str) -> bool:
        """
        Parse a kill-switch status string from Dhan GET /killswitch data.

        Important: do NOT use naive substring ``\"ACTIV\" in s`` — ``\"INACTIVE\".upper()`` contains
        ``ACTIV`` and would be misclassified as active.
        """
        s = str(text).strip().upper()
        if not s:
            return False
        if "INACTIVE" in s or "DEACTIV" in s or s in ("OFF", "FALSE", "0", "NO", "DISABLED"):
            return False
        if s in ("ACTIVE", "ACTIVATED", "ACTIVATE", "ON", "TRUE", "1", "YES", "ENABLED"):
            return True
        if "ACTIVATED" in s and "DEACTIV" not in s:
            return True
        return False

    def broker_kill_switch_reports_active(self) -> bool:
        """True when GET /killswitch indicates kill switch is on (trading disabled)."""
        if not self.dhan or not hasattr(self.dhan, "status_kill_switch"):
            return False
        try:
            r = self.dhan.status_kill_switch()
        except Exception:
            return False
        if not isinstance(r, dict) or r.get("status") != "success":
            return False
        data = r.get("data")
        if isinstance(data, dict):
            for key in ("killSwitchStatus", "kill_switch_status", "killSwitch", "status"):
                val = data.get(key)
                if val is None:
                    continue
                if self._kill_switch_text_means_active(str(val)):
                    return True
            return False
        if isinstance(data, str):
            return self._kill_switch_text_means_active(data)
        return False

    def log(self, message: str, level: str = "INFO"):
        """Log message to both file and console, with immediate flush"""
        try:
            if self.logger:
                if level == "ERROR":
                    self.logger.error(message)
                elif level == "WARNING":
                    self.logger.warning(message)
                else:
                    self.logger.info(message)
            else:
                # Fallback to print
                print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {level}: {message}")
            
            # Force immediate flush
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception as e:
            # Last resort - direct print
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {level}: {message}")
            sys.stdout.flush()

    def load_credentials(self) -> bool:
        """
        Load clientid and token from cred.yml (the token is generated separately by
        generate_token.py) and initialize the Dhan API.

        Returns:
            bool: True if credentials loaded and API initialized successfully, False otherwise
        """
        try:
            if not DHAN_AVAILABLE:
                print("❌ Dhan API not available")
                return False

            cred_file = os.path.join(self._base_path(), 'cred.yml')
            self.log(f"Loading credentials from {cred_file}...")

            if not os.path.exists(cred_file):
                print(f"❌ Credentials file not found: {cred_file}")
                return False

            with open(cred_file, 'r') as f:
                credentials = yaml.load(f, Loader=yaml.FullLoader)

            if not credentials:
                print(f"❌ Empty credentials file: {cred_file}")
                return False

            client_id = credentials.get('clientid')
            access_token = credentials.get('token')
            if not client_id or not access_token:
                print(f"❌ cred.yml needs 'clientid' and 'token' (run generate_token.py to create the token)")
                return False

            self.credentials = credentials
            self.dhan = dhanhq(DhanContext(client_id, access_token))
            print("✅ Dhan API initialized with token from cred.yml")
            return True

        except Exception as e:
            print(f"❌ Error loading credentials: {e}")
            import traceback
            traceback.print_exc()
            self.credentials = None
            self.dhan = None
            return False

    def test_api_connection(self) -> bool:
        """Test the Dhan API connection"""
        try:
            if not self.dhan:
                return False
                
            test_response = self.dhan.get_positions() 
            if test_response.get("status") != "success":
                print(f"⚠️  API Test Warning: {test_response.get('message', 'Unknown')}")
                return False
            print("✅ API connection test successful")
            return True
        except Exception as e:
            print(f"❌ API Test Failed: {e}")
            return False

    def has_open_position(self) -> bool:
        """Return True if Dhan reports any position with a non-zero net quantity."""
        try:
            response = self.dhan.get_positions()
        except Exception as e:
            self.log(f"Could not fetch positions: {e}", level="WARNING")
            return False
        if not isinstance(response, dict) or response.get("status") != "success":
            return False
        for pos in response.get("data") or []:
            try:
                if float(pos.get("netQty", 0) or 0) != 0:
                    return True
            except (TypeError, ValueError, AttributeError):
                continue
        return False

    def ensure_pnl_exit(self) -> None:
        """Configure P&L exit once, only after an open position exists."""
        if self.pnl_exit_config is None or self.pnl_exit_configured:
            return
        if not self.has_open_position():
            return
        result = self.configure_pnl_exit()
        if "error" in result:
            self.log(f"❌ Failed to configure Dhan P&L exit (will retry): {result['error']}", level="ERROR")
            return
        self.pnl_exit_configured = True
        if "verification_error" in result:
            self.log(
                "✅ Dhan accepted the P&L exit configuration, but its status could not "
                f"be verified: {result['verification_error']}. The configuration will "
                "not be submitted again.",
                level="WARNING",
            )
            return

        verification = result["verification"]
        verification_data = verification.get("data")
        if not isinstance(verification_data, dict):
            verification_data = {}
        pnl_exit_status = str(verification_data.get("pnlExitStatus", "")).strip().upper()
        if pnl_exit_status == "ACTIVE":
            self.log(f"✅ Dhan P&L exit is active: {result}")
        elif pnl_exit_status == "PENDING":
            self.log(
                "✅ Dhan accepted and registered the P&L exit; broker reports "
                f"PENDING activation: {result}"
            )
        elif "NO ACTIVE PNL TRACKING" in str(verification_data.get("message", "")).upper():
            self.log(
                "✅ Dhan accepted and registered the P&L exit; broker is awaiting "
                f"active P&L tracking: {result}"
            )
        else:
            self.log(
                "✅ Dhan accepted the P&L exit configuration; broker status is "
                f"{pnl_exit_status or 'unknown'}: {result}",
                level="WARNING",
            )

    def count_completed_trades(self) -> int:
        """
        Count completed trades (orders with status = 'TRADED').
        
        If the order endpoint fails, reload the token written to cred.yml by a
        sibling process and retry once. Failures are logged only when the API
        enters or recovers from the broken state.
        """
        try:
            if not self.dhan:
                return 0

            orders_response = self.dhan.get_order_list()
            if not isinstance(orders_response, dict) or orders_response.get("status") != "success":
                remarks = (
                    orders_response.get("remarks", "")
                    if isinstance(orders_response, dict)
                    else repr(orders_response)
                )
                self.log(
                    f"get_order_list failed: {remarks}. Reloading token from cred.yml and retrying...",
                    level="WARNING",
                )
                if self._reload_token_from_file():
                    orders_response = self.dhan.get_order_list()

                if not isinstance(orders_response, dict) or orders_response.get("status") != "success":
                    if not self._order_api_broken:
                        self._order_api_broken = True
                        remarks = (
                            orders_response.get("remarks", "")
                            if isinstance(orders_response, dict)
                            else repr(orders_response)
                        )
                        self.log(
                            f"get_order_list still failing after token reload: {remarks}; "
                            "trade count cannot trigger the kill switch until this is resolved.",
                            level="ERROR",
                        )
                    return 0

                if self._order_api_broken:
                    self.log("get_order_list recovered after reloading the token.")
                self._order_api_broken = False

            orders_data = orders_response.get("data") or []
            if not isinstance(orders_data, list):
                raise ValueError("Dhan order response data is not a list")
            return sum(
                1
                for order in orders_data
                if isinstance(order, dict) and order.get("orderStatus") == "TRADED"
            )
        except Exception as e:
            self.log(f"Error counting completed trades: {e}", level="ERROR")
            return 0

    def _reload_token_from_file(self) -> bool:
        """Reload and validate a token another process may have refreshed in cred.yml."""
        try:
            cred_file = os.path.join(self._base_path(), "cred.yml")
            with open(cred_file, "r") as f:
                credentials = yaml.load(f, Loader=yaml.FullLoader) or {}

            client_id = credentials.get("clientid")
            token = credentials.get("token")
            if not client_id or not token or not DhanContext or not dhanhq:
                self.log("token_reload: cred.yml is missing a usable clientid/token.", level="WARNING")
                return False

            candidate = dhanhq(DhanContext(client_id, token))
            response = candidate.get_positions()
            if not isinstance(response, dict) or response.get("status") != "success":
                remarks = response.get("remarks", "") if isinstance(response, dict) else repr(response)
                self.log(f"token_reload: the token in cred.yml is invalid: {remarks}", level="WARNING")
                return False

            self.dhan = candidate
            if self.credentials is None:
                self.credentials = {}
            self.credentials["clientid"] = client_id
            self.credentials["token"] = token
            return True
        except Exception as e:
            self.log(f"token_reload: could not reload token from cred.yml: {e}", level="ERROR")
            return False

    def configure_pnl_exit(self) -> Dict[str, Any]:
        """Configure and verify Dhan's automatic P&L exit using cred.yml settings."""
        if not self.dhan or not self.pnl_exit_config:
            return {"error": "Dhan connection or P&L exit configuration is unavailable"}
        if not hasattr(self.dhan, "set_pnl_exit") or not hasattr(self.dhan, "get_pnl_exit"):
            return {"error": "Installed Dhan client does not support P&L exit configuration"}

        try:
            response = self.dhan.set_pnl_exit(
                profit_value=self.pnl_exit_config["profit_value"],
                # Dhan represents the loss threshold as a negative amount.
                loss_value=-self.pnl_exit_config["loss_value"],
                product_type=self.pnl_exit_config["product_type"],
                enable_kill_switch=self.pnl_exit_config["enable_kill_switch"],
            )
        except Exception as e:
            return {"error": f"set_pnl_exit failed: {e}"}

        if not isinstance(response, dict) or response.get("status") != "success":
            return {"error": f"Dhan did not accept the P&L exit configuration: {response!r}"}
        data = response.get("data")
        if isinstance(data, dict) and str(data.get("status", "")).upper() == "ERROR":
            return {"error": f"Dhan rejected the P&L exit: {data.get('message', response)!r}"}

        try:
            verification = self.dhan.get_pnl_exit()
        except Exception as e:
            return {
                "response": response,
                "verification_error": f"status request failed: {e}",
            }

        if not isinstance(verification, dict) or verification.get("status") != "success":
            return {
                "response": response,
                "verification_error": (
                    f"Dhan returned an unsuccessful status check: {verification!r}"
                ),
            }

        return {"response": response, "verification": verification}

    def set_kill_switch(self, status: str) -> Dict[str, Any]:
        """Set the Dhan kill switch to ACTIVATE or DEACTIVATE."""
        action = status.strip().upper()
        if action not in {"ACTIVATE", "DEACTIVATE"}:
            return {"error": f"Invalid kill-switch action: {status!r}"}
        if not self.dhan or not hasattr(self.dhan, "kill_switch"):
            return {"error": "Dhan kill-switch API is unavailable"}

        try:
            response = self.dhan.kill_switch(action.lower())
        except Exception as e:
            return {"error": f"Kill-switch request failed: {e}"}
        if not isinstance(response, dict) or response.get("status") != "success":
            return {"error": f"Dhan did not accept the kill-switch request: {response!r}"}
        return response

    def activate_kill_switch(self) -> bool:
        """Activate the broker kill switch without separately closing positions."""
        if self.broker_kill_switch_reports_active():
            self.kill_switch_activated = True
            self.log("Broker kill switch is already active.")
            return True

        result = self.set_kill_switch("ACTIVATE")
        if "error" in result:
            self.log(result["error"], level="ERROR")
            return False

        self.kill_switch_activated = True
        self.activation_count += 1
        self.log(f"Broker kill switch activated (activation #{self.activation_count}).")
        return True

    def run_monitoring_loop(self) -> None:
        """Monitor completed trades and broker kill-switch status until stopped."""
        self.log("=" * 60)
        self.log("Starting trade-count and P&L exit monitoring")
        self.log(f"Polling every {self.monitoring_interval} seconds")
        self.log("=" * 60)

        loop_count = 0
        next_heartbeat_at = time.monotonic() + 60
        while not self._stop_monitoring.is_set():
            try:
                if self._daily_cutoff_reached():
                    self._session_end_stop = True
                    self._stop_monitoring.set()
                    self.log("Daily IST cutoff reached; stopping monitoring.")
                    break

                if self.broker_kill_switch_reports_active():
                    self.kill_switch_activated = True
                    self.log("Broker kill switch is active; stopping monitoring.")
                    break

                loop_count += 1
                self.ensure_pnl_exit()
                completed_trades = self.count_completed_trades()
                now_monotonic = time.monotonic()
                if now_monotonic >= next_heartbeat_at:
                    self.log(
                        f"Monitoring active - loop #{loop_count}; "
                        f"trades {completed_trades}/{self.max_trades}"
                    )
                    next_heartbeat_at = now_monotonic + 60

                if completed_trades >= self.max_trades:
                    self.log(
                        f"Trade limit reached ({completed_trades}/{self.max_trades}); "
                        "activating broker kill switch."
                    )
                    if self.activate_kill_switch():
                        self.log("Trade-count limit handled; monitoring stopped.")
                        break
                    self.log(
                        "Could not activate broker kill switch; will retry on the next poll.",
                        level="ERROR",
                    )
            except Exception as e:
                self.log(f"Error in monitoring loop iteration: {e}", level="ERROR")

            sleep_seconds = float(self.monitoring_interval)
            until_cutoff = self._seconds_until_daily_cutoff_ist()
            if until_cutoff > 0:
                sleep_seconds = min(sleep_seconds, until_cutoff)
            if self._interruptible_sleep(sleep_seconds):
                break

        if self._session_end_stop:
            hh, mm = getattr(type(self), "auto_stop_time_ist", (15, 10))
            self.log(f"Monitoring ended at the daily {hh:02d}:{mm:02d} IST cutoff.")
        elif self._stop_monitoring.is_set():
            self.log("Monitoring stopped by SIGINT/SIGTERM.")


def main() -> None:
    """Initialize the monitor and run until the session ends or a limit is reached."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

    monitor = TradeCountKillSwitch()
    monitor.run_monitoring_loop()


if __name__ == "__main__":
    main()
