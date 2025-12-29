# TeslaUSB Optimization Recommendations

## Completed Items ✅

| Item | Impact | Completed |
|------|--------|-----------|
| **Lazy-load PyAV imports** | Reduced baseline memory ~10MB | 2024-12-29 |
| **Reduce Waitress threads 6→3** | Saves ~5-10MB, matches 4-core Pi | 2024-12-29 |
| **Remove duplicate VIDEO_EXTENSIONS** | Code deduplication | 2024-12-29 |
| **Use os.scandir() context managers** | Prevents directory handle leaks | 2024-12-29 |
| **Set Flask TEMPLATES_AUTO_RELOAD=False** | Saves inode watches | 2024-12-29 |
| **Standardize logging setup** | Consistent debugging across all services | 2024-12-29 |
| **Consolidate camera angle definitions** | Single source of truth in config.py | 2024-12-29 |
| **Create base template context function** | Reduced ~50 lines of repeated code | 2024-12-29 |
| **Load Bootstrap Icons locally** | Enables offline AP mode functionality | 2024-12-29 |

---

## Removed Items (Not Material Impact)

The following items were removed from the original list because analysis showed they provide negligible benefit or introduce unnecessary risk:

| Removed Item | Reason |
|--------------|--------|
| **Lazy-load service imports in blueprints** | Python imports are cached after first load. Services are pure Python code (~KB each), not heavy data. The real savings came from PyAV (already done). |
| **Use generator patterns for video/file listing** | `get_video_files()` and `get_events()` already use efficient `os.scandir()`. Converting to generators would break sorting (needs full list) and pagination. Callers need list features. |
| **Stream light show ZIP downloads** | Already writes to temp file on disk (not memory). Light shows are small (~10-20MB max). No memory concern. |
| **Unify file validation patterns** | Each file type (chimes, wraps, light shows) has genuinely different validation rules. A "unified" validator would add abstraction without simplification. |
| **Extract common subprocess patterns** | Context-specific error handling is valuable. A generic wrapper would either be too generic (useless) or too complex (worse than current). |
| **Consistent error return patterns** | Changing return signatures would require updating all callers. High risk, no user-facing benefit. |
| **Standardize mount path resolution** | Different methods exist for different use cases: `get_mount_path()` for single lookup, `iter_all_partitions()` for scanning. Intentional design. |
| **Consistent datetime formatting** | Different contexts genuinely need different formats (display vs logging vs filenames). Forcing one format reduces clarity. |
| **Flask application factory pattern** | Adds complexity for theoretical testability. This is an embedded production system, not a library. |
| **Move holiday calculations to startup** | Analyzed: ~1-2ms CPU per page load for date arithmetic. Not material. |
| **lru_cache for mode_service** | DANGEROUS: `current_mode()` checks live system state. Caching would return stale data when mode changes. |
| **Add loading indicator for video folder** | UI change - out of scope for optimization work. |
| **Minify CSS for production** | ~50KB savings on local network is imperceptible. Adds build complexity. |
| **Consolidate CSS files** | HTTP/2 multiplexing makes "fewer requests" optimization obsolete. |
| **Add __slots__ to classes** | Classes instantiated once per request, not thousands of times. Negligible savings. |
| **Incremental JSON parsing** | Config files are < 10KB. `json.load()` is already optimized C code. |
| **Merge partition_service.py and partition_mount_service.py** | Intentional separation: partition_service (70 lines) is simple path utilities; partition_mount_service (693 lines) is complex mount/LUN operations. Not all consumers need both. Merging adds risk to critical USB gadget code with no simplification benefit. |
