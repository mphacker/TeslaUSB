"""
Cloud Bandwidth Test Service.

Tests upload speed at escalating bandwidth limits while monitoring
system health (web latency, memory, load). Finds the highest safe
upload speed that doesn't degrade Pi responsiveness.

Creates a small temp file on tmpfs (no SD card I/O), uploads it to
cloud at each speed level, measures system impact, and cleans up.
"""

import logging
import os
import subprocess
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_bw_test_lock = threading.Lock()
_bw_test_status: Dict = {
    "running": False,
    "progress": "",
    "results": [],
    "recommended_mbps": None,
    "error": None,
}

# Test levels (Mbps) — escalating until system becomes unresponsive
_TEST_LEVELS = [2, 5, 10, 15, 20, 30, 50]

# Thresholds for "system is still responsive"
_MAX_WEB_LATENCY_S = 1.5      # Web server must respond within this
_MIN_FREE_MEMORY_MB = 50       # Must have this much RAM available
_MAX_LOAD_AVERAGE = 3.5        # 1-minute load average cap
_TEST_FILE_SIZE_MB = 10        # Small enough to finish quickly per level
_TMPFS_DIR = "/run/teslausb"
_TEST_FILENAME = "bw_test_payload.bin"


def get_bandwidth_test_status() -> dict:
    """Return current test status for UI polling."""
    return dict(_bw_test_status)


def start_bandwidth_test(conf_path: str, remote_path: str) -> tuple:
    """Start bandwidth test in background thread.

    Args:
        conf_path: Path to rclone config file.
        remote_path: Remote path prefix (e.g., "TeslaUSB").

    Returns (success, message).
    """
    with _bw_test_lock:
        if _bw_test_status["running"]:
            return False, "Bandwidth test already running"

    t = threading.Thread(
        target=_run_bandwidth_test,
        args=(conf_path, remote_path),
        daemon=True,
    )
    t.start()
    return True, "Bandwidth test started"


def _measure_web_latency() -> float:
    """Measure web server response time in seconds."""
    try:
        start = time.time()
        result = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{time_total}",
             "http://localhost/settings/"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
        return time.time() - start
    except Exception:
        return 99.0


def _get_free_memory_mb() -> float:
    """Return available memory in MB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except Exception:
        pass
    return 0.0


def _get_load_average() -> float:
    """Return 1-minute load average."""
    try:
        return os.getloadavg()[0]
    except Exception:
        return 0.0


def _run_bandwidth_test(conf_path: str, remote_path: str) -> None:
    """Background thread: test upload at escalating speeds."""
    global _bw_test_status

    _bw_test_status.update({
        "running": True,
        "progress": "Preparing test file…",
        "results": [],
        "recommended_mbps": None,
        "error": None,
    })

    test_file = os.path.join(_TMPFS_DIR, _TEST_FILENAME)
    remote_test_dir = f"teslausb:{remote_path}/.bw_test"
    mem_flags = ["--buffer-size", "0", "--transfers", "1", "--checkers", "1"]

    try:
        # Create test file on tmpfs (no SD card I/O)
        os.makedirs(_TMPFS_DIR, exist_ok=True)
        with open(test_file, "wb") as f:
            f.write(os.urandom(_TEST_FILE_SIZE_MB * 1024 * 1024))

        results: List[Dict] = []
        best_mbps = _TEST_LEVELS[0]

        for mbps in _TEST_LEVELS:
            _bw_test_status["progress"] = f"Testing {mbps} Mbps…"
            logger.info("Bandwidth test: trying %d Mbps", mbps)

            # Baseline measurements before upload
            pre_latency = _measure_web_latency()
            pre_memory = _get_free_memory_mb()
            pre_load = _get_load_average()

            # Upload test file
            start = time.time()
            try:
                result = subprocess.run(
                    [
                        "nice", "-n", "19", "ionice", "-c", "3",
                        "rclone", "copyto", "--config", conf_path,
                        "--bwlimit", f"{mbps}M",
                        "--stats", "0", "--log-level", "ERROR",
                        *mem_flags,
                        test_file,
                        f"{remote_test_dir}/{_TEST_FILENAME}",
                    ],
                    capture_output=True, text=True, timeout=120,
                )
                upload_ok = result.returncode == 0
                elapsed = time.time() - start
            except subprocess.TimeoutExpired:
                upload_ok = False
                elapsed = 120
            except Exception as e:
                logger.warning("Upload failed at %d Mbps: %s", mbps, e)
                upload_ok = False
                elapsed = time.time() - start

            if not upload_ok:
                results.append({
                    "mbps": mbps, "status": "upload_failed",
                    "actual_mbps": 0, "web_latency": 0,
                    "free_memory_mb": 0, "load_avg": 0,
                })
                break

            actual_mbps = round((_TEST_FILE_SIZE_MB * 8) / elapsed, 1) if elapsed > 0 else 0

            # Measure system health DURING the tail end / right after upload
            post_latency = _measure_web_latency()
            post_memory = _get_free_memory_mb()
            post_load = _get_load_average()

            # Use the worse of pre/post measurements
            web_latency = max(pre_latency, post_latency)
            free_mem = min(pre_memory, post_memory)
            load_avg = max(pre_load, post_load)

            level_result = {
                "mbps": mbps,
                "actual_mbps": actual_mbps,
                "web_latency": round(web_latency, 3),
                "free_memory_mb": round(free_mem, 0),
                "load_avg": round(load_avg, 2),
                "elapsed_s": round(elapsed, 1),
                "status": "ok",
            }

            # Check thresholds
            if web_latency > _MAX_WEB_LATENCY_S:
                level_result["status"] = "web_slow"
            elif free_mem < _MIN_FREE_MEMORY_MB:
                level_result["status"] = "low_memory"
            elif load_avg > _MAX_LOAD_AVERAGE:
                level_result["status"] = "high_load"

            results.append(level_result)
            logger.info(
                "Bandwidth test: %d Mbps → actual=%.1f Mbps, latency=%.3fs, mem=%.0fMB, load=%.2f [%s]",
                mbps, actual_mbps, web_latency, free_mem, load_avg, level_result["status"],
            )

            if level_result["status"] == "ok":
                best_mbps = mbps
            else:
                # System degraded — stop testing higher speeds
                break

            # Clean up remote test file before next iteration
            try:
                subprocess.run(
                    ["rclone", "delete", "--config", conf_path,
                     *mem_flags, f"{remote_test_dir}/{_TEST_FILENAME}"],
                    capture_output=True, timeout=30,
                )
            except Exception:
                pass

            # Brief pause between tests to let system stabilize
            time.sleep(3)

        _bw_test_status.update({
            "running": False,
            "progress": f"Complete — recommended: {best_mbps} Mbps",
            "results": results,
            "recommended_mbps": best_mbps,
        })
        logger.info("Bandwidth test complete: recommended %d Mbps", best_mbps)

    except Exception as e:
        logger.exception("Bandwidth test failed")
        _bw_test_status.update({
            "running": False,
            "progress": f"Error: {e}",
            "error": str(e),
        })

    finally:
        # Clean up local test file
        try:
            os.unlink(test_file)
        except OSError:
            pass
        # Clean up remote test directory
        try:
            subprocess.run(
                ["rclone", "purge", "--config", conf_path,
                 *mem_flags, remote_test_dir],
                capture_output=True, timeout=30,
            )
        except Exception:
            pass
